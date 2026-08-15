"""Stage 5 — build the exact order a client would send, then don't send it.

The README promise is that this tool never asks for a private key and never
places, cancels, or modifies an order. This stage keeps that promise while
still exercising the step where integrations actually break: payload
construction. It prices a BUY candidate off the live book (GETs only), builds
the amounts the way a client would, and runs the invariants the server
enforces — locally. Nothing is signed and nothing is POSTed.

Ground truth, verified against production on 2026-08-15:

- The exchange minimum is 5 outcome tokens per order regardless of notional,
  and prices must sit exactly on the market's tick grid (0.01 or 0.001).
- pUSD/USDC and outcome tokens carry 6 decimals; makerAmount and takerAmount
  travel as integer base units. Everything here is Decimal end to end because
  the SDK trackers are a catalogue of what binary floats do to these two
  fields: round_normal vs round_down disagreeing in the last base unit and
  leaving a sub-cent maker amount the server rejects (py-clob-client#323,
  py-clob-client-v2#68); accumulated float drift (py-clob-client-v2#66); and
  taker amounts computed at 5 decimals on 0.001-tick markets, rejected on
  every order (py-clob-client-v2#99 — issues.FINE_TICK_REJECTED).
- GET /book?token_id=T returns prices and sizes as strings and a millisecond
  "timestamp". Bids arrive ascending by price, so the best bid is the maximum
  entry, not the first one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from .. import issues
from ..core.check import Check, Finding, Severity, Stage
from ..core.context import Context
from ..core.facts import Fact

BOOK_PATH = "/book"

# Both collateral and outcome tokens are 6-decimal; wire amounts are integers
# of these units.
MICRO = Decimal(10) ** 6

# The exchange floor: 5 tokens per order no matter how small the notional is.
MIN_MARKETABLE_TOKENS = Decimal(5)

# Sizes are accepted in hundredths of a token. Two size decimals times three
# price decimals is five, which is why every product below is exact at six.
SIZE_STEP = Decimal("0.01")

NOTHING_SENT = (
    "Nothing was signed and nothing was sent: no key is involved, the candidate "
    "was built and judged locally, and the only request this stage makes is a "
    "GET of the order book."
)


@dataclass(frozen=True, slots=True)
class Violation:
    """One invariant the candidate broke, tied to the bug that breaks it in the wild."""

    invariant: str
    problem: str
    caused_by: str | None = None
    remedy: str | None = None
    issue: issues.KnownIssue | None = None


def build_candidate(*, token_id: str, funder: str | None, tick: Decimal, price: Decimal,
                    size: Decimal, neg_risk: bool, signature_type: int | None) -> dict:
    """The order dict a client would sign, minus salt and signature — deliberately.

    Snapping the price down (never to nearest) is the point: rounding up can
    cross the book or walk off the grid, and round-to-nearest is exactly the
    round_normal behaviour behind the sub-cent maker bug.
    """
    snapped = (price // tick) * tick
    sized = size.quantize(SIZE_STEP, rounding=ROUND_DOWN)
    # snapped has at most 3 decimals and sized at most 2, so both products are
    # exact at 6 decimals — int() truncates nothing here.
    maker_units = int(sized * snapped * MICRO)
    taker_units = int(sized * MICRO)
    return {
        "maker": funder,
        "side": "BUY",
        "tokenId": token_id,
        "price": _plain(snapped),
        "size": _plain(sized),
        "makerAmount": str(maker_units),
        "takerAmount": str(taker_units),
        "signatureType": int(signature_type) if signature_type is not None else None,
        "negRisk": neg_risk,
    }


def evaluate(candidate: dict, *, tick: Decimal, min_size: Decimal) -> tuple[Violation, ...]:
    """Run the server's order invariants against a candidate, locally.

    Order matters: the first violation names the FAIL, so the list starts with
    the invariants whose breakage explains the ones after them.
    """
    price = Decimal(candidate["price"])
    size = Decimal(candidate["size"])
    maker = int(candidate["makerAmount"])
    taker = int(candidate["takerAmount"])
    grid_decimals = -tick.normalize().as_tuple().exponent
    fine_tick = grid_decimals >= 3

    found: list[Violation] = []

    if not Decimal(0) < price < Decimal(1):
        found.append(Violation(
            "price inside (0, 1)",
            f"price {price} — outcome prices are probabilities and the exchange "
            "rejects anything at or beyond either bound",
            remedy="Price the candidate strictly between one tick and 1 minus one tick.",
        ))

    if price % tick != 0:
        found.append(Violation(
            "price on the tick grid",
            f"price {price} is not a multiple of the {_plain(tick)} tick",
            caused_by="In the wild this is float rounding: round_normal and "
                      "round_down disagree in the last unit (py-clob-client#323, "
                      "py-clob-client-v2#68), and accumulated float error walks "
                      "prices off the grid entirely (py-clob-client-v2#66).",
            remedy="Quantize prices with Decimal against the tick the CLOB "
                   "reports for this market, rounding down.",
        ))

    price_decimals = -price.normalize().as_tuple().exponent
    if price_decimals > grid_decimals:
        found.append(Violation(
            "price decimals match the tick regime",
            f"price {price} carries {price_decimals} decimals where a "
            f"{_plain(tick)}-tick market allows {grid_decimals}",
        ))

    if size < min_size:
        found.append(Violation(
            "size at or above the exchange floor",
            f"size {_plain(size)} is below the minimum of {_plain(min_size)} "
            "tokens (5 regardless of notional, higher where the market says so)",
            remedy="Order at least the minimum token count; notional doesn't matter.",
        ))

    exact_maker = size * price * MICRO
    if exact_maker != exact_maker.to_integral_value():
        found.append(Violation(
            "maker amount lands on whole base units",
            f"size × price = {size * price} leaves a remainder below one base "
            "unit at 6 decimals, so no integer makerAmount is exact",
            caused_by="This is the sub-cent maker the server rejects "
                      "(py-clob-client#323, py-clob-client-v2#68).",
        ))
    elif maker != exact_maker:
        found.append(Violation(
            "maker amount equals size × price exactly",
            f"makerAmount {maker}, but {_plain(size)} × {_plain(price)} is "
            f"{int(exact_maker)} base units",
            caused_by="The round_normal vs round_down discrepancy produces "
                      "exactly this off-by-one-unit maker (py-clob-client#323, "
                      "py-clob-client-v2#68); float accumulation drift shows the "
                      "same signature (py-clob-client-v2#66).",
            remedy="Compute amounts with Decimal and round the maker amount down, once.",
        ))

    exact_taker = size * MICRO
    if taker != exact_taker:
        found.append(Violation(
            "taker amount equals size exactly",
            f"takerAmount {taker}, but size {_plain(size)} is "
            f"{int(exact_taker)} base units",
            caused_by=(
                "On 0.001-tick markets this is the 5-decimal taker: amounts "
                "signed at 5 decimals instead of 6 are rejected on every order "
                "(py-clob-client-v2#99)."
                if fine_tick
                else "Float accumulation drift in size handling (py-clob-client-v2#66)."
            ),
            remedy="Compute the taker amount from size with Decimal at all 6 decimals.",
            issue=issues.FINE_TICK_REJECTED if fine_tick else None,
        ))

    return tuple(found)


def appraise(candidate: dict, *, tick: Decimal, min_size: Decimal,
             **extra_evidence: object) -> Finding:
    """Turn a candidate into a Finding. Pure — no facts written, no network."""
    violations = evaluate(candidate, tick=tick, min_size=min_size)

    size_s, price_s = candidate["size"], candidate["price"]
    maker_s, taker_s = candidate["makerAmount"], candidate["takerAmount"]
    evidence = {
        "side": candidate["side"],
        "price": price_s,
        "size": size_s,
        "maker_amount": maker_s,
        "taker_amount": taker_s,
        "signature_type": candidate["signatureType"],
        "neg_risk": candidate["negRisk"],
        "tick_size": _plain(tick),
        **extra_evidence,
    }

    if not violations:
        return Finding.ok(
            f"BUY {size_s} @ {price_s} builds cleanly: "
            f"maker {maker_s} / taker {taker_s} base units",
            detail="This is the payload a client would sign, amounts computed "
                   "with Decimal at all 6 decimals and the price joined to the "
                   "book rather than crossing it. " + NOTHING_SENT,
            **evidence,
        )

    worst = violations[0]
    lines = []
    for violation in violations:
        line = f"- {violation.invariant}: {violation.problem}."
        if violation.caused_by:
            line += f" {violation.caused_by}"
        lines.append(line)
    lines.append(NOTHING_SENT)
    return Finding.fail(
        f"candidate order breaks the '{worst.invariant}' invariant",
        detail="\n".join(lines),
        remedy=worst.remedy,
        issue=next((v.issue for v in violations if v.issue is not None), None),
        **evidence,
    )


class OrderDryRun(Check):
    id = "order.dry-run"
    stage = Stage.ORDER_DRY_RUN
    title = "A realistic order payload survives the server's invariants"
    reads = frozenset({
        Fact.TOKEN_ID,
        Fact.TICK_SIZE,
        Fact.NEG_RISK,
        Fact.MIN_ORDER_SIZE,
        Fact.FUNDER_ADDRESS,
        Fact.SIGNATURE_TYPE,
    })
    writes = frozenset({Fact.ORDER_PAYLOAD_VALID})

    def run(self, ctx: Context) -> Finding:
        token_id = ctx.facts.get(Fact.TOKEN_ID)
        tick_fact = ctx.facts.get(Fact.TICK_SIZE)
        if token_id is None or tick_fact is None:
            ctx.facts.set(Fact.ORDER_PAYLOAD_VALID, None)
            return Finding(
                Severity.SKIP,
                "skipped, no market to price against",
                detail="Needs the token id and tick size from stage 4; building "
                       "a candidate without them would be guessing at the grid.",
            )

        tick = Decimal(str(tick_fact))
        min_size = _floor_size(ctx.facts.get(Fact.MIN_ORDER_SIZE))

        response = ctx.probe.get(
            ctx.endpoints.clob_url(BOOK_PATH), params={"token_id": token_id}
        )
        # A dead or erroring book degrades to the fallback price rather than
        # failing: this stage judges payload construction, and whether the
        # market answers at all was stage 4's verdict to give.
        best_bid = _best_bid(response.body) if response.ok else None

        if best_bid is not None:
            # Join the best bid instead of crossing the spread: marketable
            # enough to be a realistic payload, passive enough that even a
            # signed-and-sent version of it would take no liquidity.
            price, priced_from = best_bid, "best bid"
        else:
            # One tick above the smallest representable price: on-grid, inside
            # (0, 1), and as far from crossing anything as a price can be.
            price, priced_from = tick * 2, "empty book fallback"

        candidate = build_candidate(
            token_id=token_id,
            funder=ctx.facts.get(Fact.FUNDER_ADDRESS),
            tick=tick,
            price=price,
            size=min_size,
            neg_risk=bool(ctx.facts.get(Fact.NEG_RISK)),
            signature_type=ctx.facts.get(Fact.SIGNATURE_TYPE),
        )
        finding = appraise(candidate, tick=tick, min_size=min_size, priced_from=priced_from)
        ctx.facts.set(Fact.ORDER_PAYLOAD_VALID, finding.severity is Severity.PASS)
        return finding


def _floor_size(min_order_size: object) -> Decimal:
    """The larger of the exchange-wide floor and whatever the market demands."""
    if min_order_size is None:
        return MIN_MARKETABLE_TOKENS
    return max(MIN_MARKETABLE_TOKENS, Decimal(str(min_order_size)))


def _best_bid(body: object) -> Decimal | None:
    """Highest bid on the book, or None when there's nothing to join.

    Takes the maximum rather than the first entry: production returns bids
    ascending by price, and nothing in the API contract promises an order.
    """
    if not isinstance(body, dict):
        return None
    bids = body.get("bids")
    if not isinstance(bids, list):
        return None
    prices = []
    for entry in bids:
        if not isinstance(entry, dict):
            continue
        try:
            price = Decimal(str(entry.get("price")))
        except InvalidOperation:
            continue
        if price > 0:
            prices.append(price)
    return max(prices, default=None)


def _plain(value: Decimal) -> str:
    # normalize() alone renders 15.00 as 1.5E+1; format 'f' keeps it plain.
    return format(value.normalize(), "f")


CHECKS = (OrderDryRun(),)
