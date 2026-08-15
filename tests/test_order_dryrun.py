"""Stage 5's dry run.

Two things are on trial here: the arithmetic (Decimal end to end, because the
SDK trackers show what binary floats do to maker and taker amounts) and the
README promise (the whole stage is GETs — nothing signed, nothing POSTed).
"""

from __future__ import annotations

from decimal import Decimal

from conftest import DEPOSIT_WALLET, StubProbe, ok

from polymarket_doctor import issues
from polymarket_doctor.checks.order_dryrun import (
    OrderDryRun,
    appraise,
    build_candidate,
    evaluate,
)
from polymarket_doctor.core.check import Severity
from polymarket_doctor.core.facts import Fact

# The fine-tick market used to verify the live /book shape on 2026-08-15.
TOKEN_ID = "27146956652877944551877724690365745048289675287536243265951843487691050802191"


def book(bids):
    """Production shape: string prices and sizes, millisecond timestamp."""
    return ok({
        "bids": [{"price": price, "size": size} for price, size in bids],
        "asks": [{"price": "0.999", "size": "100201.11"}],
        "timestamp": "1786811073899",
    })


def context_for(make_context, *, bids=(), tick="0.01", min_size=5, omit=()):
    probe = StubProbe({"/book": book(bids)})
    ctx = make_context(probe)
    values = {
        Fact.TOKEN_ID: TOKEN_ID,
        Fact.TICK_SIZE: tick,
        Fact.MIN_ORDER_SIZE: min_size,
        Fact.NEG_RISK: False,
        Fact.FUNDER_ADDRESS: DEPOSIT_WALLET,
        Fact.SIGNATURE_TYPE: 2,
    }
    for fact, value in values.items():
        if fact not in omit:
            ctx.facts.set(fact, value)
    return ctx, probe


def candidate_on(tick, *, price, size):
    return build_candidate(
        token_id=TOKEN_ID,
        funder=DEPOSIT_WALLET,
        tick=Decimal(tick),
        price=Decimal(price),
        size=Decimal(size),
        neg_risk=False,
        signature_type=2,
    )


class TestHappyPath:
    def test_coarse_tick_joins_the_best_bid(self, make_context):
        # Bids ascending, exactly as production returns them: the best bid is
        # the maximum, and joining it must not cross.
        ctx, _ = context_for(make_context, bids=(("0.55", "100"), ("0.57", "20")))

        finding = OrderDryRun().run(ctx)

        assert finding.severity is Severity.PASS
        assert ctx.facts.get(Fact.ORDER_PAYLOAD_VALID) is True
        assert finding.evidence["price"] == "0.57"
        assert finding.evidence["size"] == "5"
        assert finding.evidence["maker_amount"] == "2850000"
        assert finding.evidence["taker_amount"] == "5000000"

    def test_fine_tick_market(self, make_context):
        ctx, _ = context_for(
            make_context, tick="0.001", min_size=15,
            bids=(("0.001", "18195.33"), ("0.123", "50")),
        )

        finding = OrderDryRun().run(ctx)

        assert finding.severity is Severity.PASS
        assert finding.evidence["price"] == "0.123"
        assert finding.evidence["maker_amount"] == "1845000"
        assert finding.evidence["taker_amount"] == "15000000"

    def test_detail_states_nothing_was_signed_or_sent(self, make_context):
        ctx, _ = context_for(make_context, bids=(("0.57", "20"),))

        detail = OrderDryRun().run(ctx).detail

        assert "signed" in detail
        assert "sent" in detail

    def test_decimal_survives_what_binary_floats_get_wrong(self, make_context):
        # 5.1 × 0.57 is exactly 2.907. The float path lands one base unit
        # short — the sub-cent maker the server rejects (#323 / v2#68).
        assert int(5.1 * 0.57 * 1_000_000) == 2906999

        ctx, _ = context_for(make_context, min_size=5.1, bids=(("0.57", "20"),))
        finding = OrderDryRun().run(ctx)

        assert finding.severity is Severity.PASS
        assert finding.evidence["maker_amount"] == "2907000"
        assert finding.evidence["taker_amount"] == "5100000"

    def test_empty_book_prices_one_tick_above_minimum(self, make_context):
        ctx, _ = context_for(make_context, bids=())

        finding = OrderDryRun().run(ctx)

        assert finding.severity is Severity.PASS
        assert finding.evidence["price"] == "0.02"
        assert finding.evidence["priced_from"] == "empty book fallback"


class TestSkips:
    def test_skips_without_a_token_id(self, make_context):
        ctx, probe = context_for(make_context, omit=(Fact.TOKEN_ID,))

        finding = OrderDryRun().run(ctx)

        assert finding.severity is Severity.SKIP
        assert Fact.ORDER_PAYLOAD_VALID in ctx.facts
        assert ctx.facts.get(Fact.ORDER_PAYLOAD_VALID) is None
        assert probe.calls == []

    def test_skips_without_a_tick_size(self, make_context):
        ctx, probe = context_for(make_context, omit=(Fact.TICK_SIZE,))

        assert OrderDryRun().run(ctx).severity is Severity.SKIP
        assert probe.calls == []


class TestInvariants:
    def test_off_grid_price_fails_naming_the_invariant(self):
        # What a round-to-nearest client actually produces on a 0.01 market.
        candidate = candidate_on("0.01", price="0.57", size="5")
        candidate["price"] = "0.5715"

        finding = appraise(candidate, tick=Decimal("0.01"), min_size=Decimal(5))

        assert finding.severity is Severity.FAIL
        assert "tick grid" in finding.summary
        assert "py-clob-client-v2#68" in finding.detail
        assert "sent" in finding.detail  # the promise holds on failures too

    def test_fine_tick_taker_mismatch_cites_the_open_issue(self):
        candidate = candidate_on("0.001", price="0.123", size="5")
        candidate["takerAmount"] = "4999990"  # a taker signed at 5 decimals

        finding = appraise(candidate, tick=Decimal("0.001"), min_size=Decimal(5))

        assert finding.severity is Severity.FAIL
        assert finding.issue is issues.FINE_TICK_REJECTED

    def test_size_below_the_five_token_floor(self):
        candidate = candidate_on("0.01", price="0.57", size="2")

        violations = evaluate(candidate, tick=Decimal("0.01"), min_size=Decimal(5))

        assert any("floor" in violation.invariant for violation in violations)

    def test_sub_tick_bid_snaps_to_zero_and_fails(self, make_context):
        # A malformed book bidding below one tick snaps down to zero. The range
        # invariant catches it rather than the builder papering over it.
        ctx, _ = context_for(make_context, bids=(("0.005", "10"),))

        finding = OrderDryRun().run(ctx)

        assert finding.severity is Severity.FAIL
        assert ctx.facts.get(Fact.ORDER_PAYLOAD_VALID) is False
        assert "(0, 1)" in finding.summary


class TestReadOnly:
    def test_the_entire_stage_is_gets(self, make_context):
        ctx, probe = context_for(make_context, bids=(("0.57", "20"),))

        OrderDryRun().run(ctx)

        assert all(method == "GET" for method, _ in probe.calls)
        assert len(probe.calls) == 1
        assert "/book" in probe.calls[0][1]
