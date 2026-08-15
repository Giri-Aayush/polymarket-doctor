"""Stage 3 behavior around the balance read that lies.

The load-bearing assertion: zero never fails. py-clob-client-v2#105 documents
funded accounts reading balance=0 because UI deposits live on an internal
ledger this endpoint doesn't see, so a partner gating on the read would brick
a working setup — the check has to warn and say so, not block.
"""

from __future__ import annotations

from conftest import DEPOSIT_WALLET, EOA, StubProbe, http_error, ok

from polymarket_doctor import issues
from polymarket_doctor.checks.funding import CollateralBalance
from polymarket_doctor.core.check import Severity
from polymarket_doctor.core.context import Credentials
from polymarket_doctor.core.facts import Fact
from polymarket_doctor.net.http import Response

CREDS = Credentials(
    api_key="8f1a0c33-key",
    # url-safe base64 of 32 bytes; the real thing is the same shape.
    secret="cG9seW1hcmtldC1kb2N0b3ItdGVzdC1zZWNyZXQtMzI=",
    # Distinctive on purpose: the leak test does a substring search.
    passphrase="c2f4b1e0-passphrase",
)

FUNDED = ok({"balance": "25000000", "allowances": {"0xExchange": "1000000000"}})
ZERO = ok({"balance": "0", "allowances": {}})
UNAUTHORIZED = http_error(401, {"error": "Unauthorized/Invalid api key"})


class RecordingProbe:
    """Captures the request the check sends — its shape is the contract."""

    def __init__(self, response: Response) -> None:
        self.response = response
        self.headers: dict | None = None
        self.params: dict | None = None

    def get(self, url: str, *, headers=None, params=None) -> Response:
        self.headers = dict(headers or {})
        self.params = dict(params or {})
        return self.response

    def post(self, url: str, *, headers=None, json_body=None) -> Response:
        raise AssertionError("stage 3 must not POST")


def build_context(make_context, probe, *, creds=CREDS, signature_type=1):
    ctx = make_context(probe, credentials=creds)
    ctx.facts.set(Fact.HAS_L2_CREDENTIALS, creds is not None)
    ctx.facts.set(Fact.SIGNER_ADDRESS, EOA)
    ctx.facts.set(Fact.CLOCK_SKEW_SECONDS, 0.0)
    ctx.facts.set(Fact.SIGNATURE_TYPE, signature_type)
    return ctx


def test_positive_balance_passes_and_publishes_the_facts(make_context):
    probe = StubProbe({"/balance-allowance": FUNDED})
    ctx = build_context(make_context, probe)

    finding = CollateralBalance().run(ctx)

    assert finding.severity is Severity.PASS
    assert ctx.facts.get(Fact.COLLATERAL_BALANCE) == 25_000_000
    assert ctx.facts.get(Fact.EXCHANGE_APPROVALS) == {"0xExchange": "1000000000"}


def test_zero_balance_warns_with_the_ledger_issue_and_never_blocks(make_context):
    # The one behavior this stage exists for: zero is the documented answer for
    # funded accounts, so failing here would tell a working partner they're broke.
    probe = StubProbe({"/balance-allowance": ZERO})
    ctx = build_context(make_context, probe)

    finding = CollateralBalance().run(ctx)

    assert finding.severity is Severity.WARN
    assert not finding.severity.is_blocking
    assert finding.issue is issues.BALANCE_READS_ZERO
    # The detail has to carry the actual instruction, not just the citation.
    assert "gate order placement" in finding.detail
    assert ctx.facts.get(Fact.COLLATERAL_BALANCE) == 0


def test_401_fails_and_points_back_at_stage_2(make_context):
    # Verified live 2026-08-15: bad credentials get exactly this body. If it
    # shows up here, auth is the problem and stage 2 is where the answer lives.
    probe = StubProbe({"/balance-allowance": UNAUTHORIZED})
    ctx = build_context(make_context, probe)

    finding = CollateralBalance().run(ctx)

    assert finding.severity is Severity.FAIL
    assert "stage 2" in finding.detail.lower()
    # Facts still written — None means unknown, not "stage didn't run".
    assert Fact.COLLATERAL_BALANCE in ctx.facts
    assert ctx.facts.get(Fact.COLLATERAL_BALANCE) is None
    assert Fact.EXCHANGE_APPROVALS in ctx.facts


