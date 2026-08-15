"""HTTP access for checks.

Deliberately thin. Checks care about three things — did it answer, what status,
what body — and every one of those matters diagnostically, including the
failures. So nothing here raises on a non-2xx; a 401 is data.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

DEFAULT_TIMEOUT = 12.0


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Any
    elapsed_ms: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def reached_server(self) -> bool:
        return self.error is None

    def error_message(self) -> str | None:
        """Polymarket puts its reason in {"error": "..."} on both services."""
        if isinstance(self.body, Mapping):
            message = self.body.get("error")
            if isinstance(message, str):
                return message
        return None


class HttpProbe(Protocol):
    """What checks depend on. Tests pass a stub instead of hitting the network."""

    def get(self, url: str, *, headers: Mapping[str, str] | None = ...,
            params: Mapping[str, Any] | None = ...) -> Response: ...

    def post(self, url: str, *, headers: Mapping[str, str] | None = ...,
             json_body: Any = ...) -> Response: ...


class HttpxProbe:
    """Real implementation. One client per run so connections get reused."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT,
                 user_agent: str = "polymarket-doctor") -> None:
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        )

    def get(self, url: str, *, headers: Mapping[str, str] | None = None,
            params: Mapping[str, Any] | None = None) -> Response:
        return self._send("GET", url, headers=headers, params=params)

    def post(self, url: str, *, headers: Mapping[str, str] | None = None,
             json_body: Any = None) -> Response:
        return self._send("POST", url, headers=headers, json_body=json_body)

    def _send(self, method: str, url: str, *, headers=None, params=None,
              json_body=None) -> Response:
        started = time.perf_counter()
        try:
            raw = self._client.request(method, url, headers=headers, params=params, json=json_body)
        except httpx.HTTPError as exc:
            # Connect failures, DNS, TLS, timeouts. A partner behind a corporate
            # proxy hits this and needs the reason, not a stack trace.
            return Response(0, None, _ms_since(started), error=f"{type(exc).__name__}: {exc}")
        return Response(raw.status_code, _decode(raw), _ms_since(started))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpxProbe:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _decode(raw: httpx.Response) -> Any:
    try:
        return raw.json()
    except (json.JSONDecodeError, ValueError):
        # Cloudflare and nginx error pages land here. Truncated because a check
        # only needs enough to tell "HTML error page" from "JSON error body".
        return raw.text[:500]
