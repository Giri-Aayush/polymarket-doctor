"""Stage 3 — can the exchange see your collateral?

The trap here runs opposite to intuition: zero does not mean unfunded. The
collateral probe reads on-chain ERC-20 state, but UI deposits sit on an internal
ledger it never consults, so genuinely funded accounts read balance=0 and
allowance=0 (py-clob-client-v2#105). A partner who gates order placement on this
read locks themselves out of a working account. Zero therefore warns with the
citation and never fails.

Verified against production on 2026-08-15: unauthenticated
GET /balance-allowance?asset_type=COLLATERAL on clob.polymarket.com returns
401 {"error": "Unauthorized/Invalid api key"} — a 401 here is a credentials
problem, not a funding one.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .. import issues, signing
from ..core.check import Check, Finding, Severity, Stage
from ..core.context import Context
from ..core.facts import Fact
from ..net.http import Response

BALANCE_PATH = "/balance-allowance"


class CollateralBalance(Check):
    """Read the collateral balance the exchange is willing to report."""

    id = "funding.collateral"
    stage = Stage.FUNDING
    title = "Collateral visible to the exchange"
    reads = frozenset({
        Fact.HAS_L2_CREDENTIALS,
        Fact.SIGNER_ADDRESS,
        Fact.SIGNATURE_TYPE,
        Fact.CLOCK_SKEW_SECONDS,
    })
    writes = frozenset({Fact.COLLATERAL_BALANCE, Fact.EXCHANGE_APPROVALS})

    def run(self, ctx: Context) -> Finding:
        if not ctx.facts.get(Fact.HAS_L2_CREDENTIALS):
            self._record(ctx, None, None)
            return Finding(
                Severity.SKIP,
                "skipped, no credentials to read a balance with",
                detail="Supply L2 credentials to check what the exchange sees.",
            )

        response = self._fetch(ctx)

        if response.status == 401:
            self._record(ctx, None, None)
            return Finding.fail(
                "credentials rejected reading the balance",
                detail=f"GET {BALANCE_PATH} returned 401 "
                       f"({response.error_message() or 'no error body'}). Stage 2 "
                       "vets these same credentials, so either that stage was "
                       "skipped or the key changed mid-run.",
                remedy="Re-run the full suite so stage 2 can name the actual "
                       "credential problem.",
            )

        if not response.ok:
            self._record(ctx, None, None)
            return Finding.fail(
                "could not read the balance",
                detail=response.error or f"GET {BALANCE_PATH} returned {response.status}",
            )

        balance = _parse_balance(response.body)
        approvals = _approvals(response.body)
        self._record(ctx, balance, approvals)

        if balance is None:
            return Finding.warn(
                "balance response had no readable balance field",
                detail=f"GET {BALANCE_PATH} answered 200 but the body carried no "
                       "integer balance. Treat funding as unverified.",
                body=response.body,
            )

        if balance == 0:
            # Never a FAIL: this read misses the internal ledger UI deposits
            # land on, so zero is the documented answer for funded accounts.
            return Finding.warn(
                "exchange reports zero collateral, which is often wrong for "
                "funded accounts",
                detail=(
                    "This endpoint reads on-chain ERC-20 state. Deposits made "
                    "through the UI sit on an internal ledger the collateral "
                    "probe does not see, so genuinely funded accounts read 0 "
                    "here (py-clob-client-v2#105). Do not gate order placement "
                    "on this read — submit the order and let the exchange "
                    "answer for itself."
                ),
                remedy="If you deposited through the UI, ignore this reading. "
                       "Confirm funding with the stage 5 order dry run instead.",
                issue=issues.BALANCE_READS_ZERO,
                balance=0,
                approvals=approvals,
            )

        return Finding.ok(
            f"exchange sees {_usdc(balance)} of collateral",
            balance_raw=balance,
            approvals=approvals,
        )

    @staticmethod
    def _fetch(ctx: Context) -> Response:
        creds = ctx.credentials
        assert creds is not None  # HAS_L2_CREDENTIALS

        # Present the signer: L2 keys identify the signing EOA per Polymarket
        # on ts-sdk#73, and stage 2 already failed the run if that's not true.
        # Skew subtracted so a drifting host stays stage 0's finding, not a 401.
        skew = ctx.facts.get(Fact.CLOCK_SKEW_SECONDS) or 0
        headers = signing.build_l2_headers(
            address=ctx.facts.get(Fact.SIGNER_ADDRESS),
            api_key=creds.api_key,
            secret=creds.secret,
            passphrase=creds.passphrase,
            timestamp=int(time.time() - skew),
            method="GET",
            # The SDK signs the bare path; the query string stays out of the
            # digest. Folding it in would 401 against a correct key.
            request_path=BALANCE_PATH,
        )

        params: dict[str, Any] = {"asset_type": "COLLATERAL"}
        sig_type = ctx.facts.get(Fact.SIGNATURE_TYPE)
        if sig_type is not None:
            # Which ledger the server consults depends on the signature type,
            # so omitting it when known is another way to read a spurious zero.
            params["signature_type"] = int(sig_type)

        return ctx.probe.get(ctx.endpoints.clob_url(BALANCE_PATH),
                             headers=headers, params=params)

    @staticmethod
    def _record(ctx: Context, balance: int | None,
                approvals: dict[str, Any] | None) -> None:
        """Both facts land on every path — downstream stages read None as
        "unknown", never as "the funding stage didn't run"."""
        ctx.facts.set(Fact.COLLATERAL_BALANCE, balance)
        ctx.facts.set(Fact.EXCHANGE_APPROVALS, approvals)


def _parse_balance(body: Any) -> int | None:
    if not isinstance(body, Mapping):
        return None
    try:
        return int(body.get("balance"))
    except (TypeError, ValueError):
        return None


def _approvals(body: Any) -> dict[str, Any] | None:
    """Older SDK responses carry a single "allowance", newer ones a dict of
    spender allowances. Normalize to a dict either way."""
    if not isinstance(body, Mapping):
        return None
    allowances = body.get("allowances")
    if isinstance(allowances, Mapping):
        return dict(allowances)
    if "allowance" in body:
        return {"exchange": body["allowance"]}
    return None


def _usdc(raw: int) -> str:
    # Collateral is 6-decimal, same as USDC.
    return f"{raw / 1_000_000:,.2f} USDC"


CHECKS = (CollateralBalance(),)