def test_without_credentials_the_check_skips_without_touching_the_network(make_context):
    probe = StubProbe()
    ctx = build_context(make_context, probe, creds=None)

    finding = CollateralBalance().run(ctx)

    assert finding.severity is Severity.SKIP
    assert probe.calls == []
    assert Fact.COLLATERAL_BALANCE in ctx.facts
    assert ctx.facts.get(Fact.COLLATERAL_BALANCE) is None
    assert Fact.EXCHANGE_APPROVALS in ctx.facts


def test_server_errors_fail_without_a_misleading_citation(make_context):
    probe = StubProbe({"/balance-allowance": http_error(503, "<html>bad gateway</html>")})
    ctx = build_context(make_context, probe)

    finding = CollateralBalance().run(ctx)

    assert finding.severity is Severity.FAIL
    assert finding.issue is None
    assert ctx.facts.get(Fact.COLLATERAL_BALANCE) is None


def test_a_200_without_a_balance_field_warns_and_records_unknown(make_context):
    probe = StubProbe({"/balance-allowance": ok({"unexpected": True})})
    ctx = build_context(make_context, probe)

    finding = CollateralBalance().run(ctx)

    assert finding.severity is Severity.WARN
    assert finding.issue is None  # not the ledger quirk, just an odd body
    assert ctx.facts.get(Fact.COLLATERAL_BALANCE) is None


def test_the_request_presents_the_signer_and_asks_for_collateral(make_context):
    probe = RecordingProbe(FUNDED)
    ctx = build_context(make_context, probe, signature_type=2)

    CollateralBalance().run(ctx)

    assert probe.headers["POLY_ADDRESS"] == EOA
    assert "POLY_SIGNATURE" in probe.headers
    assert probe.params["asset_type"] == "COLLATERAL"
    # Which ledger the server consults depends on the signature type, so the
    # known value has to ride along.
    assert probe.params["signature_type"] == 2


def test_only_gets_are_made(make_context):
    # Belt and braces on top of RecordingProbe's raising post(): the whole
    # stage is read-only by contract.
    probe = StubProbe({"/balance-allowance": FUNDED})
    ctx = build_context(make_context, probe)

    CollateralBalance().run(ctx)

    assert probe.calls
    assert all(method == "GET" for method, _ in probe.calls)


def test_secrets_never_appear_in_any_finding(make_context):
    # A partner pastes this output into a support thread; every path has to be
    # safe to share, including the failure ones.
    for response in (FUNDED, ZERO, UNAUTHORIZED):
        ctx = build_context(make_context, StubProbe({"/balance-allowance": response}))
        finding = CollateralBalance().run(ctx)

        rendered = repr(finding)
        assert CREDS.secret not in rendered
        assert CREDS.passphrase not in rendered


def test_404_reads_as_unregistered_account_not_a_dead_endpoint(make_context):
    # Live observation 2026-08-15: valid creds + never-traded signer/funder
    # combo → 404, while bad creds → 401. The two must not be conflated.
    ctx = make_context(
        StubProbe({"/balance-allowance": http_error(404)}),
        credentials=CREDS,
    )
    ctx.facts.set(Fact.HAS_L2_CREDENTIALS, True)
    ctx.facts.set(Fact.SIGNER_ADDRESS, EOA)
    ctx.facts.set(Fact.FUNDER_ADDRESS, DEPOSIT_WALLET)
    ctx.facts.set(Fact.SIGNATURE_TYPE, None)
    ctx.facts.set(Fact.CLOCK_SKEW_SECONDS, 0.0)

    finding = CollateralBalance().run(ctx)

    assert finding.severity is Severity.WARN
    assert "no balance record" in finding.summary
    assert ctx.facts.get(Fact.COLLATERAL_BALANCE) is None
