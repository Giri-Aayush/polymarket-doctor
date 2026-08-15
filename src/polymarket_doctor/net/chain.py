"""Minimal Polygon reads.

Only three calls, all `eth_call` against the funder, because the thing we need
to know isn't in any Polymarket API: whether a funder contract is a Gnosis Safe
or a newer deposit wallet. That determines the signature type, and picking the
wrong one is the single biggest source of rejected orders.

The relayer can't answer it. `GET /deployed?type=SAFE` and `type=WALLET` return
the same thing for both kinds, and both wallet kinds share identical proxy
bytecode and the same implementation slot. What separates them is whether the
implementation answers the Safe interface.

Default RPC is a public node. Reads only, no keys, and a partner can point at
their own with --rpc.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .http import HttpProbe

DEFAULT_RPC = "https://polygon-bor-rpc.publicnode.com"

# Selectors we probe on the funder.
SELECTOR_VERSION = "0xffa1ad74"      # VERSION()
SELECTOR_GET_OWNERS = "0xa0e67e2b"   # getOwners()


@dataclass(frozen=True, slots=True)
class ContractProfile:
    """What we could learn about an address on chain."""

    has_code: bool
    safe_version: str | None = None
    owners: tuple[str, ...] = ()

    @property
    def is_gnosis_safe(self) -> bool:
        return self.safe_version is not None

    def is_owner(self, address: str) -> bool:
        return address.lower() in {owner.lower() for owner in self.owners}


class ChainReader:
    """Just enough JSON-RPC to classify a funder."""

    def __init__(self, probe: HttpProbe, rpc_url: str = DEFAULT_RPC) -> None:
        self._probe = probe
        self._rpc = rpc_url

    def profile(self, address: str) -> ContractProfile | None:
        """None means the RPC was unusable, which is different from an EOA."""
        code = self._call("eth_getCode", [address, "latest"])
        if code is None:
            return None
        if code in ("0x", ""):
            return ContractProfile(has_code=False)

        return ContractProfile(
            has_code=True,
            safe_version=_decode_string(self._eth_call(address, SELECTOR_VERSION)),
            owners=_decode_address_array(self._eth_call(address, SELECTOR_GET_OWNERS)),
        )

    def _eth_call(self, to: str, data: str) -> str | None:
        return self._call("eth_call", [{"to": to, "data": data}, "latest"])

    def _call(self, method: str, params: list[Any]) -> str | None:
        response = self._probe.post(
            self._rpc,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if not response.ok or not isinstance(response.body, dict):
            return None
        # A revert is a legitimate answer here: it means the contract doesn't
        # implement that selector, which is exactly what we're testing for.
        if "error" in response.body:
            return None
        result = response.body.get("result")
        return result if isinstance(result, str) else None


def _decode_string(word: str | None) -> str | None:
    """ABI-decode a returned string. None on a revert or anything unexpected."""
    if not word or len(word) < 130:
        return None
    body = word[2:]
    try:
        length = int(body[64:128], 16)
        raw = bytes.fromhex(body[128:128 + length * 2])
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _decode_address_array(word: str | None) -> tuple[str, ...]:
    if not word or len(word) < 130:
        return ()
    body = word[2:]
    try:
        count = int(body[64:128], 16)
    except ValueError:
        return ()
    addresses = []
    for index in range(count):
        start = 128 + index * 64
        chunk = body[start:start + 64]
        if len(chunk) < 64:
            break
        addresses.append("0x" + chunk[24:])
    return tuple(addresses)
