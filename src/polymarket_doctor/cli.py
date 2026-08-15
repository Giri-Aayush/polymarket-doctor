"""Command line entry point.

Credentials come from the environment by default. There are --api-secret style
flags for one-off use, but they put the secret in your shell history, so the
help text says so.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from collections.abc import Sequence

from rich.console import Console

from . import __version__
from .checks import default_registry
from .core.context import Context, Credentials
from .core.runner import Runner
from .net.chain import DEFAULT_RPC, ChainReader
from .net.endpoints import Endpoints
from .net.http import HttpxProbe
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
        raise SystemExit(f"incomplete credentials, missing: {', '.join(missing)}")
    return Credentials(*parts)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()

    if args.command == "list":
        for check in default_registry().resolve():
            console.print(f"  {check.stage.value}  {check.id:<26} {check.title}")
        return 0

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

    with HttpxProbe(timeout=args.timeout) as probe:
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

    TerminalReport(console).render(report, host=endpoints.clob)
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
