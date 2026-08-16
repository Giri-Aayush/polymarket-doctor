# Verification record

Everything below was re-verified on **2026-08-15** against production. Each
section gives the command so you can re-run it yourself — claims you can't
reproduce are marketing, not verification.

## 1. Test suite and lint

```console
$ .venv/bin/python -m pytest
141 passed in 1.08s

$ .venv/bin/ruff check .
All checks passed!
```

Coverage 92% (1384 statements, 116 missed — mostly the real HTTP client and
CLI plumbing that the live runs below exercise instead). No test touches the
network: HTTP goes through a stub probe, the websocket tests run a local
server on a loopback port.

## 2. CI

Latest run on `main` (`14a11a2`): **success on Python 3.10, 3.11, 3.12, 3.13**.
CI runs the same pytest + ruff with no network access.

```bash
gh run list -R Giri-Aayush/polymarket-doctor --limit 1
```

## 3. Tool output vs independent measurement

The strongest check: measure the API with raw `curl`, then run the tool, and
compare. Same day, same markets.

| Fact | Independent (`curl`/RPC) | Tool output |
|---|---|---|
| Protocol | `{"version":2}` | `protocol 2` |
| Tick size (token `5511…9123`) | `{"minimum_tick_size":0.01}` | `tick 0.01` |
| Best bid, same token | `0.16` (max of ascending bids) | dry-run `BUY 5 @ 0.16` |
| Order math | 5 × 0.16 × 10⁶ = 800000 | `maker 800000 / taker 5000000` |
| RFQ combo markets | `50` | `50 combo markets listed` |
| Funder in py-clob-client-v2#70 | `VERSION()` → `1.3.0`, owner = reporting EOA | `Gnosis Safe 1.3.0, use signature_type=2` · `is an owner` |
| Clock | `/time` epoch == local | `clock within 0.0s` |

Reproduce the left column:

```bash
curl -s https://clob.polymarket.com/version
curl -s "https://clob.polymarket.com/tick-size?token_id=<token>"
curl -s "https://clob.polymarket.com/book?token_id=<token>"   # bids ascend; best bid is the last
curl -s https://combos-rfq-api.polymarket.com/v1/rfq/combo-markets
```

Reproduce the right column:

```bash
polymarket-doctor onboard \
  --address 0x9F49475F9496c77fa95f76c7C5Bc57467B336792 \
  --funder  0x3EC7EEaa66d849a5F83bE99a3ef47c63f672d649 \
  --token   <token>
```

Exit codes verified live: bare-EOA run exits `1`; clean run exits `0`.

## 4. Authenticated paths

Run with real L2 credentials derived from a throwaway EOA
(`scripts/derive-credentials.py`, key never left the owner's terminal):

- **L1 auth**: the derive script's EIP-712 `ClobAuth` signature was accepted
  by production and returned working credentials.
- **L2 HMAC** (stage 2): signed `GET /auth/api-keys` returned 200 on the first
  attempt — `key authenticates as 0x8cC7…a00f`. Rejected or malformed
  credentials return 401, so a 200 is proof the from-scratch signing
  (unix-seconds timestamp, url-safe base64 HMAC, five `POLY_*` headers)
  matches the server byte for byte.
- **Balance read** (stage 3): signed `GET /balance-allowance` against a
  funded, actively-traded funder using credentials from an unrelated signer
  returned **404** with auth accepted — the identity-scoping behavior recorded
  in the README. Both the 200 and the 404 were observed live on 2026-08-15.

These runs used private credentials and are therefore not replayable from this
file; replay them with your own wallet via `scripts/derive-credentials.py`.

## 5. Rejection contracts (captured live, nothing executable)

The doctor quotes the exchange's refusal strings. Both were captured against
production on 2026-08-15 via `scripts/capture-rejection-contracts.py`, using
probes structurally unable to result in a fill — a signed order from an
unfunded EOA the V2 gate refuses at the door, and an empty-body POST that
carries no quote. Both refused, each in its service's own vocabulary:

- **Order gate** — a real signed order (maker = the unfunded throwaway EOA, 5
  shares at one tick) returned:
  ```
  {"error":"maker address not allowed, please use the deposit wallet flow"}
  ```
  The exact string stage 1 cites — the center of the ~49-issue cluster,
  confirmed from a live signed order, not paraphrased from docs.
- **RFQ maker gate** — an authenticated empty-body POST to `/v1/maker/quotes`
  returned `401`:
  ```
  {"error":"rpc error: code = PermissionDenied desc = could not validate hmac signature"}
  ```
  A third distinct auth-error vocabulary: the CLOB says
  `Unauthorized/Invalid api key`, an RFQ request with no address header says
  `invalid l2 address header`, and an RFQ request with a present-but-rejected
  HMAC says the gRPC `PermissionDenied` above. An integrator grepping one
  service's error table for another's strings finds nothing.

## 5b. Still not verified, by design

- **Order acceptance.** Refusal is captured above; a *successful* fill is not,
  because that means trading — permanently out of scope for a diagnostic.
- **A live RFQ quote.** Refusal is captured; a valid quote is a real quote.
- **The #70 root cause.** The Safe-vs-signature-type diagnosis matches
  Polymarket's maintainer statement (ts-sdk#73) and the on-chain evidence
  above, but the issue is open; this repo treats it as the leading hypothesis.

## 6. Known-issue citations

Every issue this tool cites was re-read from the GitHub API on 2026-08-15
(state, open date, comment count). Comment counts drift; ages are computed at
render time so staleness is visible. Re-check any of them:

```bash
gh issue view 70 -R Polymarket/py-clob-client-v2 --json state,comments,createdAt
```
