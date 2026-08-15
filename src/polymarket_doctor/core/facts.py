"""The shared vocabulary checks use to talk to each other.

Checks don't call each other directly. A check publishes facts it learned and
declares the facts it needs; the registry works out the order from that. The
alternative — each check re-deriving the deposit wallet, re-fetching /version —
meant six round trips to answer one question and no way to run a single check in
isolation.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import Enum
from typing import Any


class Fact(str, Enum):
    """Everything one check can learn and another can depend on.

    Values are dotted strings so they read well in `--explain` output and in
    the JSON report, where they're the keys.
    """

    # Stage 0 — environment
    HOST = "env.host"
    PROTOCOL_VERSION = "env.protocol_version"
    CLOCK_SKEW_SECONDS = "env.clock_skew_seconds"
    SDK = "env.sdk"

    # Stage 1 — identity
    SIGNER_ADDRESS = "identity.signer_address"
    FUNDER_ADDRESS = "identity.funder_address"
    ACCOUNT_KIND = "identity.account_kind"
    SIGNATURE_TYPE = "identity.signature_type"
    DEPOSIT_WALLET_DEPLOYED = "identity.deposit_wallet_deployed"
    FUNDER_PROFILE = "identity.funder_profile"

    # Stage 2 — auth
    API_KEY_IDENTITY = "auth.api_key_identity"
    HAS_L2_CREDENTIALS = "auth.has_l2_credentials"

    # Stage 3 — funding
    COLLATERAL_BALANCE = "funding.collateral_balance"
    EXCHANGE_APPROVALS = "funding.exchange_approvals"

    # Stage 4 — market limits
    TOKEN_ID = "market.token_id"
    TICK_SIZE = "market.tick_size"
    NEG_RISK = "market.neg_risk"
    FEE_RATE_BPS = "market.fee_rate_bps"
    MIN_ORDER_SIZE = "market.min_order_size"

    # Stage 5 — order dry run
    ORDER_PAYLOAD_VALID = "order.payload_valid"

    # Stage 6 — websocket
    WS_CONNECTED = "ws.connected"

    # Stage 7 — rfq
    RFQ_REACHABLE = "rfq.reachable"


class FactStore:
    """Write-once key/value store threaded through a run.

    Write-once because two checks disagreeing about the funder address is a bug
    in the check graph, and I'd rather it surface as a loud error during tests
    than as a confusing report six stages later.
    """

    __slots__ = ("_values",)

    def __init__(self) -> None:
        self._values: dict[Fact, Any] = {}

    def set(self, fact: Fact, value: Any) -> None:
        if fact in self._values and self._values[fact] != value:
            raise ValueError(
                f"{fact.value} already set to {self._values[fact]!r}, "
                f"refusing to overwrite with {value!r}"
            )
        self._values[fact] = value

    def get(self, fact: Fact, default: Any = None) -> Any:
        return self._values.get(fact, default)

    def __contains__(self, fact: object) -> bool:
        return fact in self._values

    def __iter__(self) -> Iterator[Fact]:
        return iter(self._values)

    def as_mapping(self) -> Mapping[str, Any]:
        """Flatten for the JSON report. Enum keys become their dotted strings."""
        return {fact.value: value for fact, value in self._values.items()}
