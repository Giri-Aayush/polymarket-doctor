"""Stage 2 — do the credentials work, and is the key bound to the right address?

The headline check is the last one. An API key is bound to whichever address
signed the L1 message that minted it. An order names its funder as signer. When
those differ the exchange returns

    the order signer address has to be the address of the API KEY

and nothing about that sentence points at the L1 step that happened minutes
earlier. Comparing the two up front is a couple of HTTP calls and saves the
multi-week investigation that py-clob-client-v2#70 documents 44 people doing.
"""

from __future__ import annotations

import time

from .. import issues, signing
from ..core.check import Check, Finding, Severity, Stage
from ..core.context import Context
from ..core.facts import Fact

API_KEYS_PATH = "/auth/api-keys"


class CredentialsPresent(Check):
    id = "auth.credentials"
    stage = Stage.AUTH
    title = "L2 credentials are usable"
    writes = frozenset({Fact.HAS_L2_CREDENTIALS})

    def run(self, ctx: Context) -> Finding:
        if not ctx.has_credentials:
            ctx.facts.set(Fact.HAS_L2_CREDENTIALS, False)
            # Not a failure. Address-only runs are the common case for a partner
            # doing a first pass before they've minted anything.
            return Finding.warn(
                "no L2 credentials supplied, running address-only",
                detail="Checks that need a live authenticated call are skipped.",
                remedy="Pass --api-key/--api-secret/--api-passphrase, or set "
                       "POLYMARKET_API_KEY, POLYMARKET_API_SECRET and "
                       "POLYMARKET_API_PASSPHRASE.",
            )

        creds = ctx.credentials
        assert creds is not None  # has_credentials
        try:
            signing.build_hmac_signature(creds.secret, 0, "GET", "/")
        except signing.SecretFormatError as exc:
            ctx.facts.set(Fact.HAS_L2_CREDENTIALS, False)
            return Finding.fail(
                "API secret is not url-safe base64",
                detail=str(exc),
                remedy="Re-copy the secret exactly as issued, padding included.",
            )

        ctx.facts.set(Fact.HAS_L2_CREDENTIALS, True)
        return Finding.ok("credentials present and well formed", **creds.redacted())


class HmacBodyEncoding(Check):
    """Catch the repr-vs-JSON body bug before it becomes an intermittent 401.

    Static: it signs sample bodies locally and never leaves the process. GETs
    are unaffected, which is exactly why this presents as "reads work, writes
    401" and sends people hunting through their key setup.
    """

    id = "auth.hmac-body"
    stage = Stage.AUTH
    title = "Request bodies hash the way the server expects"
    reads = frozenset({Fact.SDK})

    # A cancel body is the smallest realistic payload carrying a bool.
    SAMPLES = (
        ("strings", {"orderID": "0xabc"}),
        ("booleans", {"orderID": "0xabc", "negRisk": True}),
        ("nulls", {"orderID": "0xabc", "owner": None}),
    )

    def run(self, ctx: Context) -> Finding:
        broken = [name for name, body in self.SAMPLES if not signing.body_is_hmac_safe(body)]
        if not broken:
            return Finding.ok("body serialization matches JSON for sampled payloads")

        example = next(body for name, body in self.SAMPLES if name == broken[0])
        return Finding.warn(
            f"bodies with {' or '.join(broken)} hash differently than the server computes",
            detail=(
                "The SDK signs str(body).replace(\"'\", '\"'), which is Python's "
                "repr, not JSON. "
                f"{example!r} signs as {signing.canonical_body(example)!r} — "
                "True/None where JSON needs true/null, so the digests can't match. "
                "GET requests have no body and keep working, which is what makes "
                "this look like a credentials problem."
            ),
            remedy="Serialize the body once with json.dumps and sign exactly "
                   "those bytes, then send the same bytes.",
            issue=issues.HMAC_BODY_SERIALIZATION,
            affected=broken,
        )


