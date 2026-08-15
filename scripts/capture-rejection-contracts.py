#!/usr/bin/env python3
"""Capture the exchange's rejection contracts, live, without risking a fill.

The doctor cites specific rejection strings ("maker address not allowed, please
use the deposit wallet flow", "invalid l2 address header"). This script proves
them against production. It is human-run, credentialed, and built so nothing it
sends can ever execute:

- The order probe posts a real, signed order whose maker is the throwaway EOA
  the credentials belong to. That account holds zero collateral and has no
  deposit wallet, and the V2 exchange rejects the EOA flow at the gate — the
  entire #52/#53 issue cluster documents it. Refused at the door, and nothing
  behind the door to trade with even if it weren't.
- The RFQ probe posts an empty JSON body to the maker quotes endpoint with
  valid L2 headers. A payload with no quote in it cannot become a quote; what
  comes back is the authenticated validation error, which nobody has written
  down anywhere.

If either probe reports success:true, that is not a win — it is a critical
finding about the exchange, and the output says so.

Usage (same shell where derive-credentials.py ran):
    .venv/bin/python scripts/capture-rejection-contracts.py

Needs POLYMARKET_PRIVATE_KEY plus the three POLYMARKET_API_* variables.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time

import httpx

CLOB = "https://clob.polymarket.com"
RFQ = "https://combos-rfq-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
POLYGON = 137


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        fail(f"{name} is not set — run derive-credentials.py first, same shell")
    return value


def cheapest_live_market() -> tuple[str, str, bool]:
    """Token id, tick size, neg_risk for the top-volume open market."""
    body = httpx.get(
        f"{GAMMA}/markets",
        params={"closed": "false", "limit": 1, "order": "volumeNum", "ascending": "false"},
        timeout=15,
    ).json()
    market = body[0]
    token = json.loads(market["clobTokenIds"])[0]
    return token, str(market["orderPriceMinTickSize"]), bool(market.get("negRisk"))


def probe_order(key: str, creds) -> dict:
    """Signed EOA order from the unfunded throwaway. Expected: refused."""
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import (
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
    )
    from py_clob_client_v2.order_builder.constants import BUY

    token, tick, neg_risk = cheapest_live_market()
    client = ClobClient(host=CLOB, chain_id=POLYGON, key=key, creds=creds)
    maker = client.get_address()

    # Smallest order the grid allows: 5 shares at one tick. Notional at a
    # 0.001 tick is half a cent — and the maker holds zero collateral anyway.
    args = OrderArgs(token_id=token, price=float(tick), size=5, side=BUY)
    options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)

    result: dict = {"probe": "order", "maker": maker, "token": token[:8] + "…",
                    "price": tick, "size": 5}
    try:
        response = client.create_and_post_order(args, options, OrderType.GTC)
        result["accepted"] = bool(isinstance(response, dict) and response.get("success"))
        result["response"] = response
    except Exception as exc:  # noqa: BLE001 - the rejection IS the data
        result["accepted"] = False
        result["rejection"] = _extract_error(str(exc))
    return result


def probe_rfq_quote(address: str, api_key: str, secret: str, passphrase: str) -> dict:
    """Authenticated POST of an empty body. A non-quote cannot become a quote."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from polymarket_doctor import signing

    path = "/v1/maker/quotes"
    headers = dict(signing.build_l2_headers(
        address=address, api_key=api_key, secret=secret, passphrase=passphrase,
        timestamp=int(time.time()), method="POST", request_path=path,
        body={},
    ))
    response = httpx.post(f"{RFQ}{path}", headers=headers, json={}, timeout=15)
    try:
        body = response.json()
    except ValueError:
        body = response.text[:200]
    return {"probe": "rfq-quote", "status": response.status_code,
            "accepted": response.status_code < 300, "response": body}


def _extract_error(text: str) -> str:
    match = re.search(r"error_message=(\{.*?\})", text)
    return match.group(1) if match else text[:300]


def main() -> None:
    from py_clob_client_v2.clob_types import ApiCreds

    key = require_env("POLYMARKET_PRIVATE_KEY")
    creds = ApiCreds(
        api_key=require_env("POLYMARKET_API_KEY"),
        api_secret=require_env("POLYMARKET_API_SECRET"),
        api_passphrase=require_env("POLYMARKET_API_PASSPHRASE"),
    )
    address = require_env("POLYMARKET_ADDRESS")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"# Rejection contracts, captured {stamp}\n")

    for result in (probe_order(key, creds),
                   probe_rfq_quote(address, creds.api_key, creds.api_secret,
                                   creds.api_passphrase)):
        print(json.dumps(result, indent=2, default=str))
        if result.get("accepted"):
            print("\n*** UNEXPECTED ACCEPTANCE — this is a critical finding, "
                  "not a success. If an order was placed, cancel it now: the "
                  "doctor's `cancel` guidance or the Polymarket UI. Then file "
                  "what happened. ***", file=sys.stderr)
    print("\nExpected shape: both probes refused, each with its service's own "
          "error vocabulary.", file=sys.stderr)


if __name__ == "__main__":
    main()
