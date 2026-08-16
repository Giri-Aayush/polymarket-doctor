from __future__ import annotations

from conftest import StubProbe, http_error, ok, unreachable

from polymarket_doctor.checks.rfq import RfqGateway
from polymarket_doctor.core.check import Severity
from polymarket_doctor.core.facts import Fact

# Shaped like the live 2026-08-15 response, trimmed to the fields the check
# could plausibly care about.
COMBO_MARKET = {
    "id": "665374",
    "condition_id": "0x5db999fad322cea2914535aae5517060c3f80ad6d8c0231cde2124a434d16846",
    "position_ids": ["7985599...4064", "7985599...4065"],
    "pending": True,
}


def assert_read_only(probe: StubProbe) -> None:
    # The invariant this stage lives or dies by: a POST to the maker endpoints
    # with valid credentials submits a real quote, so the check must never
    # issue one regardless of what the gateway answers.
    assert all(method == "GET" for method, _ in probe.calls)


class TestRfqGateway:
    def test_markets_list_passes_and_counts(self, make_context):
        probe = StubProbe({"combo-markets": ok({"markets": [COMBO_MARKET, COMBO_MARKET]})})
        ctx = make_context(probe)
        finding = RfqGateway().run(ctx)

        assert finding.severity is Severity.PASS
        assert "2 combo markets" in finding.summary
        assert finding.evidence["market_count"] == 2
        assert ctx.facts.get(Fact.RFQ_REACHABLE) is True
        assert_read_only(probe)

    def test_empty_markets_list_still_passes(self, make_context):
        # A quiet moment with no combo RFQs open is a healthy gateway, not an
        # error — the shape is right, the list is just empty.
        probe = StubProbe({"combo-markets": ok({"markets": []})})
        ctx = make_context(probe)
        finding = RfqGateway().run(ctx)

        assert finding.severity is Severity.PASS
        assert "0 combo markets" in finding.summary
        assert ctx.facts.get(Fact.RFQ_REACHABLE) is True
        assert_read_only(probe)

    def test_pass_detail_teaches_the_maker_flow(self, make_context):
        probe = StubProbe({"combo-markets": ok({"markets": [COMBO_MARKET]})})
        ctx = make_context(probe)
        detail = RfqGateway().run(ctx).detail

        # The three things partners otherwise learn the hard way.
        assert "clob.polymarket.com/rfq" in detail  # separate-host trap
        assert "/v1/maker/quotes" in detail
        assert "/v1/maker/quotes/cancel" in detail
        assert "/v1/maker/confirmations" in detail
        # Both RFQ auth-failure vocabularies, captured live 2026-08-15.
        assert "invalid l2 address header" in detail   # no address header
        assert "could not validate hmac signature" in detail  # rejected HMAC
        assert_read_only(probe)

    def test_without_credentials_notes_quotes_were_not_exercised(self, make_context):
        probe = StubProbe({"combo-markets": ok({"markets": []})})
        ctx = make_context(probe)
        ctx.facts.set(Fact.HAS_L2_CREDENTIALS, False)
        finding = RfqGateway().run(ctx)

        assert finding.severity is Severity.PASS
        assert "not exercised" in finding.detail
        assert_read_only(probe)

    def test_with_credentials_omits_the_not_exercised_note(self, make_context):
        probe = StubProbe({"combo-markets": ok({"markets": []})})
        ctx = make_context(probe)
        ctx.facts.set(Fact.HAS_L2_CREDENTIALS, True)
        finding = RfqGateway().run(ctx)

        assert finding.severity is Severity.PASS
        assert "not exercised" not in finding.detail
        assert_read_only(probe)

    def test_404_html_body_fails_with_host_note(self, make_context):
        # What a partner sees when they point at clob.polymarket.com/rfq, or
        # when a proxy hands back its own error page.
        probe = StubProbe({"combo-markets": http_error(404, "<html>404 Not Found</html>")})
        ctx = make_context(probe)
        finding = RfqGateway().run(ctx)

        assert finding.severity is Severity.FAIL
        assert "combos-rfq-api.polymarket.com" in finding.remedy
        assert ctx.facts.get(Fact.RFQ_REACHABLE) is False
        assert_read_only(probe)

    def test_connect_failure_reports_the_transport_error(self, make_context):
        probe = StubProbe({"combo-markets": unreachable()})
        ctx = make_context(probe)
        finding = RfqGateway().run(ctx)

        assert finding.severity is Severity.FAIL
        assert "ConnectError" in finding.detail
        assert ctx.facts.get(Fact.RFQ_REACHABLE) is False
        assert_read_only(probe)

    def test_200_without_markets_list_fails(self, make_context):
        # A 200 that isn't the documented shape is usually a captive portal or
        # proxy answering in the gateway's place.
        probe = StubProbe({"combo-markets": ok("<html>welcome</html>")})
        ctx = make_context(probe)
        finding = RfqGateway().run(ctx)

        assert finding.severity is Severity.FAIL
        assert ctx.facts.get(Fact.RFQ_REACHABLE) is False
        assert_read_only(probe)