class KeyIdentity(Check):
    """Compare the address the API key is bound to against the order signer."""

    id = "auth.key-identity"
    stage = Stage.AUTH
    title = "API key identity matches the order signer"
    reads = frozenset({
        Fact.HAS_L2_CREDENTIALS,
        Fact.SIGNER_ADDRESS,
        Fact.FUNDER_ADDRESS,
        Fact.CLOCK_SKEW_SECONDS,
    })
    writes = frozenset({Fact.API_KEY_IDENTITY})

    def run(self, ctx: Context) -> Finding:
        if not ctx.facts.get(Fact.HAS_L2_CREDENTIALS):
            ctx.facts.set(Fact.API_KEY_IDENTITY, None)
            return Finding(
                Severity.SKIP,
                "skipped, no credentials to test",
                detail="Supply L2 credentials to check key binding.",
            )

        signer = ctx.facts.get(Fact.SIGNER_ADDRESS)
        funder = ctx.facts.get(Fact.FUNDER_ADDRESS)

        # Try both candidates. The key authenticates as exactly one address, and
        # which one it is *is* the diagnosis — presenting only the signer would
        # report "rejected" for a key that's perfectly valid under the funder.
        bound_to = next(
            (candidate for candidate in _unique(signer, funder)
             if self._authenticates_as(ctx, candidate)),
            None,
        )

        if bound_to is None:
            return Finding.fail(
                "credentials rejected for every address we tried",
                detail=f"GET /auth/api-keys returned 401 presenting both "
                       f"{_short(signer)} and {_short(funder)}. Either the key "
                       f"belongs to some third address, or the secret and "
                       f"passphrase don't go together.",
                remedy="Re-derive credentials with the same wallet you sign "
                       "orders from.",
                issue=issues.API_KEY_CREATION_BLOCKED,
                tried=list(_unique(signer, funder)),
            )

        ctx.facts.set(Fact.API_KEY_IDENTITY, bound_to)

        if bound_to.lower() == funder.lower():
            return Finding.ok(
                f"key is bound to the order signer {_short(funder)}",
                api_key_identity=bound_to,
            )

        return Finding.fail(
            "API key is bound to a different address than your orders name",
            detail=(
                f"Key identity:  {bound_to}\n"
                f"Order signer:  {funder}\n\n"
                "These can never match. L1 auth signed as the EOA, so the key "
                "was minted against it, while orders correctly name the deposit "
                "wallet. Every POST /order comes back with \"the order signer "
                "address has to be the address of the API KEY\" while /auth and "
                "every read endpoint keep working."
            ),
            remedy=issues.SIGNER_IDENTITY_MISMATCH.workaround,
            issue=issues.SIGNER_IDENTITY_MISMATCH,
            api_key_identity=bound_to,
            order_signer=funder,
        )

    @staticmethod
    def _authenticates_as(ctx: Context, address: str) -> bool:
        """Does a read-only L2 call succeed presenting this address?

        Subtracts the measured clock skew so a drifting host surfaces as stage
        0's finding instead of a misleading 401 here.
        """
        creds = ctx.credentials
        assert creds is not None

        skew = ctx.facts.get(Fact.CLOCK_SKEW_SECONDS) or 0
        headers = signing.build_l2_headers(
            address=address,
            api_key=creds.api_key,
            secret=creds.secret,
            passphrase=creds.passphrase,
            timestamp=int(time.time() - skew),
            method="GET",
            request_path=API_KEYS_PATH,
        )
        return ctx.probe.get(ctx.endpoints.clob_url(API_KEYS_PATH), headers=headers).ok


def _unique(*addresses: str) -> tuple[str, ...]:
    """Preserve order, drop repeats — signer and funder are equal for EOAs."""
    seen: dict[str, None] = {}
    for address in addresses:
        seen.setdefault(address, None)
    return tuple(seen)


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


CHECKS = (CredentialsPresent(), HmacBodyEncoding(), KeyIdentity())
