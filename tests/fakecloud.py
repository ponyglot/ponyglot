"""An in-memory stand-in for the Ponyglot Cloud API v1, plugged in as the client transport."""

import json
import urllib.parse
from itertools import count

from ponyglot.fingerprint import fingerprint


class FakeCloud:
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
        assert headers["Authorization"] == "Bearer pg_test"
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
        if method == "DELETE" and path.startswith("/units/"):
            key = urllib.parse.unquote(path.removeprefix("/units/"))
            self.units.pop(key, None)
            self.deleted.append(key)
            return 204, None
        if (method, path) == ("GET", "/results"):
            pending = [r for r in self.results if r["id"] not in self.acked]
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
            self.jobs[job_id] = {"id": job_id, "status": "succeeded", "errors": [], **estimate}
            return 202, self.jobs[job_id]
        if method == "GET" and path.startswith("/jobs/"):
            return 200, self.jobs[int(path.rsplit("/", 1)[1])]
        return 404, {"detail": f"not found: {method} {path}"}

    def add_result(self, unit, key, language, text, *, source, format="plain", qa=None):
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
        }
        self.results.append(result)
        return result
