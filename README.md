# polymarket-doctor

Find out why your Polymarket integration won't place orders, before you write
the trading code.

The V2 exchange has a failure mode that costs people weeks: authentication
succeeds, every read endpoint works, and `POST /order` is rejected every single
time with an error that points nowhere near the cause. As of 2026-08-15, **49 of
the ~118 open issues** across `py-clob-client-v2`, `clob-client-v2` and
`rs-clob-client-v2` are that one bug. The biggest thread
([py-clob-client-v2#70](https://github.com/Polymarket/py-clob-client-v2/issues/70))
has 44 comments and has been open since May.

This runs the checks that would have caught it in about four seconds.

```console
$ polymarket-doctor onboard --address 0x9F49…6792

Polymarket Integration Doctor    https://clob.polymarket.com · protocol v2

 0  environment
    ✓ https://clob.polymarket.com responding  261ms
    ✓ clock within 0.3s of the exchange  174ms
    ✓ protocol 2  171ms

 1  identity
    ✓ signer and funder are both 0x9F49…6792
    ✗ 0x9F49…6792 is an EOA with no deposit wallet deployed
      ╭──────────────────────────────────────────────────────────────────────╮
      │ The V2 exchange rejects EOA-funded orders with "maker address not    │
      │ allowed, please use the deposit wallet flow". Authentication still   │
      │ succeeds, which is why this reads as a signing bug.                  │
      │                                                                      │
      │ Fix: Deploy a deposit wallet, fund it, and pass it as --funder.      │
      │                                                                      │
      │ Known issue: py-clob-client-v2#111 · open 5d                         │
      │ https://github.com/Polymarket/py-clob-client-v2/issues/111           │
      ╰──────────────────────────────────────────────────────────────────────╯
```

Every failure names the open issue it maps to, how long it's been open, and how
many people are behind you on it.

## Install

```bash
pip install polymarket-doctor
```

## Use

An address is enough to get through stages 0 and 1:

```bash
polymarket-doctor onboard --address 0xYourWallet
```

Add the deposit wallet if collateral lives somewhere other than the signer:

```bash
polymarket-doctor onboard --address 0xYourEOA --funder 0xYourDepositWallet
```

L2 credentials unlock the key-binding check, which is the one most people need.
Prefer the environment over flags so secrets stay out of your shell history:

```bash
export POLYMARKET_API_KEY=...
export POLYMARKET_API_SECRET=...
export POLYMARKET_API_PASSPHRASE=...
polymarket-doctor onboard --address 0xYourEOA --funder 0xYourDepositWallet
```

Run a single gate and it pulls in whatever it depends on:

```bash
polymarket-doctor check auth.key-identity
```

Exit code is 1 on a blocking failure and 0 otherwise, so it drops into CI. A
warning never fails the run.

## What it checks

**Stage 0 — environment**

- CLOB is reachable, and a 403 at the edge is called out as a bot challenge
  rather than an auth problem
- Protocol version, so a V1 host doesn't silently produce `order_version_mismatch`
- Clock drift against the exchange. `POLY_TIMESTAMP` is validated server-side,
  and a drifting host produces 401s that look random
- Which SDK is installed, including whether it's the archived v1

**Stage 1 — identity**

- Address format and checksum
- Whether the funder is a deployed deposit wallet or a bare EOA, asked directly
  of the relayer
- **Whether your SDK can sign for that account at all.** Orders from a deposit
  wallet need Poly1271 with ERC-7739 nested `TypedDataSign`. The Python and
  TypeScript clients don't emit it; only `rs-clob-client-v2` does

**Stage 2 — auth**

- Credentials are present and the secret is valid url-safe base64
- Request bodies hash the way the server computes them. The SDK signs
  `str(body).replace("'", '"')`, which is Python's repr, not JSON — the moment a
  bool or `None` appears the digests diverge, and since GETs have no body this
  looks like a credentials problem
- **The API key is bound to the same address your orders name as signer.** These
  differ whenever L1 auth signed as the EOA while orders name the deposit wallet,
  and they can never converge

Stages 3 through 7 — funding, market limits, order dry-run, WebSocket, RFQ — are
not implemented yet. The tool says so rather than reporting a clean run, because
a green stage 2 is not clearance to trade.

## What it will not do

- **It never places, cancels, or modifies an order.** Stages 0 through 2 send
  four GETs and nothing else
- **It never asks for a private key.** Everything here works from an address plus
  L2 credentials
- **It never prints your secret.** The passphrase and secret are redacted
  everywhere, the API key is masked, and there's a test that fails if either
  leaks into a finding
- **It sends nothing anywhere.** No telemetry, no phone-home. The only hosts it
  talks to are Polymarket's

## Notes from the API

Things verified against production on 2026-08-15 that cost time to discover:

- `GET /time` returns unix **seconds** as a bare integer, not JSON. The timestamp
  on `GET /book` is **milliseconds**. `POLY_TIMESTAMP` is seconds
- RFQ is a separate service on `combos-rfq-api.polymarket.com`. Paths under
  `clob.polymarket.com/rfq` 404 through nginx
- `clob-v2.polymarket.com` appears in `rs-clob-client-v2`'s README. The CLOB is
  served from `clob.polymarket.com`, which already answers `/version` with `2`
- `GET /balance-allowance` can report `balance: 0` for a funded account, because
  UI deposits sit on an internal ledger it doesn't see. Don't gate order
  placement on it
- Minimum order is 5 outcome tokens regardless of notional, and tick size varies
  per market between 0.01 and 0.001

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

Checks declare the facts they read and write; the registry topologically sorts
them, so ordering is derived rather than hand-maintained and `check <id>` can
pull in prerequisites on its own. Adding a check means subclassing `Check`,
declaring `reads`/`writes`, and registering it in `checks/__init__.py`.

## License

MIT
