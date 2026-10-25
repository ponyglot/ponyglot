"""Test helpers for connector packages: an in-memory stand-in for the Ponyglot Cloud API v1.

cloud = FakeCloud()
client = Client(transport=cloud)   # ponyglot.client.Client
sync.run(client)
cloud.add_result(unit_key, segment_key, "de", "Preise", source="Pricing")
"""

import json
import urllib.parse
from itertools import count

from ponyglot.fingerprint import fingerprint


class FakeCloud:
    DASHBOARD = "https://app.ponyglot.test/o/acme"

    def __init__(self):
        self.units = {}  # external_key → last snapshot payload
        self.pushes = []  # every pushed snapshot, in order
        self.deleted = []
        self.results = []  # dicts as /results returns them
        self.acked = []
        self.reviews = []
        self.handshakes = []
        self.jobs = {}
        self.requests = []
        self.reject_keys = set()  # batch items answered with ok=false
        self.fail_next = []  # statuses to answer the next requests with (e.g. 503)
        self.confirm_above = None  # characters above which jobs need confirmation
        self.qa_mode = "editors"
        self.job_status = "succeeded"  # status new jobs report (e.g. "running")
        self.languages = ["de", "fr"]  # target languages of the site
        self.states = {}  # (unit, segment key, language) → state for unit status
        self.attention = []  # items for GET /attention
        self.has_attention = True  # False: an older cloud without the endpoint
        self._ids = count(5000)

    # The transport signature of ponyglot.client.Client
    def __call__(self, method, url, headers, body, timeout):
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path.removeprefix("/v1")
        query = dict(urllib.parse.parse_qsl(parsed.query))
        payload = json.loads(body) if body else None
        self.requests.append((method, path, payload))
        if not headers.get("Authorization", "").startswith("Bearer "):
            return 401, b'{"detail": "missing key"}'
        if self.fail_next:
            return self.fail_next.pop(0), b'{"detail": "unavailable"}'
        status, data = self.route(method, path, query, payload)
        return status, json.dumps(data).encode() if data is not None else b""

    def route(self, method, path, query, payload):
        if (method, path) == ("POST", "/handshake"):
            self.handshakes.append(payload)
            return 200, {
                "name": "Test site",
                "warnings": [],
                "review_mode": "always",
                "qa_mode": self.qa_mode,
                "capabilities": payload.get("capabilities", []),
            }
        if (method, path) == ("GET", "/attention") and self.has_attention:
            return 200, {"qa_mode": self.qa_mode, "items": self.attention, "truncated": False}
        if (method, path) == ("GET", "/site"):
            return 200, {
                "name": "Test site",
                "target_languages": [
                    {"code": "de", "writable": True},
                    {"code": "fr", "writable": True},
                ],
            }
        if (method, path) == ("PUT", "/units/batch"):
            items = []
            for unit in payload["units"]:
                key = unit["external_key"]
                self.pushes.append(unit)
                if key in self.reject_keys:
                    items.append({"external_key": key, "ok": False, "error": "invalid"})
                    continue
                self.units[key] = unit
                items.append({"external_key": key, "ok": True, "result": {}})
            return 200, {"results": items}
        if (method, path) == ("GET", "/units/keys"):
            keys = [k for k in self.units if k.startswith(query.get("prefix", ""))]
            start = int(query.get("cursor") or 0)
            limit = int(query.get("limit") or 500)
            page = [
                {
                    "external_key": k,
                    "label": self.units[k].get("label", ""),
                    "adapter": self.units[k].get("adapter", ""),
                }
                for k in keys[start : start + limit]
            ]
            more = start + limit < len(keys)
            return 200, {"units": page, "next_cursor": str(start + limit) if more else None}
        if (method, path) == ("GET", "/units"):
            return 200, self._units_needing_work(query)
        if method == "GET" and path.startswith("/units/") and path.endswith("/status"):
            key = urllib.parse.unquote(path.removeprefix("/units/").removesuffix("/status"))
            unit = self.units.get(key)
            if unit is None:
                return 404, {"detail": "Not Found"}
            codes = [
                c for c in self.languages if c in (unit.get("writable_languages") or self.languages)
            ]
            held = {(i["key"], i["language"]) for i in self.attention if i["unit"] == key}
            segments = [
                {
                    "key": s["key"],
                    "states": {c: self.states.get((key, s["key"], c), "missing") for c in codes},
                    "held_back": [c for c in codes if (s["key"], c) in held],
                    "qa": {},
                }
                for s in unit["segments"]
            ]
            needing = sum(any(v != "ok" for v in s["states"].values()) for s in segments)
            return 200, {
                "external_key": key,
                "label": unit.get("label", ""),
                "path": unit.get("path", ""),
                "segments_total": len(segments),
                "segments_needing_work": needing,
                "dashboard_url": f"{self.DASHBOARD}/content/{key}/",
                "languages": [
                    {
                        "code": c,
                        "attention_url": f"{self.DASHBOARD}/attention/?unit={key}&language={c}",
                    }
                    for c in codes
                ],
                "segments": segments,
            }
        if method == "DELETE" and path.startswith("/units/"):
            key = urllib.parse.unquote(path.removeprefix("/units/"))
            self.units.pop(key, None)
            self.deleted.append(key)
            return 204, None
        if (method, path) == ("GET", "/results"):
            reviewed = {r["id"] for r in self.reviews}  # approved/rejected: not deliverable
            pending = [r for r in self.results if r["id"] not in {*self.acked, *reviewed}]
            if query.get("unit"):
                pending = [r for r in pending if r["unit"] == query["unit"]]
            start = int(query.get("cursor") or 0)
            limit = int(query.get("limit") or 100)
            page = pending[start : start + limit]
            more = start + limit < len(pending)
            return 200, {"results": page, "next_cursor": str(start + limit) if more else None}
        if (method, path) == ("POST", "/results/ack"):
            self.acked += payload["ids"]
            return 200, {"acknowledged": len(payload["ids"])}
        if (method, path) == ("POST", "/reviews"):
            self.reviews += payload["reviews"]
            return 200, {"updated": [r["id"] for r in payload["reviews"]], "ignored": []}
        if (method, path) == ("POST", "/jobs"):
            characters = sum(
                len(s["text"]) for unit in self.units.values() for s in unit["segments"]
            )
            estimate = {
                "estimated_segments": 3,
                "estimated_characters": characters,
                "estimated_tm_segments": 1,
                "cost": {"remaining_characters": 1000, "overage_characters": 0},
            }
            if payload.get("estimate_only"):
                return 200, estimate
            too_large = self.confirm_above is not None and characters > self.confirm_above
            if too_large and not payload.get("confirm"):
                return 409, {
                    "detail": "Confirm",
                    "code": "confirmation_required",
                    "estimate": estimate,
                }
            job_id = len(self.jobs) + 1
            self.jobs[job_id] = {
                "id": job_id,
                "type": payload.get("type", "delta"),
                "status": self.job_status,
                "units": payload.get("units"),
                "prefix": payload.get("prefix", ""),
                "languages": payload.get("languages") or self.languages,
                "publish": bool(payload.get("publish")),
                "publish_as": payload.get("publish_as", ""),
                "done_segments": 0,
                "failed_segments": 0,
                "errors": [],
                **estimate,
            }
            return 202, self.jobs[job_id]
        if (method, path) == ("GET", "/status"):
            prefix = query.get("prefix", "")
            units = {k: u for k, u in self.units.items() if k.startswith(prefix)}
            counts = {
                c: dict.fromkeys(("ok", "stale", "review", "missing", "attention"), 0)
                for c in self.languages
            }
            for key, unit in units.items():
                for segment in unit["segments"]:
                    for code in self.languages:
                        state = self.states.get((key, segment["key"], code), "missing")
                        counts[code][state] += 1
            return 200, {
                "units_total": len(units),
                "segments_total": sum(len(u["segments"]) for u in units.values()),
                "glossary_terms": 0,
                "languages": [{"code": c, **counts[c]} for c in self.languages],
            }
        if (method, path) == ("GET", "/jobs"):
            jobs = list(reversed(self.jobs.values()))
            if query.get("active") == "true":
                jobs = [job for job in jobs if job["status"] in ("queued", "running")]
            return 200, jobs[:20]
        if method == "GET" and path.startswith("/jobs/"):
            return 200, self.jobs[int(path.rsplit("/", 1)[1])]
        return 404, {"detail": f"not found: {method} {path}"}

    def _units_needing_work(self, query):
        prefix, language = query.get("prefix", ""), query.get("language")
        rows = []
        for key, unit in self.units.items():
            if not key.startswith(prefix):
                continue
            writable = unit.get("writable_languages") or self.languages
            languages = []
            for code in self.languages:
                if code not in writable or (language and code != language):
                    continue
                counts = dict.fromkeys(("missing", "stale", "review", "attention"), 0)
                for segment in unit["segments"]:
                    state = self.states.get((key, segment["key"], code), "missing")
                    if state != "ok":
                        counts[state] += 1
                if any(counts.values()):
                    url = f"{self.DASHBOARD}/attention/?unit={key}&language={code}"
                    languages.append(
                        {
                            "code": code,
                            **counts,
                            "attention_url": url if counts["attention"] else "",
                        }
                    )
            if languages:
                rows.append(
                    {
                        "external_key": key,
                        "label": unit.get("label", ""),
                        "path": unit.get("path", ""),
                        "adapter": unit.get("adapter", ""),
                        "last_synced_at": None,
                        "dashboard_url": f"{self.DASHBOARD}/content/{key}/",
                        "languages": languages,
                    }
                )
        start = int(query.get("cursor") or 0)
        limit = int(query.get("limit") or 100)
        more = start + limit < len(rows)
        return {
            "units": rows[start : start + limit],
            "next_cursor": str(start + limit) if more else None,
        }

    def add_result(
        self,
        unit,
        key,
        language,
        text,
        *,
        source,
        format="plain",
        qa=None,
        replaces="",
        publish_as="",
    ):
        """A finished translation; `publish_as`: its job asked to publish it right away as that
        editor (the connector's user id)."""
        result = {
            "id": f"r_{next(self._ids)}",
            "unit": unit,
            "key": key,
            "language": language,
            "text": text,
            "format": format,
            "source_fingerprint": fingerprint(source, format),
            "origin": "deepl",
            "engine": "deepl",
            "job_id": 1,
            "qa": qa or [],
            "replaces": replaces,
            "publish": bool(publish_as),
            "publish_as": str(publish_as),
        }
        self.results.append(result)
        return result
