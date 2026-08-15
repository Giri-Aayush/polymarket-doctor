from __future__ import annotations

import pytest

from polymarket_doctor.core.context import Context
from polymarket_doctor.net.endpoints import Endpoints
from polymarket_doctor.net.http import Response

# Real addresses from py-clob-client-v2#70. Public in the issue, and useful
# because the relayer's answers for them are a known fixed point.
EOA = "0x9F49475F9496c77fa95f76c7C5Bc57467B336792"
DEPOSIT_WALLET = "0x3EC7EEaa66d849a5F83bE99a3ef47c63f672d649"


class StubProbe:
    """Serves canned responses by URL substring.

    Substring rather than exact match so a test can say "/deployed" without
    rebuilding the query string.
    """

    def __init__(self, routes: dict[str, Response] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, *, headers=None, params=None) -> Response:
        self.calls.append(("GET", url))
        return self._match(url)

    def post(self, url: str, *, headers=None, json_body=None) -> Response:
        self.calls.append(("POST", url))
        return self._match(url)

    def _match(self, url: str) -> Response:
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        raise AssertionError(f"StubProbe has no route for {url}")


def ok(body, status: int = 200) -> Response:
    return Response(status, body, 1.0)


def http_error(status: int, body=None) -> Response:
    return Response(status, body, 1.0)


def unreachable(message: str = "ConnectError: refused") -> Response:
    return Response(0, None, 1.0, error=message)


@pytest.fixture
def endpoints() -> Endpoints:
    return Endpoints()


@pytest.fixture
def make_context(endpoints):
    def _make(probe, **kwargs) -> Context:
        return Context(endpoints=endpoints, probe=probe, **kwargs)
    return _make
