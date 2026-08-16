"""Command line entry point.

Credentials come from the environment by default. There are --api-secret style
flags for one-off use, but they put the secret in your shell history, so the
help text says so.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import os
import sys
from collections.abc import Sequence

from rich.console import Console
from rich.text import Text

from . import __version__
from .checks import default_registry
from .core.context import Context, Credentials
from .core.runner import Runner
from .net.chain import DEFAULT_RPC, ChainReader
from .net.endpoints import Endpoints
from .net.http import DEFAULT_RETRIES, HttpxProbe
from .render.json_report import build_report
from .render.terminal import TerminalReport

ENV_ADDRESS = "POLYMARKET_ADDRESS"
ENV_FUNDER = "POLYMARKET_FUNDER"
ENV_API_KEY = "POLYMARKET_API_KEY"
ENV_API_SECRET = "POLYMARKET_API_SECRET"
ENV_API_PASSPHRASE = "POLYMARKET_API_PASSPHRASE"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymarket-doctor",
        description="Preflight diagnostics for Polymarket integrations.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    onboard = sub.add_parser("onboard", help="run every implemented gate in order")
    _add_account_args(onboard)
    _add_transport_args(onboard)

    check = sub.add_parser("check", help="run one check and whatever it depends on")
    check.add_argument("check_id", help="id from `polymarket-doctor list`")
    _add_account_args(check)
    _add_transport_args(check)

    verify = sub.add_parser(
        "verify-order",
        help="validate a signed order your code produced, without sending it",
    )
    verify.add_argument(
        "--file", default="-",
        help="signed order JSON (the POST /order body, or the order object); "
             "'-' or omitted reads stdin",
    )
    verify.add_argument(
        "--neg-risk", action="store_true",
        help="the market is neg-risk (selects the neg-risk exchange contract)",
    )
    verify.add_argument(
        "--token", default=None,
        help="token id; when given, the market's real tick and neg-risk flag "
             "are fetched live instead of inferred",
    )
    verify.add_argument("--timeout", type=float, default=12.0)
    verify.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="text for humans, json for pipelines",
    )

    sub.add_parser("list", help="show every registered check in run order")
    return parser


def _add_account_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--address",
        default=os.environ.get(ENV_ADDRESS),
        help=f"wallet that signs (default: ${ENV_ADDRESS})",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("POLYMARKET_TOKEN_ID"),
        help="CLOB token id to run market checks against (default: "
             "$POLYMARKET_TOKEN_ID; omitted, a liquid market is picked)",
    )
    parser.add_argument(
        "--funder",
        default=os.environ.get(ENV_FUNDER),
        help=f"deposit wallet that holds collateral, if different (default: ${ENV_FUNDER})",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get(ENV_API_KEY),
        help=f"L2 API key (default: ${ENV_API_KEY})",
    )
    parser.add_argument(
        "--api-secret",
        default=os.environ.get(ENV_API_SECRET),
        help=f"L2 API secret. Prefer ${ENV_API_SECRET} — a flag lands in shell history",
    )
    parser.add_argument(
        "--api-passphrase",
        default=os.environ.get(ENV_API_PASSPHRASE),
        help=f"L2 API passphrase (default: ${ENV_API_PASSPHRASE})",
    )


def _add_transport_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=None, help="override the CLOB host")
    parser.add_argument(
        "--rpc",
        default=os.environ.get("POLYGON_RPC_URL", DEFAULT_RPC),
        help="Polygon RPC, used to tell a Gnosis Safe funder from a deposit "
             "wallet (default: $POLYGON_RPC_URL, else a public node)",
    )
    parser.add_argument(
        "--no-rpc",
        action="store_true",
        help="skip chain reads; funder type resolves to 'unknown'",
    )
    parser.add_argument("--timeout", type=float, default=12.0, help="per-request timeout, seconds")
    parser.add_argument(
        "--retries", type=int, default=DEFAULT_RETRIES,
        help=f"retries on a transient network failure (default: {DEFAULT_RETRIES})",
    )
    parser.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="text for humans, json for pipelines and monitoring",
    )


def _credentials_from(args: argparse.Namespace) -> Credentials | None:
    parts = (args.api_key, args.api_secret, args.api_passphrase)
    if not any(parts):
        return None
    if not all(parts):
        missing = [
            name for name, value in
            (("--api-key", args.api_key),
             ("--api-secret", args.api_secret),
             ("--api-passphrase", args.api_passphrase))
            if not value
        ]
        # Usage error, so exit 2 (SystemExit with a non-int would print and
        # exit 1, colliding with the blocking-failure code).
        print(f"incomplete credentials, missing: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)
    return Credentials(*parts)


def _verify_order(args: argparse.Namespace, console: Console) -> int:
    """Validate a signed order from a file or stdin. Read-only throughout."""
    from decimal import Decimal

    from .core.check import Severity
    from .order_verify import verify_order

    if args.file == "-":
        raw = sys.stdin.read()
    else:
        try:
            with open(args.file) as handle:
                raw = handle.read()
        except OSError as exc:
            # A fat-fingered path is a usage error (exit 2), not a rejected
            # order (exit 1) — a consumer must be able to tell them apart.
            print(f"could not read {args.file}: {exc.strerror or exc}", file=sys.stderr)
            return 2
    try:
        order = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"could not parse order JSON: {exc}", file=sys.stderr)
        return 2

    neg_risk = args.neg_risk
    tick: Decimal | None = None
    # A token id lets us replace inference with the market's real tick and
    # neg-risk flag — one GET each, no auth.
    if args.token:
        with HttpxProbe(timeout=args.timeout) as probe:
            endpoints = Endpoints()
            ts = probe.get(endpoints.clob_url("/tick-size"), params={"token_id": args.token})
            nr = probe.get(endpoints.clob_url("/neg-risk"), params={"token_id": args.token})
            if isinstance(ts.body, dict) and "minimum_tick_size" in ts.body:
                tick = Decimal(str(ts.body["minimum_tick_size"]))
            if isinstance(nr.body, dict) and "neg_risk" in nr.body:
                neg_risk = bool(nr.body["neg_risk"])

    results = verify_order(order, neg_risk=neg_risk, tick_size=tick)
    failed = any(r.finding.severity is Severity.FAIL for r in results)

    if args.format == "json":
        print(json.dumps({
            "schema_version": "1.0",
            "tool": "polymarket-doctor",
            "command": "verify-order",
            "exchange": "neg-risk" if neg_risk else "standard",
            "ok": not failed,
            "exit_code": 1 if failed else 0,
            "checks": [
                {
                    "name": r.name,
                    "severity": r.finding.severity.value,
                    "summary": r.finding.summary,
                    **({"detail": r.finding.detail} if r.finding.detail else {}),
                    **({"remedy": r.finding.remedy} if r.finding.remedy else {}),
                }
                for r in results
            ],
        }, indent=2))
        return 1 if failed else 0

    console.print()
    console.print(Text("Order verification", style="bold"),
                  Text(f"   {'neg-risk' if neg_risk else 'standard'} exchange",
                       style="bright_black"))
    console.print()
    glyph = {Severity.PASS: ("✓", "green"), Severity.WARN: ("!", "yellow"),
             Severity.FAIL: ("✗", "red"), Severity.SKIP: ("·", "bright_black")}
    for result in results:
        mark, colour = glyph[result.finding.severity]
        line = Text("  ")
        line.append(f"{mark} ", style=colour)
        line.append(result.finding.summary)
        console.print(line)
        for extra in (result.finding.detail, result.finding.remedy):
            if extra:
                label = "Fix: " if extra is result.finding.remedy else ""
                console.print(Text(f"      {label}{extra}", style="bright_black"))
    console.print()

    console.print(Text(" This order would be rejected." if failed
                       else " No blocking problems found in this order.",
                       style="bold red" if failed else "bold green"))
    console.print(Text(" Structural and signature checks only, nothing was sent.",
                       style="bright_black"))
    console.print()
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point with a top-level guard so nothing reaches the user as a
    traceback. Ctrl-C exits 130, a broken pipe exits quietly, and any
    unexpected error becomes a one-line message with exit 2."""
    try:
        return _run(argv)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # The reader (a `| head`, say) went away. Silence the flush error that
        # Python would otherwise raise at shutdown, and exit.
        with contextlib.suppress(Exception):
            sys.stdout.close()
        return 0
    except SystemExit as exit_:
        # argparse and our own usage errors raise SystemExit; honor the code.
        return exit_.code if isinstance(exit_.code, int) else 2
    except Exception as exc:  # noqa: BLE001 - last resort, never a stack trace
        print(f"polymarket-doctor: unexpected error: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2


def _run(argv: Sequence[str] | None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()

    if args.command == "list":
        for check in default_registry().resolve():
            console.print(f"  {check.stage.value}  {check.id:<26} {check.title}")
        return 0

    if args.command == "verify-order":
        return _verify_order(args, console)

    endpoints = Endpoints(clob=args.host) if args.host else Endpoints()
    registry = default_registry()

    if args.command == "check":
        known = sorted(check.id for check in registry)
        if args.check_id not in known:
            # A typo'd id shouldn't produce a traceback. Suggest the closest
            # real one, since 'auth.key_identity' for 'auth.key-identity' is
            # the likely shape of the mistake.
            closest = difflib.get_close_matches(args.check_id, known, n=1)
            hint = f" Did you mean {closest[0]}?" if closest else ""
            print(f"no such check: {args.check_id}.{hint}", file=sys.stderr)
            print(f"known checks: {', '.join(known)}", file=sys.stderr)
            return 2

    with HttpxProbe(timeout=args.timeout, retries=args.retries) as probe:
        ctx = Context(
            endpoints=endpoints,
            probe=probe,
            chain=None if args.no_rpc else ChainReader(probe, args.rpc),
            signer_address=args.address,
            funder_address=args.funder,
            token_id=args.token,
            credentials=_credentials_from(args),
        )
        only = [args.check_id] if args.command == "check" else None
        report = Runner(registry).run(ctx, only=only)

    if args.format == "json":
        # print, not console.print: Rich would wrap and colorize, and a
        # pipeline wants the raw document on stdout.
        print(json.dumps(build_report(report, host=endpoints.clob), indent=2))
    else:
        TerminalReport(console).render(report, host=endpoints.clob)
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
