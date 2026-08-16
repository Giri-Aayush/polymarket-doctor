"""HTTP access for checks.

Deliberately thin. Checks care about three things: did it answer, what status,
what body. Every one of those matters diagnostically, including the failures, so
nothing here raises on a non-2xx. A 401 is data.

For production use, transient failures (a dropped connection, a 502 from an edge
node) are retried with backoff. A blip on a market maker's pre-trade health
check should not flip a stage red when a second attempt would have answered. A
real 4xx is never retried: it's the exchange's answer, not a blip.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

DEFAULT_TIMEOUT = 12.0
DEFAULT_RETRIES = 2
RETRY_BACKOFF_BASE = 0.25
# Edge/proxy statuses that mean "try again", not "the exchange said no".
TRANSIENT_STATUS = frozenset({502, 503, 504})


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Any
    elapsed_ms: float
    error: str | None = None
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def reached_server(self) -> bool:
        return self.error is None

    @property
    def transient(self) -> bool:
        """A failure worth retrying, as opposed to a real answer."""
        return self.error is not None or self.status in TRANSIENT_STATUS

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
                 user_agent: str = "polymarket-doctor",
                 retries: int = DEFAULT_RETRIES,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            transport=transport,
        )
        self._retries = max(0, retries)
        self._sleep = sleep

    def get(self, url: str, *, headers: Mapping[str, str] | None = None,
            params: Mapping[str, Any] | None = None) -> Response:
        return self._send("GET", url, headers=headers, params=params)

    def post(self, url: str, *, headers: Mapping[str, str] | None = None,
             json_body: Any = None) -> Response:
        # POSTs aren't retried: the only POSTs this project makes are order and
        # quote submissions, where a blind retry could double-submit.
        return self._send("POST", url, headers=headers, json_body=json_body, retries=0)

    def _send(self, method: str, url: str, *, headers=None, params=None,
              json_body=None, retries: int | None = None) -> Response:
        budget = self._retries if retries is None else retries
        attempt = 0
        while True:
            attempt += 1
            response = self._attempt(method, url, headers, params, json_body, attempt)
            if not response.transient or attempt > budget:
                return response
            # Exponential backoff. Bounded by the retry budget, so worst case
            # here is base * (1 + 2) = 0.75s of waiting across two retries.
            self._sleep(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))

    def _attempt(self, method, url, headers, params, json_body, attempt) -> Response:
        started = time.perf_counter()
        try:
            raw = self._client.request(method, url, headers=headers, params=params, json=json_body)
        except httpx.HTTPError as exc:
            # Connect failures, DNS, TLS, timeouts. A partner behind a corporate
            # proxy hits this and needs the reason, not a stack trace.
            return Response(0, None, _ms_since(started),
                            error=f"{type(exc).__name__}: {exc}", attempts=attempt)
        return Response(raw.status_code, _decode(raw), _ms_since(started), attempts=attempt)

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
