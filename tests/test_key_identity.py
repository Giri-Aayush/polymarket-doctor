"""The check the whole tool exists for: py-clob-client-v2#70.

An API key is bound to whichever address signed the L1 message that minted it.
An order names its funder as signer. When those differ, /auth and every read
endpoint keep working and POST /order is rejected forever.
"""

from __future__ import annotations

from conftest import DEPOSIT_WALLET, EOA, StubProbe, ok

from polymarket_doctor import issues
from polymarket_doctor.checks.auth import CredentialsPresent, KeyIdentity
from polymarket_doctor.core.check import Severity
from polymarket_doctor.core.context import Credentials
from polymarket_doctor.core.facts import Fact
from polymarket_doctor.net.http import Response

CREDS = Credentials(
    api_key="8f1a0c33-key",
    # url-safe base64 of 32 bytes; the real thing is the same shape.
    secret="cG9seW1hcmtldC1kb2N0b3ItdGVzdC1zZWNyZXQtMzI=",
    # Distinctive on purpose: the leak test does a substring search, and a
    # short generic value collides with unrelated text like Severity.PASS.
    passphrase="c2f4b1e0-passphrase",
)


class OnlyAuthenticatesAs:
    """Accepts L2 requests for exactly one POLY_ADDRESS, 401s everything else.

    That's how the exchange behaves: the key belongs to one address, and asking
    as anyone else is unauthorized.
    """

    def __init__(self, address: str) -> None:
        self.address = address
        self.presented: list[str] = []

    def get(self, url: str, *, headers=None, params=None) -> Response:
        presented = (headers or {}).get("POLY_ADDRESS", "")
        self.presented.append(presented)
        if presented.lower() == self.address.lower():
            return ok({"apiKeys": [self.address]})
        return Response(401, {"error": "Unauthorized/Invalid api key"}, 1.0)

    def post(self, url: str, *, headers=None, json_body=None) -> Response:
        raise AssertionError("stage 2 must not POST")


def build_context(make_context, probe, *, signer, funder, creds=CREDS):
    ctx = make_context(probe, credentials=creds)
    ctx.facts.set(Fact.HAS_L2_CREDENTIALS, creds is not None)
    ctx.facts.set(Fact.SIGNER_ADDRESS, signer)
    ctx.facts.set(Fact.FUNDER_ADDRESS, funder)
    ctx.facts.set(Fact.CLOCK_SKEW_SECONDS, 0.0)
    return ctx


def test_key_bound_to_the_order_signer_passes(make_context):
    probe = OnlyAuthenticatesAs(EOA)
    ctx = build_context(make_context, probe, signer=EOA, funder=EOA)

    finding = KeyIdentity().run(ctx)

    assert finding.severity is Severity.PASS
    assert ctx.facts.get(Fact.API_KEY_IDENTITY) == EOA


def test_key_on_the_eoa_with_a_separate_funder_is_the_intended_shape(make_context):
    # The whole #70 cluster reads this as the bug. Polymarket's answer on
    # ts-sdk#73 is that it's the documented model: L1 auth identifies the
    # signer, orders execute from the funder. Reporting it as a failure sends
    # people off changing SDKs when their signature type is the actual problem.
    probe = OnlyAuthenticatesAs(EOA)
    ctx = build_context(make_context, probe, signer=EOA, funder=DEPOSIT_WALLET)

    finding = KeyIdentity().run(ctx)

    assert finding.severity is Severity.PASS
    assert finding.evidence["api_key_identity"] == EOA
    assert finding.evidence["order_funder"] == DEPOSIT_WALLET


def test_key_minted_against_the_funder_is_the_inverse_and_only_warns(make_context):
    # Backwards from the documented shape, but we don't know that the exchange
    # rejects it, so it doesn't get to block the run.
    probe = OnlyAuthenticatesAs(DEPOSIT_WALLET)
    ctx = build_context(make_context, probe, signer=EOA, funder=DEPOSIT_WALLET)

    finding = KeyIdentity().run(ctx)

    assert finding.severity is Severity.WARN
    assert finding.issue is issues.SIGNATURE_TYPE_MISMATCH


def test_both_candidates_are_tried_before_giving_up(make_context):
    probe = OnlyAuthenticatesAs(DEPOSIT_WALLET)
    ctx = build_context(make_context, probe, signer=EOA, funder=DEPOSIT_WALLET)

    KeyIdentity().run(ctx)

    # Signer first, funder second — reporting "rejected" after only trying the
    # signer would be wrong for a key that's valid under the funder.
    assert probe.presented == [EOA, DEPOSIT_WALLET]


def test_credentials_valid_for_neither_address(make_context):
    probe = OnlyAuthenticatesAs("0x0000000000000000000000000000000000000009")
    ctx = build_context(make_context, probe, signer=EOA, funder=DEPOSIT_WALLET)

    finding = KeyIdentity().run(ctx)

    assert finding.severity is Severity.FAIL
    assert finding.issue is issues.API_KEY_CREATION_BLOCKED
    assert finding.evidence["tried"] == [EOA, DEPOSIT_WALLET]


def test_an_eoa_account_is_only_probed_once(make_context):
    probe = OnlyAuthenticatesAs(EOA)
    ctx = build_context(make_context, probe, signer=EOA, funder=EOA)

    KeyIdentity().run(ctx)

    assert probe.presented == [EOA]


def test_without_credentials_the_check_skips(make_context):
    ctx = build_context(make_context, StubProbe(), signer=EOA, funder=EOA, creds=None)
    ctx.facts.set(Fact.HAS_L2_CREDENTIALS, False)

    finding = KeyIdentity().run(ctx)

    assert finding.severity is Severity.SKIP
    assert ctx.facts.get(Fact.API_KEY_IDENTITY) is None


class TestCredentialsPresent:
    def test_absent_credentials_warn_rather_than_fail(self, make_context):
        # Address-only is a legitimate first pass, not an error.
        ctx = make_context(StubProbe())
        finding = CredentialsPresent().run(ctx)

        assert finding.severity is Severity.WARN
        assert ctx.facts.get(Fact.HAS_L2_CREDENTIALS) is False

    def test_unpadded_secret_is_caught_before_any_request(self, make_context):
        probe = StubProbe()
        ctx = make_context(probe, credentials=Credentials("k", "not base64!!", "p"))

        finding = CredentialsPresent().run(ctx)

        assert finding.severity is Severity.FAIL
        assert probe.calls == []

    def test_secret_never_appears_in_the_finding(self, make_context):
        ctx = make_context(StubProbe(), credentials=CREDS)
        finding = CredentialsPresent().run(ctx)

        rendered = repr(finding)
        assert CREDS.secret not in rendered
        assert CREDS.passphrase not in rendered
