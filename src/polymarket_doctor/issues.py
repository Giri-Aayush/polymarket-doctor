"""Open bugs a failing check probably maps to.

Most of what this tool catches isn't the partner's mistake — it's a known bug
someone already filed and is sitting unanswered. Telling them "you hit #70,
here's the workaround, 44 people are behind you" is worth more than telling them
their signature is invalid.

Counts and dates were read off the GitHub API on 2026-08-15. They drift; ages are
computed at render time so nothing here goes stale silently. `verify_issues.py`
in scripts/ re-reads them and fails CI when a number moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class KnownIssue:
    repo: str
    number: int
    title: str
    opened: date
    comments: int
    workaround: str | None = None

    @property
    def url(self) -> str:
        return f"https://github.com/Polymarket/{self.repo}/issues/{self.number}"

    @property
    def slug(self) -> str:
        return f"{self.repo}#{self.number}"

    def age_days(self, today: date | None = None) -> int:
        return ((today or date.today()) - self.opened).days

    def summarize(self, today: date | None = None) -> str:
        parts = [self.slug, f"open {self.age_days(today)}d"]
        if self.comments:
            parts.append(f"{self.comments} affected")
        return " · ".join(parts)


# The 49-issue cluster. Every one of these is the same root cause: V2 requires
# the deposit-wallet flow (Poly1271 + ERC-7739 nested TypedDataSign), the Python
# and TypeScript SDKs can't produce that signature, so /auth succeeds and every
# POST /order is rejected. #111 has the evidence matrix across all three SDKs.
SIGNER_IDENTITY_MISMATCH = KnownIssue(
    repo="py-clob-client-v2",
    number=70,
    title="POLY_1271 (sig type 3) order placement fails: L1 auth always binds "
          "API key to EOA, never the deposit wallet",
    opened=date(2026, 5, 19),
    comments=44,
    workaround="rs-clob-client-v2 with SignatureType::Poly1271 places orders on "
               "this flow today; it's the only client that emits the ERC-7739 "
               "wrapping the deposit wallet's isValidSignature expects.",
)

DEPOSIT_WALLET_REQUIRED = KnownIssue(
    repo="py-clob-client-v2",
    number=111,
    title="Root cause for 'maker address not allowed': V2 requires the "
          "deposit-wallet flow (Poly1271 + ERC-7739 nested signing)",
    opened=date(2026, 8, 10),
    comments=0,
    workaround="Fund and sign from the deployed deposit wallet, not the EOA or "
               "a derived Safe.",
)

API_KEY_CREATION_BLOCKED = KnownIssue(
    repo="py-clob-client-v2",
    number=87,
    title="create_or_derive_api_key() returns 400 with signature_type=3 — "
          "API key resolves to EOA not deposit wallet",
    opened=date(2026, 6, 2),
    comments=4,
)

CLOUDFLARE_BLOCKS_KEY_CREATION = KnownIssue(
    repo="py-clob-client-v2",
    number=41,
    title="Cloudflare blocks API key creation (403) with py_clob_client_v2",
    opened=date(2026, 5, 1),
    comments=11,
    workaround="Retry from a residential IP or set a browser-like User-Agent; "
               "datacenter ranges get challenged.",
)

ORDER_VERSION_MISMATCH = KnownIssue(
    repo="py-clob-client-v2",
    number=32,
    title="order_version_mismatch on all orders post April 28 cutover",
    opened=date(2026, 4, 28),
    comments=11,
    workaround="v1 clients can't sign V2 orders. Move to a v2 client.",
)

BALANCE_READS_ZERO = KnownIssue(
    repo="py-clob-client-v2",
    number=105,
    title="get_balance_allowance() returns balance=0, allowance=0 for "
          "signature_type=1 despite funder wallet holding real pUSD",
    opened=date(2026, 8, 1),
    comments=0,
    workaround="Don't gate order placement on this read. UI deposits sit on an "
               "internal ledger the collateral probe doesn't see.",
)

HMAC_BODY_SERIALIZATION = KnownIssue(
    repo="py-clob-client-v2",
    number=108,
    title="Dict bodies with booleans or None produce a different HMAC",
    opened=date(2026, 8, 5),
    comments=0,
    workaround="Serialize the body once and sign exactly those bytes. Python's "
               "True/None don't match JSON's true/null.",
)

FINE_TICK_REJECTED = KnownIssue(
    repo="py-clob-client-v2",
    number=99,
    title="Market orders on fine-tick markets (0.001) are always rejected: "
          "taker signed with 5 decimals",
    opened=date(2026, 7, 4),
    comments=4,
)

WEBSOCKET_STREAM_STOPS = KnownIssue(
    repo="real-time-data-client",
    number=26,
    title="WebSocket data stream stops after some time",
    opened=date(2026, 1, 17),
    comments=18,
)

V1_SDK_ARCHIVED = KnownIssue(
    repo="py-clob-client",
    number=335,
    title="order_version_mismatch on order creation after clob v2 migration",
    opened=date(2026, 4, 28),
    comments=2,
    workaround="py-clob-client was archived 2026-05-25 and is read-only. Its "
               "issue tracker can't take new reports.",
)

ALL: tuple[KnownIssue, ...] = (
    SIGNER_IDENTITY_MISMATCH,
    DEPOSIT_WALLET_REQUIRED,
    API_KEY_CREATION_BLOCKED,
    CLOUDFLARE_BLOCKS_KEY_CREATION,
    ORDER_VERSION_MISMATCH,
    BALANCE_READS_ZERO,
    HMAC_BODY_SERIALIZATION,
    FINE_TICK_REJECTED,
    WEBSOCKET_STREAM_STOPS,
    V1_SDK_ARCHIVED,
)
