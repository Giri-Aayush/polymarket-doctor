"""Stage 1's funder classification.

The distinction it draws — Gnosis Safe vs deposit wallet — isn't available from
any Polymarket API. `GET /deployed` answers identically for `type=SAFE` and
`type=WALLET`, and the two wallet kinds share byte-identical proxy code and the
same implementation slot. Asking the contract whether it implements the Safe
interface is the only thing that separates them.
"""

from __future__ import annotations

from conftest import DEPOSIT_WALLET, EOA, StubProbe, http_error, ok

from polymarket_doctor import issues
from polymarket_doctor.checks.identity import AccountShape, SignatureType, SignerAuthority
from polymarket_doctor.core.check import Severity
from polymarket_doctor.core.facts import Fact
from polymarket_doctor.net.chain import ContractProfile

SAFE_V13 = ContractProfile(has_code=True, safe_version="1.3.0", owners=(EOA.lower(),))
NEW_DEPOSIT_WALLET = ContractProfile(has_code=True, safe_version=None, owners=())
NO_CODE = ContractProfile(has_code=False)


class FakeChain:
    def __init__(self, profile: ContractProfile | None) -> None:
        self._profile = profile

    def profile(self, address: str) -> ContractProfile | None:
        return self._profile


def context_for(make_context, profile, *, signer=EOA, funder=DEPOSIT_WALLET, probe=None):
    ctx = make_context(probe or StubProbe(), signer_address=signer, funder_address=funder)
    ctx.chain = FakeChain(profile)
    ctx.facts.set(Fact.SIGNER_ADDRESS, signer)
    ctx.facts.set(Fact.FUNDER_ADDRESS, funder)
    return ctx


class TestAccountShape:
    def test_gnosis_safe_funder_selects_signature_type_2(self, make_context):
        # This is what both the #70 and #73 funders actually are on chain.
        ctx = context_for(make_context, SAFE_V13)

        finding = AccountShape().run(ctx)

        assert finding.severity is Severity.PASS
        assert ctx.facts.get(Fact.SIGNATURE_TYPE) is SignatureType.POLY_GNOSIS_SAFE
        assert "signature_type=2" in finding.summary
        assert "1.3.0" in finding.summary

    def test_safe_finding_names_the_mistake_it_prevents(self, make_context):
        ctx = context_for(make_context, SAFE_V13)
        assert "POLY_1271" in AccountShape().run(ctx).detail

    def test_deposit_wallet_funder_selects_signature_type_3(self, make_context):
        ctx = context_for(make_context, NEW_DEPOSIT_WALLET)

        finding = AccountShape().run(ctx)

        assert finding.severity is Severity.PASS
        assert ctx.facts.get(Fact.SIGNATURE_TYPE) is SignatureType.POLY_1271
        assert "signature_type=3" in finding.summary

    def test_funder_with_no_contract_fails(self, make_context):
        ctx = context_for(make_context, NO_CODE, funder=EOA)

        finding = AccountShape().run(ctx)

        assert finding.severity is Severity.FAIL
        assert finding.issue is issues.EOA_FLOW_REJECTED
        assert ctx.facts.get(Fact.SIGNATURE_TYPE) is SignatureType.EOA

    def test_unreadable_rpc_fails_rather_than_guessing(self, make_context):
        # Guessing here would send someone to the wrong signature type, which is
        # the exact failure this stage exists to prevent.
        ctx = context_for(make_context, None)
        finding = AccountShape().run(ctx)

        assert finding.severity is Severity.FAIL
        assert "--rpc" in finding.remedy


class TestAccountShapeWithoutChainAccess:
    """--no-rpc degrades to the relayer, which can't name a signature type."""

    def _context(self, make_context, relayer_body, funder=DEPOSIT_WALLET):
        ctx = make_context(
            StubProbe({"/deployed": relayer_body}),
            signer_address=EOA, funder_address=funder,
        )
        ctx.facts.set(Fact.SIGNER_ADDRESS, EOA)
        ctx.facts.set(Fact.FUNDER_ADDRESS, funder)
        return ctx

    def test_deployed_contract_warns_that_the_kind_is_unknown(self, make_context):
        ctx = self._context(make_context, ok({"deployed": True}))

        finding = AccountShape().run(ctx)

        assert finding.severity is Severity.WARN
        assert ctx.facts.get(Fact.SIGNATURE_TYPE) is None
        assert "--rpc" in finding.remedy

    def test_relayer_unknown_wallet_warns_rather_than_declaring_an_eoa(self, make_context):
        # The relayer only tracks Safe-factory wallets. A beacon-proxy wallet
        # has code the relayer never deployed and answers false here, so
        # without an RPC "deployed: false" cannot support a confident EOA
        # diagnosis — observed live with a builder wallet on 2026-08-15.
        ctx = self._context(make_context, ok({"deployed": False}), funder=EOA)

        finding = AccountShape().run(ctx)

        assert finding.severity is Severity.WARN
        assert "--rpc" in finding.remedy
        assert ctx.facts.get(Fact.SIGNATURE_TYPE) is None

    def test_relayer_error_is_a_failure(self, make_context):
        ctx = self._context(make_context, http_error(500))
        assert AccountShape().run(ctx).severity is Severity.FAIL


class TestSignerAuthority:
    def test_owner_passes(self, make_context):
        ctx = context_for(make_context, SAFE_V13)
        ctx.facts.set(Fact.FUNDER_PROFILE, SAFE_V13)

        assert SignerAuthority().run(ctx).severity is Severity.PASS

    def test_non_owner_fails_with_the_owners_listed(self, make_context):
        stranger = "0x0000000000000000000000000000000000000042"
        ctx = context_for(make_context, SAFE_V13, signer=stranger)
        ctx.facts.set(Fact.FUNDER_PROFILE, SAFE_V13)

        finding = SignerAuthority().run(ctx)

        assert finding.severity is Severity.FAIL
        assert EOA.lower() in finding.remedy

    def test_self_funded_account_has_nothing_to_check(self, make_context):
        ctx = context_for(make_context, NO_CODE, signer=EOA, funder=EOA)
        ctx.facts.set(Fact.FUNDER_PROFILE, NO_CODE)

        assert SignerAuthority().run(ctx).severity is Severity.PASS

    def test_deposit_wallet_has_no_owner_set_to_check(self, make_context):
        ctx = context_for(make_context, NEW_DEPOSIT_WALLET)
        ctx.facts.set(Fact.FUNDER_PROFILE, NEW_DEPOSIT_WALLET)

        assert SignerAuthority().run(ctx).severity is Severity.PASS

    def test_safe_with_no_owners_warns(self, make_context):
        empty = ContractProfile(has_code=True, safe_version="1.3.0", owners=())
        ctx = context_for(make_context, empty)
        ctx.facts.set(Fact.FUNDER_PROFILE, empty)

        assert SignerAuthority().run(ctx).severity is Severity.WARN
