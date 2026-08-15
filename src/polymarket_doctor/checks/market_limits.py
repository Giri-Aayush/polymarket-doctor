"""Stage 4 — the per-market numbers an order is validated against.

Price on a tick boundary, size above the minimum, the neg-risk flag that
changes which settlement contract the order routes through — all of it is
per-token, none of it is an SDK default, and getting any of it wrong produces
a rejection that reads like a signing problem. This stage resolves a token to
test against and reads its limits so stage 5 can build an order that the
exchange would actually accept.

Response shapes re-verified against production on 2026-08-15:

- GET {gamma}/markets?closed=false&limit=1&order=volumeNum&ascending=false
  returns a list of market objects. `clobTokenIds` is a JSON-encoded *string*
  containing a list, not a list — a client that indexes it directly gets
  characters, not token ids. `orderMinSize` was 5 on every market sampled;
  `orderPriceMinTickSize` is 0.001 or 0.01; `negRisk` is a bool.
- GET {clob}/tick-size?token_id=T  → {"minimum_tick_size": 0.001}
- GET {clob}/neg-risk?token_id=T   → {"neg_risk": true}
- GET {clob}/fee-rate?token_id=T   → {"base_fee": 0}

On fee-rate: py-clob-client-v2#107 reports the endpoint answering base_fee
1000 for every fee-bearing market regardless of the market's actual rate.
Unconfirmed here, so it's noted rather than cited in a finding — but treat a
non-zero reading as an upper bound, not gospel.
"""

from __future__ import annotations

import json
from typing import Any

from .. import issues
from ..core.check import Check, Finding, Severity, Stage
from ..core.context import Context
from ..core.facts import Fact
from ..net.http import Response

# What gamma reported for every open market sampled on 2026-08-15. Used only
# when the user supplies a token id, because then gamma is never consulted and
# there is nothing to read orderMinSize from.
DEFAULT_MIN_ORDER_SIZE = 5

# Highest-volume open market first. Volume is the selector because the point
# of the discovered token is that later stages can assume a live book; a
# freshly listed or near-resolution market can't promise that.
DISCOVERY_PARAMS = {
    "closed": "false",
    "limit": 1,
    "order": "volumeNum",
    "ascending": "false",
}

# The threshold below which the taker-amount decimal count changes. See
# MarketLimits for why that matters.
FINE_TICK = 0.001


class ResolveToken(Check):
    id = "market.resolve-token"
    stage = Stage.MARKET_LIMITS
    title = "A token to run market checks against"
    reads = frozenset({Fact.HOST})
    writes = frozenset({Fact.TOKEN_ID, Fact.MIN_ORDER_SIZE})

    def run(self, ctx: Context) -> Finding:
        if ctx.token_id:
            ctx.facts.set(Fact.TOKEN_ID, ctx.token_id)
            ctx.facts.set(Fact.MIN_ORDER_SIZE, DEFAULT_MIN_ORDER_SIZE)
            return Finding.ok(
                f"using supplied token {_short(ctx.token_id)}",
                detail=f"Gamma isn't consulted for a supplied token, so the minimum "
                       f"order size is assumed to be {DEFAULT_MIN_ORDER_SIZE} shares — "
                       f"the value every sampled market reports.",
                token_id=ctx.token_id,
                min_order_size=DEFAULT_MIN_ORDER_SIZE,
            )

        response = ctx.probe.get(f"{ctx.endpoints.gamma}/markets", params=DISCOVERY_PARAMS)
        market = _first_market(response)
        if market is None:
            return self._fail_unresolved(
                ctx,
                "no market to test against",
                detail=response.error or f"gamma answered {response.status} with "
                                         f"{response.body!r}",
            )

        token = _first_token_id(market)
        if token is None:
            return self._fail_unresolved(
                ctx,
                "gamma's top market has no usable token id",
                detail=f"clobTokenIds was {market.get('clobTokenIds')!r} — expected a "
                       f"JSON-encoded list of ids.",
            )

        min_size = market.get("orderMinSize")
        if isinstance(min_size, bool) or not isinstance(min_size, (int, float)):
            min_size = DEFAULT_MIN_ORDER_SIZE

        slug = market.get("slug") or "<no slug>"
        ctx.facts.set(Fact.TOKEN_ID, token)
        ctx.facts.set(Fact.MIN_ORDER_SIZE, min_size)
        return Finding.ok(
            f"{slug} · token {_short(token)}",
            detail=f"Highest-volume open market, chosen so later stages see a live "
                   f"book. Minimum order size {min_size} shares.",
            slug=slug,
            token_id=token,
            min_order_size=min_size,
        )

    @staticmethod
    def _fail_unresolved(ctx: Context, summary: str, detail: str) -> Finding:
        # Facts still get written so the store shows "tried and got nothing"
        # rather than looking like this check never ran.
        ctx.facts.set(Fact.TOKEN_ID, None)
        ctx.facts.set(Fact.MIN_ORDER_SIZE, None)
        return Finding.fail(
            summary,
            detail=detail,
            remedy="Pass --token with a CLOB token id and the market checks run "
                   "without gamma.",
        )


