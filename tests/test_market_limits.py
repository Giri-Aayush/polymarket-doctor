from __future__ import annotations

import json

from conftest import StubProbe, http_error, ok, unreachable

from polymarket_doctor import issues
from polymarket_doctor.checks.market_limits import (
    DEFAULT_MIN_ORDER_SIZE,
    MarketLimits,
    ResolveToken,
)
from polymarket_doctor.core.check import Severity
from polymarket_doctor.core.facts import Fact

TOKEN = "27146956652877944551877724690365745048289675287536243265951843487691050802191"
OTHER_TOKEN = "33216695217861742195941369663873573949679634432452142092545486849801915283392"


def gamma_market(**overrides) -> dict:
    # clobTokenIds is a JSON-encoded string, exactly as gamma serves it. The
    # discovery test exists to prove the check json.loads it rather than
    # indexing into the string.
    market = {
        "slug": "will-it-rain-in-lisbon",
        "clobTokenIds": json.dumps([TOKEN, OTHER_TOKEN]),
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.001,
        "negRisk": True,
    }
    market.update(overrides)
    return market


class TestResolveToken:
    def test_discovers_the_top_market_from_gamma(self, make_context):
        ctx = make_context(StubProbe({"gamma-api": ok([gamma_market()])}))
        finding = ResolveToken().run(ctx)

        assert finding.severity is Severity.PASS
        assert "will-it-rain-in-lisbon" in finding.summary
        # The double-encoded string must come apart into real ids — the first
        # character of the raw string is '[', not a token.
        assert ctx.facts.get(Fact.TOKEN_ID) == TOKEN
        assert ctx.facts.get(Fact.MIN_ORDER_SIZE) == 5

    def test_supplied_token_skips_gamma_entirely(self, make_context):
        # An empty StubProbe raises on any request, so passing means zero calls.
        probe = StubProbe()
        ctx = make_context(probe, token_id=TOKEN)
        finding = ResolveToken().run(ctx)

        assert finding.severity is Severity.PASS
        assert probe.calls == []
        assert ctx.facts.get(Fact.TOKEN_ID) == TOKEN
        assert ctx.facts.get(Fact.MIN_ORDER_SIZE) == DEFAULT_MIN_ORDER_SIZE
        assert str(DEFAULT_MIN_ORDER_SIZE) in finding.detail

    def test_gamma_down_with_a_token_still_passes(self, make_context):
        ctx = make_context(StubProbe({"gamma-api": unreachable()}), token_id=TOKEN)
        assert ResolveToken().run(ctx).severity is Severity.PASS

    def test_gamma_down_without_a_token_fails(self, make_context):
        ctx = make_context(StubProbe({"gamma-api": unreachable()}))
        finding = ResolveToken().run(ctx)

        assert finding.severity is Severity.FAIL
        assert "--token" in finding.remedy
        # Declared facts are written even on failure, as explicit unknowns.
        assert Fact.TOKEN_ID in ctx.facts
        assert ctx.facts.get(Fact.TOKEN_ID) is None
        assert ctx.facts.get(Fact.MIN_ORDER_SIZE) is None

    def test_empty_market_list_fails(self, make_context):
        ctx = make_context(StubProbe({"gamma-api": ok([])}))
        assert ResolveToken().run(ctx).severity is Severity.FAIL

    def test_unparseable_token_ids_fail_rather_than_crash(self, make_context):
        market = gamma_market(clobTokenIds="not json at all")
        ctx = make_context(StubProbe({"gamma-api": ok([market])}))
        finding = ResolveToken().run(ctx)

        assert finding.severity is Severity.FAIL
        assert ctx.facts.get(Fact.TOKEN_ID) is None


class TestMarketLimits:
    def run_with(self, make_context, routes, token=TOKEN):
        ctx = make_context(StubProbe(routes))
        if token is not None:
            ctx.facts.set(Fact.TOKEN_ID, token)
        return MarketLimits().run(ctx), ctx

    def test_happy_path_writes_all_three_facts(self, make_context):
        finding, ctx = self.run_with(make_context, {
            "/tick-size": ok({"minimum_tick_size": 0.01}),
            "/neg-risk": ok({"neg_risk": False}),
            "/fee-rate": ok({"base_fee": 0}),
        })

        assert finding.severity is Severity.PASS
        assert finding.summary == "tick 0.01 · no neg-risk · fee 0bps"
        assert ctx.facts.get(Fact.TICK_SIZE) == 0.01
        assert ctx.facts.get(Fact.NEG_RISK) is False
        assert ctx.facts.get(Fact.FEE_RATE_BPS) == 0

    def test_fine_tick_cites_the_taker_decimals_issue(self, make_context):
        finding, _ = self.run_with(make_context, {
            "/tick-size": ok({"minimum_tick_size": 0.001}),
            "/neg-risk": ok({"neg_risk": True}),
            "/fee-rate": ok({"base_fee": 0}),
        })

        assert finding.severity is Severity.PASS
        assert finding.summary == "tick 0.001 · neg-risk · fee 0bps"
        assert finding.issue is issues.FINE_TICK_REJECTED
        assert "decimal" in finding.detail

    def test_tick_size_unreachable_fails(self, make_context):
        finding, ctx = self.run_with(make_context, {
            "/tick-size": unreachable(),
            "/neg-risk": ok({"neg_risk": True}),
            "/fee-rate": ok({"base_fee": 0}),
        })

        assert finding.severity is Severity.FAIL
        assert ctx.facts.get(Fact.TICK_SIZE) is None
        # The other reads still landed; a partial answer beats none.
        assert ctx.facts.get(Fact.NEG_RISK) is True
        assert ctx.facts.get(Fact.FEE_RATE_BPS) == 0

    def test_secondary_endpoint_failure_warns_but_keeps_the_tick(self, make_context):
        finding, ctx = self.run_with(make_context, {
            "/tick-size": ok({"minimum_tick_size": 0.01}),
            "/neg-risk": http_error(500),
            "/fee-rate": ok({"base_fee": 0}),
        })

        assert finding.severity is Severity.WARN
        assert "neg-risk" in finding.detail
        assert ctx.facts.get(Fact.TICK_SIZE) == 0.01
        assert ctx.facts.get(Fact.NEG_RISK) is None

    def test_no_token_fails_and_still_writes_unknowns(self, make_context):
        finding, ctx = self.run_with(make_context, {}, token=None)

        assert finding.severity is Severity.FAIL
        for fact in (Fact.TICK_SIZE, Fact.NEG_RISK, Fact.FEE_RATE_BPS):
            assert fact in ctx.facts
            assert ctx.facts.get(fact) is None


class TestReadOnly:
    def test_the_whole_stage_never_posts(self, make_context):
        # Stage 4 runs against production markets before the partner has traded
        # anything, so a POST here would be a live mutation. GET-only is a
        # contract, not an accident.
        probe = StubProbe({
            "gamma-api": ok([gamma_market()]),
            "/tick-size": ok({"minimum_tick_size": 0.001}),
            "/neg-risk": ok({"neg_risk": True}),
            "/fee-rate": ok({"base_fee": 0}),
        })
        ctx = make_context(probe)
        ResolveToken().run(ctx)
        MarketLimits().run(ctx)

        assert probe.calls, "expected the stage to make requests"
        assert all(method == "GET" for method, _ in probe.calls)
