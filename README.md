# polymarket-doctor

**Find out why your Polymarket integration won't place orders, before you write the trading code.**

[![CI](https://github.com/Giri-Aayush/polymarket-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/Giri-Aayush/polymarket-doctor/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10--3.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)

The V2 exchange has a failure mode that costs people weeks. Authentication
succeeds, every read endpoint works, and then `POST /order` is rejected every
single time with an error that points nowhere near the cause. As of August 2026,
**49 of the ~118 open issues** across `py-clob-client-v2`, `clob-client-v2` and
`rs-clob-client-v2` are that one bug. The [biggest
thread](https://github.com/Polymarket/py-clob-client-v2/issues/70) has 44
comments and has been open since May.

For most people the cause is a signature type that doesn't match what their
funder contract actually is, and no Polymarket API will tell you which one you
have. This tool finds out in about four seconds.

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

That funder is the one from
[py-clob-client-v2#70](https://github.com/Polymarket/py-clob-client-v2/issues/70),
where 44 comments conclude the SDK simply can't sign for it. On chain it's a
Gnosis Safe that needs `signature_type=2`. The [signature-type
section](#the-signature-type-thing) explains why. Every finding that maps to a
known issue links it, with its current state and how many people are on the
thread.

---

**Contents** · [Install](#install) · [Use](#use) · [Production](#running-it-in-production) · [The eight
stages](#the-eight-stages) · [The signature-type
thing](#the-signature-type-thing) · [What it will not
do](#what-it-will-not-do) · [Notes from the API](#notes-from-the-api) ·
[Verification](#verification) · [Development](#development)

## Install

It isn't on PyPI yet, so install from source:

```bash
pip install git+https://github.com/Giri-Aayush/polymarket-doctor
```

Or clone it to work on the checks:

```bash
git clone https://github.com/Giri-Aayush/polymarket-doctor
cd polymarket-doctor
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

## Use

An address is enough for the environment and identity stages:

```bash
polymarket-doctor onboard --address 0xYourWallet
```

Add the deposit wallet if collateral lives somewhere other than the signer:

```bash
polymarket-doctor onboard --address 0xYourEOA --funder 0xYourDepositWallet
```

L2 credentials unlock the auth and funding stages. If you don't have them yet,
`scripts/derive-credentials.py` derives them from your wallet, locally, in your
terminal. That's the one place a private key belongs. The doctor itself never
reads `POLYMARKET_PRIVATE_KEY`; only that helper does, for a single signature.

```bash
pip install py-clob-client-v2
export POLYMARKET_PRIVATE_KEY=0x...
creds="$(python scripts/derive-credentials.py)" && eval "$creds"
polymarket-doctor onboard --address 0xYourEOA --funder 0xYourDepositWallet
```

Already have credentials? Export them directly. Prefer the environment over
flags so secrets stay out of your shell history:

```bash
export POLYMARKET_API_KEY=...
export POLYMARKET_API_SECRET=...
export POLYMARKET_API_PASSPHRASE=...
polymarket-doctor onboard --funder 0xYourDepositWallet
```

Run a single gate and it pulls in whatever it depends on:

```bash
polymarket-doctor check auth.key-identity
```

Exit codes are a stable contract, so a pipeline can branch on them: `0` all
clear, `1` a blocking failure (the integration isn't ready), `2` a usage error
(a bad flag, an unreadable file, missing credentials). A warning never fails the
run.

### Verify an order your own code produced

`onboard` checks that your *account* is ready. `verify-order` checks the other
half: that a signed order your code built is one the exchange will accept. It
runs the server's own validations against the order and never sends it.

```bash
your-bot --dump-signed-order | polymarket-doctor verify-order --token <token-id>
```

It recovers the signer from the EIP-712 signature, so it can tell you whether
your signing actually works. Then it confirms the signature type matches your
funder and checks the tick grid, the minimum size, and the base-unit math. For a
market order that would otherwise come back as a bare `400`, it names the exact
invariant that broke:

```console
  ✓ signature recovers to signer 0x19E7…ff2A
  ✗ price 0.0050002 is off the 0.001 tick grid
      On 0.001-tick markets this is usually a taker amount computed with 5
      decimals instead of 6.
```

It accepts either the full `POST /order` body or the bare order object, from a
file (`--file`) or stdin. A `signatureType 3` order (deposit wallet, EIP-1271)
can only be verified on chain, and the tool says so rather than guessing.

## Running it in production

The terminal output is for a human debugging an integration. For a market
maker's own tooling, there are three things you'll want.

**Machine-readable output.** Pass `--format json` to any command and it emits a
versioned document instead of colored text. Every check is in there with its
severity, summary, remedy, and any issue it maps to, plus a top-level `ok` and
`exit_code`.

```bash
polymarket-doctor onboard --funder 0xYourWallet --format json | jq '.ok'
```

```json
{
  "schema_version": "1.0",
  "ok": true,
  "exit_code": 0,
  "summary": { "total": 16, "passed": 12, "warnings": 2, "failures": 0, "skipped": 2 },
  "checks": [
    {
      "id": "identity.account-kind",
      "severity": "pass",
      "summary": "funder is a Gnosis Safe 1.3.0, use signature_type=2"
    }
  ]
}
```

The `schema_version` only changes for a breaking change to the shape, so a
parser can rely on it. The **stable** fields are the top-level `ok`,
`exit_code`, `summary`, and each check's `id`/`severity`/`summary`/`issue`. The
`facts` and `evidence` blocks are best-effort context and may gain fields, so
gate on the stable set. Secrets never appear in the document; a test fails the
build if they do.

**Embed it, don't shell out.** The same run is available as a library call, so
a pre-trade startup check can gate on it in-process:

```python
from polymarket_doctor import run_onboard

report = run_onboard(address="0xYourEOA", funder="0xYourDepositWallet")
if report.blocked:
    raise SystemExit("Polymarket integration not ready: "
                     f"{report.first_failure().finding.summary}")
```

**Resilience.** Transient failures (a dropped connection, a 502 from an edge
node) are retried with backoff, so a blip on a health check doesn't flip a stage
red when a second attempt would have answered. A real 4xx is never retried,
because that's the exchange's answer, not a blip. Tune it with `--retries`
(default 2), and POSTs are never retried so nothing can double-submit.

A minimal pre-trade gate in CI:

```bash
polymarket-doctor onboard --funder "$FUNDER" --format json > health.json
jq -e '.ok' health.json >/dev/null || { echo "integration not ready"; exit 1; }
```

## The eight stages

| # | Stage | What it answers |
|---|-------|-----------------|
| 0 | **environment** | Right host, right protocol version, clock in sync, which SDK is installed |
| 1 | **identity** | What signature type your funder needs, and whether your signer can authorize for it |
| 2 | **auth** | Do your credentials work, which address do they authenticate as, do request bodies hash the way the server expects |
| 3 | **funding** | Collateral the exchange sees, with the caveat that this endpoint lies |
| 4 | **market limits** | Tick grid, neg-risk, fee rate, minimum size for a real market |
| 5 | **order dry run** | Builds the order payload and validates every invariant, without signing or sending it |
| 6 | **websocket** | Subscribes to the market feed and measures time to first frame |
| 7 | **RFQ** | Reaches the RFQ gateway and documents the maker flow |

What each stage does, in more detail:

**0 · environment.** Flags a 403 at the edge as a bot challenge rather than an
auth problem. Catches a V1 host before it silently produces
`order_version_mismatch`. Measures clock drift, because `POLY_TIMESTAMP` is
validated server-side and a drifting host throws 401s that look random.

**1 · identity.** The stage that matters most. It classifies the funder from
chain state: a Gnosis Safe needs `signature_type=2`, a beacon-proxy deposit
wallet needs `3`. It then checks that your signer is actually an owner of that
funder. A Safe only accepts signatures from its owners, so if yours isn't one,
no signature type will work.

**2 · auth.** Verifies the secret is valid url-safe base64, confirms which
address the key authenticates as, and catches the body-hashing bug. The SDK
signs `str(body).replace("'", '"')`, which is Python's repr, not JSON. The moment
a bool or `None` appears, the digests diverge, and since GETs carry no body it
reads like a credentials problem
([#108](https://github.com/Polymarket/py-clob-client-v2/issues/108)).

**3 · funding.** A signed read of `/balance-allowance`. A zero balance is a
warning, never a failure, because that endpoint reports `0` for genuinely funded
accounts. UI deposits sit on an internal ledger it doesn't see
([#105](https://github.com/Polymarket/py-clob-client-v2/issues/105)). Don't gate
order placement on it.

**4 · market limits.** Resolves a market, either yours via `--token` or the
highest-volume open one, and reads its constraints. On 0.001-tick books it warns
about the taker-decimal-count trap that rejects every market order computed with
the coarse default
([#99](https://github.com/Polymarket/py-clob-client-v2/issues/99)).

**5 · order dry run.** Builds the exact payload a client would sign: maker, price
snapped to the tick grid, amounts in 6-decimal base units computed with
`Decimal`. It validates every invariant the server enforces. Nothing is signed
and nothing is sent; the only request is a GET of the book.

**6 · websocket.** Connects, subscribes, and measures first-frame latency. The
passing finding carries the caveat that matters: the stream is known to stop
silently while the socket stays open
([#26](https://github.com/Polymarket/real-time-data-client/issues/26)), so you
need last-frame staleness tracking, not connection liveness.

**7 · RFQ.** Reaches the gateway on its own host, counts combo markets, and
documents the maker flow (quote, cancel, last-look confirm) along with its two
distinct auth-error strings. Quote submission is deliberately left alone, because
sending one would place a real quote.

## The signature-type thing

Roughly 49 of the open issues across the v2 clients report the same error:

```
the order signer address has to be the address of the API KEY
```

The threads mostly conclude that the SDKs can't sign for deposit wallets and that
only the Rust client works. [Polymarket's answer on
ts-sdk#73](https://github.com/Polymarket/ts-sdk/issues/73) is different. An API
key authenticating the EOA while orders execute from the funder is the *intended*
model, and the reported failures were accounts sending `signature_type=3`
(POLY_1271) for a funder that is actually an older Gnosis Safe and needs `2`.

That holds up on chain. The funder in
[#70](https://github.com/Polymarket/py-clob-client-v2/issues/70) and the one
Polymarket identified in #73 are both **Gnosis Safe v1.3.0** proxies owned by the
reporting EOA, with byte-identical proxy code and the same implementation address.

Nothing in the Polymarket API tells you which kind you have. `GET /deployed`
answers the same for `type=SAFE` and `type=WALLET`. The only reliable
discriminator is asking the contract itself whether it implements the Safe
interface, which is why the tool makes one `eth_call` to Polygon:

```
VERSION() / getOwners() answers  →  Gnosis Safe   →  signature_type=2
both revert                      →  beacon proxy  →  signature_type=3
```

Run with `--no-rpc` and it reports the funder kind as *unknown* rather than
guessing, because a wrong guess here is the exact failure it exists to prevent.

> [#70](https://github.com/Polymarket/py-clob-client-v2/issues/70) is still open,
> so treat this as the leading hypothesis, not the last word.

## What it will not do

- **Never places, cancels, or modifies an order.** Every request across all eight
  stages is a GET, a read-only `eth_call`, or a websocket subscribe. The dry run
  builds and validates the payload locally without signing it, and the RFQ stage
  documents the maker endpoints without calling them.
- **Never asks for a private key.** Everything works from an address plus L2
  credentials. The one helper that touches a key, `derive-credentials.py`, is a
  separate script you run yourself.
- **Never prints your secret.** The passphrase and secret are redacted
  everywhere, the API key is masked, and a test fails the build if either ever
  leaks into a finding.
- **Sends nothing anywhere.** No telemetry, no phone-home. It talks to
  Polymarket's hosts and one Polygon RPC, which sees your funder address in a read
  call. Point it at an RPC you trust with `--rpc`, or skip chain reads with
  `--no-rpc`.

## Notes from the API

Details verified against production in August 2026, each of which cost time to
discover:

- `GET /time` returns unix **seconds** as a bare integer, not JSON. The timestamp
  on `GET /book` is **milliseconds**. `POLY_TIMESTAMP` is seconds. Three places,
  two units.
- RFQ is a separate service on `combos-rfq-api.polymarket.com`. Paths under
  `clob.polymarket.com/rfq` 404 through nginx.
- Three services speak three auth-error dialects. The CLOB says
  `Unauthorized/Invalid api key`. RFQ with no address header says `invalid l2
  address header`. RFQ with a rejected HMAC returns the gRPC `could not validate
  hmac signature`. Grep one service's error table for another's strings and you
  find nothing.
- `GET /balance-allowance` is scoped to the API key's identity. A funded funder
  with trade history returns **404** (not 403, not 0) under credentials from a
  different signer, while rejected credentials return 401.
- `clob-v2.polymarket.com` appears in `rs-clob-client-v2`'s README, but the CLOB
  is served from `clob.polymarket.com`, which already answers `/version` with `2`.
- The relayer's `/deployed` only tracks wallets its own Safe factory deployed.
  Beacon-proxy deposit wallets have code on chain and still read `deployed:
  false`, which is another reason the funder gets classified from chain state
  rather than the relayer.
- Order-book bids come back **ascending** by price, so the best bid is the last
  entry, not the first.
- Market-channel websocket frames arrive wrapped in a JSON **array**, which the
  AsyncAPI spec's examples don't show.
- Minimum order is 5 outcome tokens regardless of notional. Tick size varies per
  market between 0.01 and 0.001.
- The unified SDK ships on PyPI as `polymarket-client` and imports as
  `polymarket`. The bare name `polymarket` on PyPI is an unrelated package.

## Verification

Every claim above is backed by a live API call, an on-chain read, or a pinned
test, not by assertion. [`VERIFICATION.md`](VERIFICATION.md) records the proof
for each one, with the command to reproduce it: 238 tests, CI green on Python
3.10 through 3.13, 100% line coverage,, side-by-side `curl`-versus-tool measurements, and the live
rejection contracts (`maker address not allowed…`, the RFQ HMAC error) captured
without ever placing an order. What the tool deliberately does not verify is
listed there too: a successful fill, a live quote, and the still-open #70 root
cause.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

Checks declare the facts they read and write, and the registry topologically
sorts them from that. Run order is derived rather than hand-maintained, and
`check <id>` pulls in its own prerequisites. Adding a check means subclassing
`Check`, declaring its `reads` and `writes`, and registering it in
`checks/__init__.py`.

## License

MIT
