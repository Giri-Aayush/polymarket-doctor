"""Stage 1 — which account is this, and can the installed SDK sign for it?

The V2 exchange wants orders from a deployed deposit wallet, signed with
Poly1271 and ERC-7739 nested TypedDataSign. The Python and TypeScript clients
don't emit that wrapping, so auth succeeds and every order is rejected. That one
mismatch accounts for 49 of the open issues across the v2 clients, and it is
undebuggable from the error text alone.

This stage answers it before a partner writes any trading code.
"""

from __future__ import annotations

from enum import IntEnum

from eth_utils import is_address, to_checksum_address

from .. import issues
from ..core.check import Check, Finding, Stage
from ..core.context import Context
from ..core.facts import Fact


class SignatureType(IntEnum):
    """Wire values the exchange accepts on an order."""

    EOA = 0
    POLY_PROXY = 1
    POLY_GNOSIS_SAFE = 2
    POLY_1271 = 3

    @property
    def label(self) -> str:
        return {
            SignatureType.EOA: "EOA",
            SignatureType.POLY_PROXY: "POLY_PROXY",
            SignatureType.POLY_GNOSIS_SAFE: "POLY_GNOSIS_SAFE",
            SignatureType.POLY_1271: "POLY_1271",
        }[self]


class AccountKind(str):
    """Free-form on purpose — this is a label for humans, not a branch key."""


# Clients that can produce the ERC-7739 nested signature a deposit wallet's
# isValidSignature accepts. Verified against the evidence matrix in
# py-clob-client-v2#111: Rust places orders on this flow, Python and TS don't.
SDKS_WITH_ERC7739 = frozenset({"rs_clob_client_v2"})


class Addresses(Check):
    id = "identity.addresses"
    stage = Stage.IDENTITY
    title = "Signer and funder addresses are well formed"
    writes = frozenset({Fact.SIGNER_ADDRESS, Fact.FUNDER_ADDRESS})

    def run(self, ctx: Context) -> Finding:
        if not ctx.signer_address:
            return Finding.fail(
                "no signer address supplied",
                remedy="Pass --address, or set POLYMARKET_ADDRESS.",
            )

        signer = _checksum(ctx.signer_address)
        if signer is None:
            return Finding.fail(
                f"{ctx.signer_address!r} is not an Ethereum address",
                remedy="Expected 0x followed by 40 hex characters.",
            )

        # No explicit funder means self-funded: the signer pays. That's the EOA
        # shape, and stage 1's next check is where it gets ruled out for V2.
        funder_input = ctx.funder_address or ctx.signer_address
        funder = _checksum(funder_input)
        if funder is None:
            return Finding.fail(
                f"funder {funder_input!r} is not an Ethereum address",
                remedy="Expected 0x followed by 40 hex characters.",
            )

        ctx.facts.set(Fact.SIGNER_ADDRESS, signer)
        ctx.facts.set(Fact.FUNDER_ADDRESS, funder)

        if signer == funder:
            return Finding.ok(f"signer and funder are both {_short(signer)}",
                              signer=signer, funder=funder)
        return Finding.ok(
            f"signer {_short(signer)} funding {_short(funder)}",
            signer=signer,
            funder=funder,
        )


class AccountShape(Check):
    """Classify the funder and pick the signature type the exchange expects."""

    id = "identity.account-kind"
    stage = Stage.IDENTITY
    title = "Account type and required signature scheme"
    reads = frozenset({Fact.SIGNER_ADDRESS, Fact.FUNDER_ADDRESS, Fact.SDK})
    writes = frozenset({
        Fact.ACCOUNT_KIND,
        Fact.SIGNATURE_TYPE,
        Fact.DEPOSIT_WALLET_DEPLOYED,
    })

    def run(self, ctx: Context) -> Finding:
        signer = ctx.facts.get(Fact.SIGNER_ADDRESS)
        funder = ctx.facts.get(Fact.FUNDER_ADDRESS)

        deployed = self._is_deployed(ctx, funder)
        if deployed is None:
            return Finding.fail(
                "relayer would not say whether the funder is deployed",
                detail=f"GET {ctx.endpoints.relayer}/deployed did not return a "
                       f"usable answer for {funder}.",
                remedy="Re-run; if it persists the relayer is down and stage 2 "
                       "can't be trusted either.",
            )

        ctx.facts.set(Fact.DEPOSIT_WALLET_DEPLOYED, deployed)

        if not deployed:
            # Nothing at that address, so the funder is a bare EOA. V2 stopped
            # accepting those after the 2026-04-28 cutover.
            ctx.facts.set(Fact.ACCOUNT_KIND, "EOA")
            ctx.facts.set(Fact.SIGNATURE_TYPE, SignatureType.EOA)
            return Finding.fail(
                f"{_short(funder)} is an EOA with no deposit wallet deployed",
                detail="The V2 exchange rejects EOA-funded orders with "
                       "\"maker address not allowed, please use the deposit "
                       "wallet flow\". Authentication still succeeds, which is "
                       "why this reads as a signing bug.",
                remedy="Deploy a deposit wallet, fund it, and pass it as "
                       "--funder.",
                issue=issues.DEPOSIT_WALLET_REQUIRED,
                funder=funder,
                deployed=False,
            )

        ctx.facts.set(Fact.ACCOUNT_KIND, "deposit wallet")
        ctx.facts.set(Fact.SIGNATURE_TYPE, SignatureType.POLY_1271)

        sdk = ctx.facts.get(Fact.SDK) or {}
        module = sdk.get("module")
        if module and module not in SDKS_WITH_ERC7739:
            return Finding.fail(
                f"{sdk.get('label', module)} cannot sign for a deposit wallet",
                detail="Orders from a deposit wallet need Poly1271 with ERC-7739 "
                       "nested TypedDataSign, which its on-chain isValidSignature "
                       "verifies. This client doesn't emit that wrapping, so "
                       "/auth passes and POST /order is rejected every time.",
                remedy=issues.SIGNER_IDENTITY_MISMATCH.workaround,
                issue=issues.SIGNER_IDENTITY_MISMATCH,
                funder=funder,
                signature_type=SignatureType.POLY_1271.label,
            )

        return Finding.ok(
            f"deposit wallet {_short(funder)} deployed, signing as "
            f"{SignatureType.POLY_1271.label}",
            signer=signer,
            funder=funder,
            signature_type=SignatureType.POLY_1271.label,
        )

    @staticmethod
    def _is_deployed(ctx: Context, address: str) -> bool | None:
        """Ask the relayer. type=SAFE is the default and covers deposit wallets.

        The endpoint accepts unknown `type` values without complaint, so don't
        read anything into a response for a type you made up.
        """
        response = ctx.probe.get(
            ctx.endpoints.relayer_url("/deployed"),
            params={"address": address, "type": "SAFE"},
        )
        if not response.ok or not isinstance(response.body, dict):
            return None
        deployed = response.body.get("deployed")
        return deployed if isinstance(deployed, bool) else None


def _checksum(address: str) -> str | None:
    candidate = address.strip()
    if not is_address(candidate):
        return None
    return to_checksum_address(candidate)


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


CHECKS = (Addresses(), AccountShape())
