"""Stage 6 — the market data websocket, end to end.

Polling /book carries an integration through development and falls over in
production; every serious partner ends up on the stream. This stage proves the
feed delivers a frame, not merely that the socket opens.

Verified against production on 2026-08-15:

- docs.polymarket.com/asyncapi.json names one server,
  wss://ws-subscriptions-clob.polymarket.com with pathname /ws/market, and a
  single public market channel. The subscribe message is JSON text sent right
  after the handshake: {"assets_ids": ["<token id>"], "type": "market"}. The
  server answers with an initial book snapshot per subscribed asset, and
  expects a PING every 10 seconds to keep the connection alive.
- A live probe against that host confirmed the shape, plus one thing the
  spec's examples don't show: the first frame is a JSON *array* of event
  objects, not a bare object. It landed ~200ms after subscribing and carried
  the full snapshot (event_type "book", bids/asks as price/size levels).

Market channel only. The user channel needs L2 credential semantics this stage
deliberately doesn't touch; nothing here ever sends anything but the subscribe.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .. import issues
from ..core.check import Check, Finding, Severity, Stage
from ..core.context import Context
from ..core.facts import Fact

# Live handshakes complete in ~1.5s and the first snapshot lands ~200ms after
# subscribing, so at 8s a timeout means something is wrong, not that the
# network was having a moment.
CONNECT_TIMEOUT_SECONDS = 8.0
FIRST_MESSAGE_TIMEOUT_SECONDS = 8.0


class MarketFeed(Check):
    """Connect to the market channel, subscribe, and wait for one real frame."""

    id = "ws.market-feed"
    stage = Stage.WEBSOCKET
    title = "Market data websocket delivers"
    reads = frozenset({Fact.TOKEN_ID})
    writes = frozenset({Fact.WS_CONNECTED})

    def __init__(
        self,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        first_message_timeout: float = FIRST_MESSAGE_TIMEOUT_SECONDS,
    ) -> None:
        # Constructor params so tests can walk the slow paths in milliseconds.
        self.connect_timeout = connect_timeout
        self.first_message_timeout = first_message_timeout

    def run(self, ctx: Context) -> Finding:
        token_id = ctx.facts.get(Fact.TOKEN_ID)
        if token_id is None:
            ctx.facts.set(Fact.WS_CONNECTED, None)
            return Finding(
                Severity.SKIP,
                "no token id, nothing to subscribe to",
                detail="Stage 4 didn't resolve a market, so there's no asset to "
                       "stream. Fix the market lookup and this stage runs.",
            )

        url = f"{ctx.endpoints.websocket.rstrip('/')}/market"

        # The check protocol is synchronous. asyncio.run gives the client a
        # private event loop and tears it down before we return, so nothing
        # leaks into a loop the host application might own.
        try:
            connect_ms, first_ms, raw = asyncio.run(self._subscribe_once(url, token_id))
        except TimeoutError:
            # A recv timeout is handled inside the coroutine; only the
            # handshake timing out gets here.
            ctx.facts.set(Fact.WS_CONNECTED, False)
            return Finding.fail(
                f"websocket handshake timed out after {self.connect_timeout:.0f}s",
                detail=f"No handshake from {url}. HTTPS working while wss stalls "
                       "usually means a proxy or firewall that won't pass the "
                       "Upgrade header.",
                remedy="Check egress for websocket traffic specifically — the "
                       "HTTP checks passing proves nothing about wss.",
                url=url,
            )
        except ConnectionClosed as exc:
            ctx.facts.set(Fact.WS_CONNECTED, False)
            return Finding.fail(
                "feed accepted the connection then closed it",
                detail=f"The handshake completed but the socket closed before "
                       f"delivering a frame ({exc!r}). The subscribe payload "
                       f"reaching a server that hangs up points at the service, "
                       f"not your network.",
                url=url,
            )
        except (OSError, WebSocketException) as exc:
            ctx.facts.set(Fact.WS_CONNECTED, False)
            return Finding.fail(
                f"cannot connect to {url}",
                detail=f"{type(exc).__name__}: {exc}",
                remedy="Confirm the host resolves and port 443 egress is open "
                       "for websocket upgrades, not just plain HTTPS.",
                url=url,
            )

        if raw is None:
            # Connected and subscribed, then silence. The spec promises an
            # immediate snapshot per subscribed asset, so a quiet market and a
            # broken feed genuinely look the same from here.
            ctx.facts.set(Fact.WS_CONNECTED, True)
            return Finding.warn(
                f"connected, but no frame within {self.first_message_timeout:.0f}s",
                detail="Handshake and subscribe both worked. Silence after that "
                       "is either a market with an empty book or a feed that "
                       "took the subscription and won't deliver — "
                       "indistinguishable from this side.",
                remedy="Re-run against the highest-volume open market "
                       "(gamma-api /markets?order=volumeNum). If that one is "
                       "silent too, the feed is the problem, not the market.",
                connect_ms=connect_ms,
                url=url,
            )

        ctx.facts.set(Fact.WS_CONNECTED, True)
        event = _first_event(raw)
        event_type = event.get("event_type") if event else None

        evidence: dict[str, Any] = {
            "connect_ms": connect_ms,
            "first_message_ms": first_ms,
            "event_type": event_type,
        }
        if event_type == "book":
            evidence["bid_levels"] = len(event.get("bids") or [])
            evidence["ask_levels"] = len(event.get("asks") or [])

        caveat = issues.WEBSOCKET_STREAM_STOPS
        label = f" ({event_type})" if event_type else ""
        return Finding.ok(
            f"first frame in {first_ms:.0f}ms{label}",
            detail="One frame proves the path works today, not that it stays "
                   f"up: this stream is known to stop silently mid-session "
                   f"({caveat.slug}, {caveat.comments} comments). The socket "
                   "stays open while the data dies, so connection liveness "
                   "catches nothing. Track the last-frame timestamp, treat "
                   "sustained silence on an active market as a dead stream, "
                   "and reconnect + resubscribe. Send the PING the spec asks "
                   "for every 10s, but don't mistake a PONG for fresh data.",
            **evidence,
        )

    async def _subscribe_once(
        self, url: str, token_id: str
    ) -> tuple[float, float | None, str | bytes | None]:
        """Returns (connect_ms, first_message_ms, raw frame).

        A recv timeout returns (connect_ms, None, None) rather than raising, so
        the caller can tell "never connected" from "connected but silent" —
        those are a FAIL and a WARN respectively.
        """
        started = time.monotonic()
        async with websockets.connect(url, open_timeout=self.connect_timeout) as ws:
            connect_ms = round((time.monotonic() - started) * 1000, 1)
            await ws.send(json.dumps({"assets_ids": [token_id], "type": "market"}))
            subscribed = time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), self.first_message_timeout)
            except TimeoutError:
                return connect_ms, None, None
            return connect_ms, round((time.monotonic() - subscribed) * 1000, 1), raw


def _first_event(raw: str | bytes) -> dict[str, Any] | None:
    """First event object out of a frame.

    Live, the feed wraps events in a JSON array; the spec's examples show bare
    objects. Accept both — and anything else (a stray PONG, say) is simply a
    frame we can't classify, which is still a delivering feed.
    """
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else None
    return parsed if isinstance(parsed, dict) else None


CHECKS = (MarketFeed(),)
