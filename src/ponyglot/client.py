"""Client for the Ponyglot Cloud API v1 (standard library only)."""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .conf import get_config

logger = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class APIError(Exception):
    """The cloud answered with an error (`status` is the HTTP status, `data` its JSON body)."""

    def __init__(self, status, message, data=None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.data = data or {}

    @property
    def code(self):
        return self.data.get("code", "") if isinstance(self.data, dict) else ""


class ConfigurationError(Exception):
    pass


def urllib_transport(method, url, headers, body, timeout):
    """Send one request; return `(status, body bytes)`. HTTP errors are returned, not raised."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class Client:
    """Talks to one site's API (the API key decides which site).

    `transport` is swappable for tests: `(method, url, headers, body, timeout) -> (status,
    bytes)`. Temporary failures (429, 5xx, connection errors) are retried with backoff.
    """

    def __init__(self, api_key=None, api_url=None, *, timeout=None, transport=None, retries=3):
        # Django settings only for what isn't passed: the gettext CLI runs without Django.
        config = get_config() if not (api_key and api_url and timeout) else None
        self.api_key = api_key or config.api_key
        self.api_url = (api_url or config.api_url).rstrip("/")
        if not self.api_key:
            raise ConfigurationError("PONYGLOT['API_KEY'] is not set.")
        if not self.api_url.startswith(("https://", "http://")):
            raise ConfigurationError(f"Not an http(s) URL: {self.api_url}")
        self.timeout = timeout or config.timeout
        self.transport = transport or urllib_transport
        self.retries = retries

    def request(self, method, path, payload=None, params=None):
        url = self.api_url + path
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(query)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": f"ponyglot/{__version__}",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        for attempt in range(self.retries + 1):
            try:
                status, content = self.transport(method, url, headers, body, self.timeout)
            except OSError as error:  # connection refused, DNS, timeout
                if attempt == self.retries:
                    raise APIError(0, f"cannot reach {self.api_url}: {error}") from error
                status, content = None, b""
            if status is not None and status not in RETRY_STATUSES:
                break
            if attempt < self.retries:
                delay = 2**attempt
                logger.warning("Ponyglot %s %s: %s; retrying in %ss", method, path, status, delay)
                time.sleep(delay)
        try:
            data = json.loads(content) if content else None
        except ValueError:
            # Not the API: e.g. an HTML error page because API_URL points to the dashboard or
            # misses "/v1".
            raise APIError(
                status,
                f"{method} {url} didn't answer like the Ponyglot API (not JSON). Check "
                f"PONYGLOT['API_URL'] (now {self.api_url}); it ends in /v1.",
            ) from None
        if status >= 400:
            message = data.get("detail", data) if isinstance(data, dict) else content[:500]
            raise APIError(status, message, data)
        return data

    # --- Endpoints (docs/api.md in ponyglot-cloud) -------------------------------------------

    def handshake(self, *, adapters, source_language, languages, capabilities=()):
        return self.request(
            "POST",
            "/handshake",
            {
                "connector_version": __version__,
                "adapters": list(adapters),
                "source_language": source_language,
                "languages": list(languages),
                "capabilities": list(capabilities),
            },
        )

    def push_units(self, units):
        """Push snapshots (≤ 100); returns one `{"external_key", "ok", "result"|"error"}` each."""
        return self.request("PUT", "/units/batch", {"units": list(units)})["results"]

    def delete_unit(self, external_key):
        self.request("DELETE", f"/units/{urllib.parse.quote(external_key, safe='')}")

    def unit_keys(self, prefix=None, cursor=None, limit=500):
        """One page of every live unit of the site (`{"units", "next_cursor"}`), without
        counts."""
        params = {"prefix": prefix, "cursor": cursor, "limit": limit}
        return self.request("GET", "/units/keys", params=params)

    def unit_status(self, external_key):
        return self.request("GET", f"/units/{urllib.parse.quote(external_key, safe='')}/status")

    def site_status(self, prefix=None):
        """Counts per language (`prefix`: units whose key starts with it, e.g. one model's)."""
        return self.request("GET", "/status", params={"prefix": prefix} if prefix else None)

    def units_needing_work(self, prefix=None, language=None, cursor=None, limit=50):
        """One page of the units with something not up to date (`{"units", "next_cursor"}`)."""
        params = {"prefix": prefix, "language": language, "cursor": cursor, "limit": limit}
        return self.request("GET", "/units", params=params)

    def create_job(self, **payload):
        return self.request("POST", "/jobs", payload)

    def jobs(self, active=False):
        """The site's 20 most recent jobs (`active`: only queued and running)."""
        return self.request("GET", "/jobs", params={"active": "true"} if active else None)

    def job(self, job_id):
        return self.request("GET", f"/jobs/{int(job_id)}")

    def results(self, limit=100, unit=None):
        """All translations ready for delivery (follows the cursor); `unit`: only that unit's
        (a cloud from before the filter ignores it, so callers that need it filter again)."""
        cursor = None
        while True:
            params = {"cursor": cursor, "limit": limit, "unit": unit}
            page = self.request("GET", "/results", params=params)
            yield from page["results"]
            cursor = page.get("next_cursor")
            if not cursor:
                return

    def attention(self):
        """Translations held back for QA review (`{"qa_mode", "items", "truncated"}`)."""
        return self.request("GET", "/attention")

    def acknowledge(self, ids):
        return self.request("POST", "/results/ack", {"ids": list(ids)})

    def report_reviews(self, reviews):
        return self.request("POST", "/reviews", {"reviews": list(reviews)})
