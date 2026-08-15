# polymarket-doctor

Find out why your Polymarket integration won't place orders, before you write
the trading code.

The V2 exchange has a failure mode that costs people weeks: authentication
succeeds, every read endpoint works, and `POST /order` is rejected every single
time with an error that points nowhere near the cause. As of 2026-08-15, **49 of
the ~118 open issues** across `py-clob-client-v2`, `clob-client-v2` and
`rs-clob-client-v2` report it. The biggest thread
([py-clob-client-v2#70](https://github.com/Polymarket/py-clob-client-v2/issues/70))
has 44 comments and has been open since May.

For most of them the cause is a signature type that doesn't match what the
funder contract actually is, and you cannot tell what it is from any Polymarket
API. This finds out in about four seconds.

```console
$ polymarket-doctor onboard --address 0x9F49…6792 --funder 0x3EC7…d649

Polymarket Integration Doctor    https://clob.polymarket.com · protocol v2

 0  environment
    ✓ https://clob.polymarket.com responding  313ms
    ✓ clock within 0.7s of the exchange  181ms
    ✓ protocol 2  176ms

 1  identity
    ✓ signer 0x9F49…6792 funding 0x3EC7…d649
    ✓ funder is a Gnosis Safe 1.3.0, use signature_type=2
      The UI deploys this kind when the account was created with an
      external wallet rather than email or Google. Signing it as
      POLY_1271 (3) is the most common cause of "the order signer
      address has to be the address of the API KEY".
    ✓ 0x9F49…6792 is an owner of the Safe
```

That funder is the one from py-clob-client-v2#70, where 44 comments conclude the
SDK can't sign for it.

Every finding that maps to a known issue cites it, with its state and how many
people are on the thread.

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

L2 credentials add the auth and funding stages. If you don't have credentials
yet, `scripts/derive-credentials.py` derives them from your wallet — locally,
in your terminal, which is the one place a private key belongs. The doctor
itself never reads `POLYMARKET_PRIVATE_KEY`; only that helper does, for one
signature.

```bash
pip install py-clob-client-v2
export POLYMARKET_PRIVATE_KEY=0x...
creds="$(python scripts/derive-credentials.py)" && eval "$creds"
polymarket-doctor onboard --address 0xYourEOA --funder 0xYourDepositWallet
```

Already have credentials? Export them directly — prefer the environment over
flags so secrets stay out of your shell history:

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
- **Which signature type your funder actually needs.** This is the one that
  matters, and it's explained below
- Whether your signer is an owner of the funder. A Safe only honours its owners,
  and if yours isn't one, no signature type will work

**Stage 2 — auth**

- Credentials are present and the secret is valid url-safe base64
- Request bodies hash the way the server computes them. The SDK signs
  `str(body).replace("'", '"')`, which is Python's repr, not JSON — the moment a
  bool or `None` appears the digests diverge, and since GETs have no body this
  looks like a credentials problem
- Which address the credentials authenticate as, since that's the fact stage 1's
  advice depends on

**Stage 3 — funding**

- A signed read of `/balance-allowance`. A zero balance is a warning, never a
  failure: that endpoint reports 0 for genuinely funded accounts because UI
  deposits sit on an internal ledger it doesn't see (#105). Don't gate order
  placement on it

**Stage 4 — market limits**

- Resolves a market (yours via `--token`, or the highest-volume open one) and
  reads its tick size, neg-risk flag, and fee rate. On 0.001-tick books the
  finding carries the taker-decimal-count trap that rejects every market order
  computed with the coarse default (#99)

**Stage 5 — order dry run**

- Builds the exact order payload a client would sign — maker, price snapped to
  the tick grid, amounts in 6-decimal base units computed with `Decimal` — and
  validates every invariant the server enforces. Nothing is signed and nothing
  is sent; the only request is a GET of the book

**Stage 6 — websocket**

- Connects to the market channel, subscribes, and measures time to first frame.
  The passing finding carries the production caveat that matters: the stream is
  known to stop silently while the socket stays open (#26), so partners need
  last-frame staleness tracking, not connection liveness

**Stage 7 — RFQ**

- Reaches the RFQ gateway on its own host, counts combo markets, and documents
  the maker flow (quote → cancel → last-look confirm) plus the gateway's
  distinct error vocabulary. Quote submission is deliberately not exercised —
  it would place a real quote

## The signature type thing

Roughly 49 of the open issues across the v2 clients report the same error:

```
the order signer address has to be the address of the API KEY
```

The threads mostly conclude that the SDKs can't sign for deposit wallets and
that only the Rust client works.
[Polymarket's answer on ts-sdk#73](https://github.com/Polymarket/ts-sdk/issues/73)
is different: an API key authenticating the EOA while orders execute from the
funder is the intended model, and the reported failures were accounts sending
`signature_type=3` (POLY_1271) for a funder that is actually an older Gnosis Safe
and needs `2`.

That checks out on chain. The funder in
[py-clob-client-v2#70](https://github.com/Polymarket/py-clob-client-v2/issues/70)
and the one Polymarket identified in #73 are both **Gnosis Safe v1.3.0** proxies
owned by the reporting EOA, with byte-identical proxy code and the same
implementation address.

Nothing in the Polymarket API tells you which kind you have. `GET /deployed`
answers the same for `type=SAFE` and `type=WALLET`. The only reliable
discriminator is asking the contract whether it implements the Safe interface,
which is why this tool makes one `eth_call` to Polygon:

```
✓ funder is a Gnosis Safe 1.3.0, use signature_type=2
  The UI deploys this kind when the account was created with an external wallet
  rather than email or Google. Signing it as POLY_1271 (3) is the most common
  cause of "the order signer address has to be the address of the API KEY".
```

Run with `--no-rpc` and the tool says the funder kind is unknown rather than
guessing, because a wrong guess here is the exact failure it exists to prevent.

#70 is still open, so treat this as the leading hypothesis rather than the last
word.

## What it will not do

- **It never places, cancels, or modifies an order.** Every request across all
  eight stages is a GET, a read-only `eth_call`, or a websocket subscribe. The
  order dry-run builds and validates the payload locally without signing it,
  and the RFQ stage documents the maker endpoints without calling them
- **It never asks for a private key.** Everything here works from an address plus
  L2 credentials
- **It never prints your secret.** The passphrase and secret are redacted
  everywhere, the API key is masked, and there's a test that fails if either
  leaks into a finding
- **It sends nothing anywhere.** No telemetry, no phone-home. It talks to
  Polymarket's hosts and one Polygon RPC, which sees your funder address in a
  read call. Point it somewhere you trust with `--rpc`, or skip it with
  `--no-rpc`

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
- Order-book bids come back ascending by price, so best bid is the last entry,
  not the first
- Market-channel websocket frames arrive wrapped in a JSON array, which the
  AsyncAPI spec's examples don't show
- The unified SDK ships on PyPI as `polymarket-client` and imports as
  `polymarket`; the PyPI name `polymarket` belongs to an unrelated package
- `/balance-allowance` is scoped to the API key's identity: a funded funder
  with trade history returns **404** (not 403, not 0) under credentials derived
  from a different signer, and rejected credentials return 401 — so 404 with
  good credentials means "wrong identity for this funder," verified against a
  live funded account
- The relayer's `/deployed` only tracks wallets its own Safe factory deployed.
  Builder wallets are EIP-1967 beacon proxies with code on chain, and the
  relayer answers `deployed: false` for them — another reason the funder gets
  classified from chain state, not from the relayer

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
