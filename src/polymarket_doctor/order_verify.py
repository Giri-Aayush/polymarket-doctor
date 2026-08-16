"""Verify a signed order the way the exchange will, without sending it.

`onboard` checks that your account and connection are set up correctly.
This checks the other half: that the signed order *your code* produced is one
the exchange will accept. It answers "would POST /order take this?" by running
the same validations the server runs — signature recovery, tick grid, base-unit
math, the signer/maker relationship — against an order you hand it, and never
posts anything.

The EIP-712 domain, order struct, exchange addresses, and per-type signing
behavior are mirrored from py-clob-client-v2 (order_utils/, config.py) as read
on 2026-08-16. Cross-checked so a signature this module accepts is one the V2
exchange accepts.

Signature types:
  0 EOA / 1 POLY_PROXY / 2 POLY_GNOSIS_SAFE — ECDSA over the order's EIP-712
    hash, signed by the signer EOA. Recoverable here: the recovered address
    must equal the order's `signer`.
  3 POLY_1271 — an EIP-1271 contract signature (ERC-7739 nested). It can only
    be verified by the maker contract's isValidSignature on-chain, so this
    module validates its structure and says so rather than pretending to
    recover it. This is the same #70/#111 wall the identity stage classifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import IntEnum

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import is_address, to_checksum_address

from .core.check import Finding, Severity

# --- constants mirrored from py-clob-client-v2 (Polygon mainnet, chain 137) ---
CHAIN_ID = 137
DOMAIN_NAME = "Polymarket CTF Exchange"
DOMAIN_VERSION = "2"
EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"

# USDC / outcome tokens are 6-decimal; wire amounts are integer base units.
BASE_UNIT = Decimal(10) ** 6
MIN_SIZE_TOKENS = Decimal(5)

ORDER_TYPE = [
    {"name": "salt", "type": "uint256"},
    {"name": "maker", "type": "address"},
    {"name": "signer", "type": "address"},
    {"name": "tokenId", "type": "uint256"},
    {"name": "makerAmount", "type": "uint256"},
    {"name": "takerAmount", "type": "uint256"},
    {"name": "side", "type": "uint8"},
    {"name": "signatureType", "type": "uint8"},
    {"name": "timestamp", "type": "uint256"},
    {"name": "metadata", "type": "bytes32"},
    {"name": "builder", "type": "bytes32"},
]


class SignatureType(IntEnum):
    EOA = 0
    POLY_PROXY = 1
    POLY_GNOSIS_SAFE = 2
    POLY_1271 = 3


class Side(IntEnum):
    BUY = 0
    SELL = 1


@dataclass(frozen=True, slots=True)
class OrderCheck:
    """One verification result. Mirrors Finding so the CLI renders it uniformly."""

    name: str
    finding: Finding


def verify_order(order: dict, *, neg_risk: bool = False,
                 tick_size: Decimal | None = None) -> list[OrderCheck]:
    """Run every offline check the exchange would, in the order failures cascade.

    `order` is the wire order object — the value of the "order" key POST /order
    receives, or that object on its own. `tick_size` when known lets the grid
    check be exact; without it the grid is inferred from the price's decimals.
    """
    order = order.get("order", order) if isinstance(order, dict) else order
    checks: list[OrderCheck] = []

    def record(name: str, finding: Finding) -> Finding:
        checks.append(OrderCheck(name, finding))
        return finding

    # 1. Shape — every field the struct needs must be present and typed right.
    missing = [f["name"] for f in ORDER_TYPE if f["name"] not in order]
    if "signature" not in order:
        missing.append("signature")
    if missing:
        record("shape", Finding.fail(
            f"order is missing required fields: {', '.join(missing)}",
            detail="Expected the wire order struct POST /order receives.",
        ))
        return checks
    record("shape", Finding.ok("all struct fields present"))

    # 2. Enums and addresses.
    try:
        side = Side(int(order["side"]) if str(order["side"]).isdigit()
                    else Side[str(order["side"]).upper()].value)
    except (KeyError, ValueError):
        record("side", Finding.fail(f"side {order['side']!r} is not BUY or SELL"))
        return checks
    record("side", Finding.ok(f"side {side.name}"))

    try:
        sig_type = SignatureType(int(order["signatureType"]))
    except ValueError:
        record("signature-type", Finding.fail(
            f"signatureType {order['signatureType']!r} is not 0-3"))
        return checks

    maker = _checksum(order["maker"])
    signer = _checksum(order["signer"])
    if maker is None or signer is None:
        record("addresses", Finding.fail("maker or signer is not a valid address"))
        return checks

    # 3. Signer / maker relationship. EOA orders sign for themselves; the proxy,
    #    safe, and 1271 flows have the EOA sign while the funder is the maker.
    if sig_type is SignatureType.EOA and maker != signer:
        record("identity", Finding.fail(
            "EOA order (signatureType 0) has maker != signer",
            detail=f"maker {_short(maker)} and signer {_short(signer)} must "
                   "match for a self-signed EOA order.",
            remedy="Use signatureType 1/2/3 for funder-backed orders, or set "
                   "maker == signer.",
        ))
    else:
        record("identity", Finding.ok(
            f"maker {_short(maker)}, signer {_short(signer)}"))

    # 4. Amounts and price grid.
    amount_checks = _check_amounts(order, side, tick_size)
    checks.extend(amount_checks)

    # 5. Signature — only meaningful once the numbers are well-formed integers.
    #    A fractional amount can't even be hashed, so recovery would raise; the
    #    amounts finding above is the real diagnosis in that case.
    if any(c.finding.severity is Severity.FAIL and c.name == "amounts"
           for c in amount_checks):
        record("signature", Finding.warn(
            "signature not checked — fix the amounts first",
            detail="The order's amounts aren't integer base units, so it can't "
                   "be hashed the way the exchange would.",
        ))
        return checks

    record("signature", _check_signature(order, sig_type, maker, signer, neg_risk))
    return checks


def _check_amounts(order: dict, side: Side, tick_size: Decimal | None) -> list[OrderCheck]:
    try:
        maker_amt = Decimal(str(order["makerAmount"]))
        taker_amt = Decimal(str(order["takerAmount"]))
    except (InvalidOperation, KeyError):
        return [OrderCheck("amounts", Finding.fail("maker/taker amounts are not integers"))]

    if maker_amt <= 0 or taker_amt <= 0 or maker_amt % 1 or taker_amt % 1:
        return [OrderCheck("amounts", Finding.fail(
            "maker/taker amounts must be positive integer base units",
            detail=f"got makerAmount={maker_amt}, takerAmount={taker_amt}. "
                   "A fractional base unit is the classic float-rounding bug.",
        ))]

    # BUY spends USDC (maker) for tokens (taker); SELL is the reverse.
    if side is Side.BUY:
        price = maker_amt / taker_amt
        size_tokens = taker_amt / BASE_UNIT
    else:
        price = taker_amt / maker_amt
        size_tokens = maker_amt / BASE_UNIT

    out: list[OrderCheck] = []
    if not (Decimal(0) < price < Decimal(1)):
        out.append(OrderCheck("price", Finding.fail(
            f"implied price {price} is not strictly between 0 and 1")))
    else:
        grid = _grid_finding(price, tick_size)
        out.append(OrderCheck("price", grid))

    if size_tokens < MIN_SIZE_TOKENS:
        out.append(OrderCheck("size", Finding.fail(
            f"size {size_tokens} is below the 5-token minimum",
            detail="Every market rejects orders under 5 outcome tokens "
                   "regardless of notional.")))
    else:
        out.append(OrderCheck("size", Finding.ok(f"size {size_tokens} tokens")))
    return out


def _grid_finding(price: Decimal, tick_size: Decimal | None) -> Finding:
    if tick_size is not None:
        on_grid = (price / tick_size) % 1 == 0
        if on_grid:
            return Finding.ok(f"price {price} on the {tick_size} tick grid")
        return Finding.fail(
            f"price {price} is off the {tick_size} tick grid",
            detail="An off-grid price is rejected. On 0.001-tick markets this "
                   "is usually a taker amount computed with 5 decimals instead "
                   "of 6.",
        )
    # No tick supplied: report the finest grid the price sits on, and flag the
    # 0.001 case where the decimal-count bug lives.
    decimals = -price.normalize().as_tuple().exponent
    return Finding.warn(
        f"price {price} needs {decimals} decimals; pass --token to check it "
        "against the market's real tick",
        detail="Without the market's tick this can't confirm the grid; a "
               "price needing more than 2 decimals implies a 0.001 market, "
               "where the taker-amount decimal count matters.",
    )


def _check_signature(order: dict, sig_type: SignatureType, maker: str,
                     signer: str, neg_risk: bool) -> Finding:
    if sig_type is SignatureType.POLY_1271:
        # A contract signature. ecrecover doesn't apply; only the maker's
        # on-chain isValidSignature can judge it. Not a failure — just the
        # boundary of what's checkable offline.
        return Finding.warn(
            "signatureType 3 (POLY_1271) can't be verified offline",
            detail="This is an EIP-1271 contract signature (ERC-7739 nested). "
                   "It's valid only if the maker contract's isValidSignature "
                   "accepts it on-chain — which is exactly why the Python SDK "
                   "can't produce one (py-clob-client-v2#70/#111). Structure "
                   "checks passed; signature validity needs the deposit wallet.",
        )

    verifying_contract = NEG_RISK_EXCHANGE_V2 if neg_risk else EXCHANGE_V2
    typed = {
        "primaryType": "Order",
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": ORDER_TYPE,
        },
        "domain": {
            "name": DOMAIN_NAME,
            "version": DOMAIN_VERSION,
            "chainId": CHAIN_ID,
            "verifyingContract": verifying_contract,
        },
        "message": {
            "salt": int(order["salt"]),
            "maker": maker,
            "signer": signer,
            "tokenId": int(order["tokenId"]),
            "makerAmount": int(order["makerAmount"]),
            "takerAmount": int(order["takerAmount"]),
            "side": int(order["side"]) if str(order["side"]).isdigit()
            else Side[str(order["side"]).upper()].value,
            "signatureType": int(order["signatureType"]),
            "timestamp": int(order["timestamp"]),
            "metadata": _bytes32(order["metadata"]),
            "builder": _bytes32(order["builder"]),
        },
    }

    try:
        recovered = Account.recover_message(
            encode_typed_data(full_message=typed), signature=order["signature"])
    except Exception as exc:  # noqa: BLE001 - a bad signature is the finding
        return Finding.fail(
            "signature could not be recovered",
            detail=f"{type(exc).__name__}: {exc}. The signature isn't a valid "
                   "ECDSA signature over this order's EIP-712 hash.",
            remedy=f"Confirm you signed against verifyingContract "
                   f"{verifying_contract} "
                   f"({'neg-risk' if neg_risk else 'standard'} exchange).",
        )

    if to_checksum_address(recovered) != signer:
        return Finding.fail(
            "signature recovers to the wrong address",
            detail=f"Recovered {_short(recovered)}, but the order names "
                   f"{_short(signer)} as signer. The signature is valid but "
                   "over a different key or a different order — a mismatched "
                   "verifyingContract (neg-risk vs standard) does exactly this.",
            remedy="Sign with the signer's key against the matching exchange "
                   "contract; pass --neg-risk for neg-risk markets.",
        )

    return Finding.ok(
        f"signature recovers to signer {_short(signer)}",
        detail="The order is signed correctly for the "
               f"{'neg-risk' if neg_risk else 'standard'} exchange.",
    )


def _checksum(address: str) -> str | None:
    if not isinstance(address, str) or not is_address(address.strip()):
        return None
    return to_checksum_address(address.strip())


def _bytes32(value: str) -> bytes:
    return bytes.fromhex(str(value).replace("0x", "").zfill(64))


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"
