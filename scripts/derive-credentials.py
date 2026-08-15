#!/usr/bin/env python3
"""Derive Polymarket L2 API credentials from your own wallet, locally.

polymarket-doctor never touches a private key — this helper is the one
deliberate exception, split out into scripts/ so the boundary stays visible.
It signs the ClobAuth EIP-712 message with the key in POLYMARKET_PRIVATE_KEY,
asks the CLOB to create-or-derive credentials, and prints the three export
lines the doctor reads. The key is read from the environment, used for one
signature, and never written anywhere.

Usage:
    pip install py-clob-client-v2
    export POLYMARKET_PRIVATE_KEY=0x...   # the EOA that controls your account
    eval "$(python scripts/derive-credentials.py)"
    polymarket-doctor onboard --address 0xYourEOA --funder 0xYourFunder

The credentials come back bound to the EOA that signed — that is the intended
model (L1/L2 identity is the signer; orders execute from the funder; see
ts-sdk#73). Deriving is idempotent: the same key and nonce return the same
credentials, so re-running is safe.

Everything except the export lines goes to stderr, so eval "$(...)" only ever
evaluates the three exports.
"""

from __future__ import annotations

import os
import sys

HOST = "https://clob.polymarket.com"
POLYGON = 137


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        fail(
            "POLYMARKET_PRIVATE_KEY is not set.\n"
            "  export POLYMARKET_PRIVATE_KEY=0x...  (never pass it as a flag —\n"
            "  flags land in shell history; the env var stays in this process)"
        )
    if not key.startswith("0x"):
        # Some wallet exports omit the prefix, and the failure that causes is
        # silent and far away from here.
        fail("private key must start with 0x — wallet exports sometimes drop it")

    try:
        from py_clob_client_v2 import ClobClient
    except ImportError:
        fail(
            "py-clob-client-v2 is not installed in this environment.\n"
            "  pip install py-clob-client-v2\n"
            "It is only needed by this helper, not by polymarket-doctor."
        )

    client = ClobClient(host=HOST, chain_id=POLYGON, key=key)
    address = client.get_address()
    print(f"deriving credentials for {address} against {HOST} …", file=sys.stderr)

    try:
        creds = client.create_or_derive_api_key()
    except Exception as exc:  # noqa: BLE001 - one shot, report and stop
        fail(
            f"the CLOB rejected the request: {exc}\n"
            "A 403 here is usually Cloudflare challenging a datacenter IP\n"
            "(py-clob-client-v2#41) — retry from a residential connection."
        )

    print("done. credentials are bound to the address above.", file=sys.stderr)
    print(f'export POLYMARKET_ADDRESS="{address}"')
    print(f'export POLYMARKET_API_KEY="{creds.api_key}"')
    print(f'export POLYMARKET_API_SECRET="{creds.api_secret}"')
    print(f'export POLYMARKET_API_PASSPHRASE="{creds.api_passphrase}"')


if __name__ == "__main__":
    main()
