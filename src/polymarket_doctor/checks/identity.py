"""Stage 1 — which account is this, and which signature type does it need?

This is where the biggest open cluster gets resolved, and the resolution is not
what the issue threads say it is.

The threads (py-clob-client-v2#70 and ~15 duplicates) conclude that the SDKs
can't sign for deposit wallets and that only the Rust client works. Polymarket's
own answer on ts-sdk#73 is different: an API key authenticating the EOA while
orders execute from the funder is the intended model, and the reported failures
were accounts using signature_type=3 (POLY_1271) on a funder that is actually an
older Gnosis Safe and needs signature_type=2.

Checked on chain, that holds. Both the funder in #70 and the one Polymarket
identified in #73 are Gnosis Safe v1.3.0 proxies owned by the reporting EOA,
with byte-identical proxy code and the same implementation. So the fix for most
people is a different signature_type, not a different SDK.

Nothing in the Polymarket API exposes which kind a funder is. `GET /deployed`
answers the same for `type=SAFE` and `type=WALLET`. The only reliable
discriminator is asking the contract whether it implements the Safe interface,
which is what this stage does.
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
        return f"{self.name} ({self.value})"


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
        return Finding.ok(f"signer {_short(signer)} funding {_short(funder)}",
                          signer=signer, funder=funder)


class AccountShape(Check):
    """Classify the funder on chain and name the signature type it needs."""

    id = "identity.account-kind"
    stage = Stage.IDENTITY
    title = "Funder type and required signature scheme"
    reads = frozenset({Fact.SIGNER_ADDRESS, Fact.FUNDER_ADDRESS})
    writes = frozenset({
        Fact.ACCOUNT_KIND,
        Fact.SIGNATURE_TYPE,
        Fact.DEPOSIT_WALLET_DEPLOYED,
        Fact.FUNDER_PROFILE,
    })

    def run(self, ctx: Context) -> Finding:
        funder = ctx.facts.get(Fact.FUNDER_ADDRESS)

        if ctx.chain is None:
            return self._without_chain_access(ctx, funder)

        profile = ctx.chain.profile(funder)
        if profile is None:
            return Finding.fail(
                "could not read the funder from chain",
                detail="The RPC did not answer eth_getCode. Without it there's "
                       "no way to tell a Gnosis Safe from a deposit wallet, and "
                       "that determines your signature type.",
                remedy="Pass --rpc with a Polygon endpoint you trust.",
            )

        ctx.facts.set(Fact.FUNDER_PROFILE, profile)
        ctx.facts.set(Fact.DEPOSIT_WALLET_DEPLOYED, profile.has_code)

        if not profile.has_code:
            ctx.facts.set(Fact.ACCOUNT_KIND, "EOA")
            ctx.facts.set(Fact.SIGNATURE_TYPE, SignatureType.EOA)
            return Finding.fail(
                f"{_short(funder)} is an EOA, nothing deployed at that address",
                detail="V2 rejects EOA-funded orders with \"maker address not "
                       "allowed, please use the deposit wallet flow\". "
                       "Authentication still succeeds, which is why this reads "
                       "as a signing problem.",
                remedy="Fund through a deposit wallet and pass it as --funder.",
                issue=issues.EOA_FLOW_REJECTED,
                funder=funder,
            )

        if profile.is_gnosis_safe:
            ctx.facts.set(Fact.ACCOUNT_KIND, f"Gnosis Safe {profile.safe_version}")
            ctx.facts.set(Fact.SIGNATURE_TYPE, SignatureType.POLY_GNOSIS_SAFE)
            return Finding.ok(
                f"funder is a Gnosis Safe {profile.safe_version}, "
                f"use signature_type={SignatureType.POLY_GNOSIS_SAFE.value}",
                detail="The UI deploys this kind when the account was created "
                       "with an external wallet rather than email or Google. "
                       "Signing it as POLY_1271 (3) is the most common cause of "
                       "\"the order signer address has to be the address of the "
                       "API KEY\".",
                funder=funder,
                safe_version=profile.safe_version,
                signature_type=SignatureType.POLY_GNOSIS_SAFE.value,
            )

        ctx.facts.set(Fact.ACCOUNT_KIND, "deposit wallet")
        ctx.facts.set(Fact.SIGNATURE_TYPE, SignatureType.POLY_1271)
        return Finding.ok(
            f"funder is a deposit wallet, use "
            f"signature_type={SignatureType.POLY_1271.value}",
            detail="It has code but doesn't implement the Safe interface, so "
                   "it's the newer deposit-wallet contract.",
            funder=funder,
            signature_type=SignatureType.POLY_1271.value,
        )

    @staticmethod
    def _without_chain_access(ctx: Context, funder: str) -> Finding:
        """Fall back to the relayer, which can only say deployed or not.

        Worth doing rather than failing outright: knowing the funder has no
        contract at all is still the answer for a lot of people.
        """
        response = ctx.probe.get(
            ctx.endpoints.relayer_url("/deployed"),
            params={"address": funder, "type": "SAFE"},
        )
        deployed = response.body.get("deployed") if isinstance(response.body, dict) else None
        if not isinstance(deployed, bool):
            return Finding.fail(
                "could not determine the funder type",
                detail="No RPC configured and the relayer did not answer.",
            )

        ctx.facts.set(Fact.FUNDER_PROFILE, None)
        ctx.facts.set(Fact.DEPOSIT_WALLET_DEPLOYED, deployed)

        if not deployed:
            # The relayer only tracks wallets its own Safe factory deployed.
            # Beacon-proxy wallets (builder wallets, the newer deposit-wallet
            # architecture) have code on chain and still answer false here —
            # observed live 2026-08-15 — so without an RPC this stays a WARN,
            # not a confident "you have no wallet".
            ctx.facts.set(Fact.ACCOUNT_KIND, "no Safe-factory wallet; kind unknown")
            ctx.facts.set(Fact.SIGNATURE_TYPE, None)
            return Finding.warn(
                f"the relayer knows no Safe-factory wallet at {_short(funder)}",
                detail="That usually means a bare EOA, which V2 rejects with "
                       "\"maker address not allowed\". But the relayer is blind "
                       "to beacon-proxy wallets, which have code it never "
                       "deployed, so this answer can't rule one out.",
                remedy="Re-run with --rpc to classify the address from chain "
                       "state instead.",
                issue=issues.EOA_FLOW_REJECTED,
            )

        ctx.facts.set(Fact.ACCOUNT_KIND, "contract wallet, kind unknown")
        ctx.facts.set(Fact.SIGNATURE_TYPE, None)
        return Finding.warn(
            "funder is a contract wallet but its kind is unknown",
            detail="The relayer answers the same for a Gnosis Safe and a "
                   "deposit wallet, and they need different signature types. "
                   "Telling them apart needs an eth_call.",
            remedy="Re-run with --rpc so this resolves to a signature type.",
            issue=issues.SIGNATURE_TYPE_MISMATCH,
        )


class SignerAuthority(Check):
    """Can the signer actually authorize for the funder?

    A Safe only accepts signatures from its owners. If the key you're signing
    with isn't one, no amount of fixing the signature type will help, and the
    rejection looks identical to every other signature failure.
    """

    id = "identity.signer-authority"
    stage = Stage.IDENTITY
    title = "Signer is authorized for the funder"
    reads = frozenset({Fact.SIGNER_ADDRESS, Fact.FUNDER_ADDRESS, Fact.FUNDER_PROFILE})

    def run(self, ctx: Context) -> Finding:
        signer = ctx.facts.get(Fact.SIGNER_ADDRESS)
        funder = ctx.facts.get(Fact.FUNDER_ADDRESS)
        profile = ctx.facts.get(Fact.FUNDER_PROFILE)

        if signer == funder:
            return Finding.ok("signer funds itself, nothing to authorize")

        if profile is None or not profile.is_gnosis_safe:
            return Finding.ok("no owner set to check for this funder type")

        if not profile.owners:
            return Finding.warn(
                "funder is a Safe but returned no owners",
                detail="getOwners() came back empty, which shouldn't happen for "
                       "a deployed Safe. Treat the signature-type advice above "
                       "as unconfirmed.",
            )

        if profile.is_owner(signer):
            return Finding.ok(
                f"{_short(signer)} is an owner of the Safe",
                owners=list(profile.owners),
            )

        return Finding.fail(
            f"{_short(signer)} is not an owner of {_short(funder)}",
            detail="The Safe only honours signatures from its owners, so orders "
                   "signed with this key are rejected no matter which signature "
                   "type you send.",
            remedy=f"Sign with one of: {', '.join(profile.owners)}",
            signer=signer,
            owners=list(profile.owners),
        )


def _checksum(address: str) -> str | None:
    candidate = address.strip()
    if not is_address(candidate):
        return None
    return to_checksum_address(candidate)


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


CHECKS = (Addresses(), AccountShape(), SignerAuthority())
