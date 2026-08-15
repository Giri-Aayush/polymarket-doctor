"""Stage 6 against a fake feed on localhost. No network, no Polymarket."""

from __future__ import annotations

import asyncio
import json
import socket
import threading

import pytest
import websockets
from conftest import StubProbe

from polymarket_doctor import issues
from polymarket_doctor.checks.websocket_feed import MarketFeed
from polymarket_doctor.core.check import Severity
from polymarket_doctor.core.context import Context
from polymarket_doctor.core.facts import Fact
from polymarket_doctor.net.endpoints import Endpoints

TOKEN = "27146956652877944551877724690365745048289675287536243265951843487691050802191"

# Shaped like the live feed: a JSON array wrapping the event, not a bare
# object. The check has to handle exactly this, so the fixture mirrors it.
BOOK_FRAME = json.dumps([{
    "event_type": "book",
    "asset_id": TOKEN,
    "market": "0x" + "ab" * 32,
    "bids": [{"price": "0.48", "size": "30"}, {"price": "0.49", "size": "20"}],
    "asks": [{"price": "0.52", "size": "25"}],
    "timestamp": "1757908892351",
    "hash": "0xabc",
}])


class FeedServer:
    """websockets server on its own thread and loop.

    The check owns the main thread's loop via asyncio.run, so the fake feed
    needs a loop of its own or the two would deadlock.
    """

    def __init__(self, handler) -> None:
        self._handler = handler
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.port: int = 0
        self.received: list[str] = []

    def start(self) -> FeedServer:
        self._thread.start()
        assert self._ready.wait(5), "fake feed server failed to start"
        return self

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with websockets.serve(self._dispatch, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    async def _dispatch(self, ws) -> None:
        await self._handler(self, ws)


async def send_book(server: FeedServer, ws) -> None:
    server.received.append(await ws.recv())
    await ws.send(BOOK_FRAME)
    # Hold the connection so the client closes on its own schedule; returning
    # here would race a close frame against the client's recv.
    await ws.wait_closed()


async def stay_silent(server: FeedServer, ws) -> None:
    server.received.append(await ws.recv())
    await ws.wait_closed()


async def hang_up(server: FeedServer, ws) -> None:
    # Handshake completes, then the server closes without a single frame.
    return


@pytest.fixture
def feed_server():
    servers: list[FeedServer] = []

    def _start(handler) -> FeedServer:
        server = FeedServer(handler).start()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.stop()


def context_for(port: int, token_id: str | None = TOKEN) -> Context:
    # Endpoints is frozen, so tests build a fresh one pointed at localhost.
    ctx = Context(
        endpoints=Endpoints(websocket=f"ws://127.0.0.1:{port}/ws"),
        probe=StubProbe(),
    )
    if token_id is not None:
        ctx.facts.set(Fact.TOKEN_ID, token_id)
    return ctx


def fast_check() -> MarketFeed:
    return MarketFeed(connect_timeout=2.0, first_message_timeout=0.75)


def refused_port() -> int:
    # Bind then release: the port existed a moment ago and nothing listens now,
    # so connecting to it is a deterministic refusal.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestPass:
    def test_book_snapshot_passes_with_latency_evidence(self, feed_server):
        ctx = context_for(feed_server(send_book).port)
        finding = fast_check().run(ctx)

        assert finding.severity is Severity.PASS
        assert finding.evidence["first_message_ms"] >= 0
        assert finding.evidence["event_type"] == "book"
        assert finding.evidence["bid_levels"] == 2
        assert finding.evidence["ask_levels"] == 1
        assert ctx.facts.get(Fact.WS_CONNECTED) is True

    def test_subscribe_is_exactly_the_documented_shape(self, feed_server):
        server = feed_server(send_book)
        fast_check().run(context_for(server.port))

        # The only thing this check may ever send. Anything extra means the
        # subscribe-only contract broke.
        assert len(server.received) == 1
        assert json.loads(server.received[0]) == {"assets_ids": [TOKEN], "type": "market"}

    def test_pass_detail_carries_the_stream_stops_caveat(self, feed_server):
        ctx = context_for(feed_server(send_book).port)
        finding = fast_check().run(ctx)

        # A green stage 6 that doesn't mention the silent-stall bug would send
        # partners to production without staleness detection.
        assert issues.WEBSOCKET_STREAM_STOPS.slug in finding.detail
        assert "resubscribe" in finding.detail


class TestWarn:
    def test_silent_feed_warns_and_suggests_a_busier_token(self, feed_server):
        ctx = context_for(feed_server(stay_silent).port)
        finding = fast_check().run(ctx)

        assert finding.severity is Severity.WARN
        assert "volume" in finding.remedy.lower()
        # It did connect — the ambiguity is about the data, not the transport.
        assert ctx.facts.get(Fact.WS_CONNECTED) is True


class TestFail:
    def test_connection_refused_fails(self):
        ctx = context_for(refused_port())
        finding = fast_check().run(ctx)

        assert finding.severity is Severity.FAIL
        assert ctx.facts.get(Fact.WS_CONNECTED) is False

    def test_close_after_handshake_fails(self, feed_server):
        ctx = context_for(feed_server(hang_up).port)
        finding = fast_check().run(ctx)

        assert finding.severity is Severity.FAIL
        assert "closed" in finding.summary
        assert ctx.facts.get(Fact.WS_CONNECTED) is False


class TestSkip:
    def test_missing_token_id_skips_and_records_none(self):
        ctx = context_for(port=1, token_id=None)
        finding = fast_check().run(ctx)

        assert finding.severity is Severity.SKIP
        # None is written, not omitted: the report should show the stage was
        # considered and had nothing to work with.
        assert Fact.WS_CONNECTED in ctx.facts
        assert ctx.facts.get(Fact.WS_CONNECTED) is None
