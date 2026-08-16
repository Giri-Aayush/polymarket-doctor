# Changelog

Notable changes to polymarket-doctor. The format follows
[Keep a Changelog](https://keepachangelog.com/). Package versions are SemVer;
the JSON output has its own `schema_version` and a major bump there means a
breaking change to the machine-readable shape.

## Unreleased

### Added
- `--format json` on `onboard`, `check`, and `verify-order` for a versioned,
  machine-readable document (`schema_version` 1.0) that pipelines and monitoring
  can gate on.
- Library entry point: `run_onboard()` and `to_json()`, so a pre-trade startup
  check can run the diagnostics in-process instead of shelling out.
- Network retries with bounded exponential backoff on transient failures
  (transport errors, 502/503/504), tunable with `--retries`. Real 4xx responses
  and POSTs are never retried.
- `verify-order`: validate a signed order your own code produced, without
  sending it, including EIP-712 signer recovery.
- `py.typed` marker so downstream type checkers see the annotations.

### Fixed
- `verify-order` no longer crashes on a malformed-but-present field (for example
  a hex-encoded `salt`); it reports a finding instead.
- A missing or unreadable `--file` now exits 2 with a message rather than a
  traceback, distinct from the exit-1 that means the order was rejected.
- Top-level guard in the CLI: Ctrl-C exits 130, a broken pipe exits quietly, and
  any unexpected error is a one-line message on stderr, never a stack trace.
- JSON output renders `bytes` as hex and survives a self-referential value.

### Security
- `scripts/derive-credentials.py` now `shlex.quote`s the values it prints, so a
  hostile or malformed server response can't inject shell into the caller's
  `eval`. It also no longer interpolates a third-party exception that could echo
  the private key.

### Known follow-ups
- `mypy` is not yet gated in CI; the `Finding.ok/warn/fail` evidence kwargs need
  a signature cleanup first.