class MarketLimits(Check):
    """Read the three per-token parameters an order gets validated against."""

    id = "market.limits"
    stage = Stage.MARKET_LIMITS
    title = "Tick size, neg-risk flag, and fee rate"
    reads = frozenset({Fact.TOKEN_ID})
    writes = frozenset({Fact.TICK_SIZE, Fact.NEG_RISK, Fact.FEE_RATE_BPS})

    def run(self, ctx: Context) -> Finding:
        token = ctx.facts.get(Fact.TOKEN_ID)
        if token is None:
            for fact in self.writes:
                ctx.facts.set(fact, None)
            return Finding.fail(
                "no token to read limits for",
                detail="Token resolution came up empty, so there is nothing to ask "
                       "the CLOB about.",
                remedy="Fix market.resolve-token first, or pass --token.",
            )

        params = {"token_id": token}
        tick_response = ctx.probe.get(ctx.endpoints.clob_url("/tick-size"), params=params)
        neg_response = ctx.probe.get(ctx.endpoints.clob_url("/neg-risk"), params=params)
        fee_response = ctx.probe.get(ctx.endpoints.clob_url("/fee-rate"), params=params)

        tick = _number(tick_response, "minimum_tick_size")
        neg_risk = _flag(neg_response, "neg_risk")
        fee = _number(fee_response, "base_fee")
        fee_bps = int(fee) if fee is not None else None

        ctx.facts.set(Fact.TICK_SIZE, tick)
        ctx.facts.set(Fact.NEG_RISK, neg_risk)
        ctx.facts.set(Fact.FEE_RATE_BPS, fee_bps)

        if tick is None:
            return Finding.fail(
                "could not read the tick size",
                detail=tick_response.error or f"HTTP {tick_response.status} from "
                                              f"/tick-size",
                remedy="Every order price is validated against the tick, so nothing "
                       "later can be priced without it. Check the token id is a "
                       "CLOB token id, not a market slug or condition id.",
                token_id=token,
            )

        summary = " · ".join((
            f"tick {tick}",
            "neg-risk" if neg_risk else
            "no neg-risk" if neg_risk is False else "neg-risk unknown",
            f"fee {fee_bps}bps" if fee_bps is not None else "fee unknown",
        ))
        evidence = {
            "token_id": token,
            "tick_size": tick,
            "neg_risk": neg_risk,
            "fee_rate_bps": fee_bps,
        }

        if neg_risk is None or fee_bps is None:
            missing = [name for name, response in (("neg-risk", neg_response),
                                                   ("fee-rate", fee_response))
                       if not response.ok]
            return Finding.warn(
                summary,
                detail=f"{' and '.join(missing)} did not answer. Neg-risk decides "
                       f"which exchange contract the order targets, so guessing it "
                       f"means signing against the wrong verifying contract.",
                remedy="Re-run before going live; both endpoints normally answer.",
                **evidence,
            )

        if tick <= FINE_TICK:
            # An issue= on a PASS is unusual but earned here: the market is fine
            # today and the trap only springs when a market order gets signed.
            return Finding(
                Severity.PASS,
                summary,
                detail="Prices must land on the tick and the taker amount must be "
                       "computed with the tick's decimal count. On a 0.001-tick "
                       "book that is one more decimal than the coarse default, and "
                       "an amount rounded to 5 decimals as if the tick were 0.01 "
                       "is rejected on every market order.",
                issue=issues.FINE_TICK_REJECTED,
                evidence=evidence,
            )

        return Finding.ok(
            summary,
            detail="Coarse tick today, but ticks tighten to 0.001 as the price "
                   "nears the bounds — recompute the taker amount's decimal count "
                   "from the tick on every order rather than hardcoding it.",
            **evidence,
        )


def _first_market(response: Response) -> dict[str, Any] | None:
    if not response.ok or not isinstance(response.body, list) or not response.body:
        return None
    market = response.body[0]
    return market if isinstance(market, dict) else None


def _first_token_id(market: dict[str, Any]) -> str | None:
    """First entry of `clobTokenIds`, tolerating both encodings.

    Gamma double-encodes the field today (a JSON string holding a list). Parsed
    here rather than upstream so that if gamma ever fixes the encoding, a plain
    list keeps working.
    """
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, list):
        return None
    for entry in raw:
        if isinstance(entry, str) and entry:
            return entry
    return None


def _number(response: Response, key: str) -> float | int | None:
    if not response.ok or not isinstance(response.body, dict):
        return None
    value = response.body.get(key)
    # bool subclasses int, and True where a tick size should be is a bug worth
    # surfacing as "unknown" rather than a nonsense number.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _flag(response: Response, key: str) -> bool | None:
    if not response.ok or not isinstance(response.body, dict):
        return None
    value = response.body.get(key)
    return value if isinstance(value, bool) else None


def _short(token_id: str) -> str:
    return token_id if len(token_id) <= 12 else f"{token_id[:6]}…{token_id[-4:]}"


CHECKS = (ResolveToken(), MarketLimits())
