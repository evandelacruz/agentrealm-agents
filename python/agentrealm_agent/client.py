"""HTTP client for the Agent Realm API. Standard library only."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class ApiError(Exception):
    """A non-2xx answer. `code` is the body's `code` field when there is one.

    Status 0 is no answer at all: the connection failed, reset, or timed out.
    """

    status: int
    code: str
    retry_after: float | None = None

    def __str__(self) -> str:
        if self.network:
            return f"network error: {self.code}"
        return f"HTTP {self.status} {self.code}"

    @property
    def network(self) -> bool:
        return self.status == 0

    @property
    def rate_limited(self) -> bool:
        return self.status == 429

    @property
    def paused(self) -> bool:
        return self.status == 503


class Client:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None, query: dict | None = None) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise _api_error(e) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            # Refused, reset, or timed out. Retryable like a pause.
            raise ApiError(0, str(getattr(e, "reason", None) or e) or type(e).__name__) from None
        return json.loads(raw) if raw else None

    # Creation and identity reads.

    def create_character(self, world: str, name: str, avatar: str, model_agent: str) -> dict:
        return self._call(
            "POST",
            f"/worlds/{urllib.parse.quote(world)}/characters",
            {"name": name, "avatar": avatar, "model_agent": model_agent},
        )

    def self_(self, cid: int) -> dict:
        return self._call("GET", f"/characters/{cid}/self")

    def position(self, cid: int) -> dict:
        return self._call("GET", f"/characters/{cid}/position")

    def world(self, cid: int) -> dict:
        return self._call("GET", f"/characters/{cid}/world")

    # Perception.

    def terrain(self, cid: int, map_id: int, x0: int, y0: int, width: int, height: int) -> dict:
        return self._call("GET", f"/characters/{cid}/terrain-tiles", query=_rect(map_id, x0, y0, width, height))

    def entities(self, cid: int, map_id: int, x0: int, y0: int, width: int, height: int) -> dict:
        return self._call("GET", f"/characters/{cid}/entity-tiles", query=_rect(map_id, x0, y0, width, height))

    # The round trip.

    def tick(self, cid: int, intents: list[dict] | None) -> dict:
        """The round trip. `intents` replaces the held queue; None leaves it as it is."""
        body = {} if intents is None else {"intents": intents}
        return self._call("POST", f"/characters/{cid}/tick", body)


def _rect(map_id: int, x0: int, y0: int, width: int, height: int) -> dict:
    return {"map_id": map_id, "x0": x0, "y0": y0, "width": width, "height": height}


def _api_error(e: urllib.error.HTTPError) -> ApiError:
    code = ""
    try:
        body = json.loads(e.read() or b"{}")
        if isinstance(body, dict):
            code = str(body.get("code", ""))
    except (ValueError, OSError):
        pass
    retry_after = None
    header = e.headers.get("Retry-After") if e.headers else None
    if header:
        try:
            retry_after = float(header)
        except ValueError:
            pass
    return ApiError(e.code, code or e.reason or "", retry_after)
