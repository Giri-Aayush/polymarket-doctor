"""Library entry point for embedding the diagnostics in your own code.

A market maker's pre-trade startup check shouldn't shell out to a CLI and parse
stdout. This runs the same checks in-process and hands back the structured
report, plus a one-call helper that returns the JSON document directly.

    from polymarket_doctor import run_onboard, to_json

    report = run_onboard(address="0x…", funder="0x…")
    if report.blocked:
        raise SystemExit("Polymarket integration not ready")

    doc = to_json(report, host="https://clob.polymarket.com")
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .checks import default_registry
from .core.context import Context, Credentials
from .core.runner import Runner, RunReport
from .net.chain import DEFAULT_RPC, ChainReader
from .net.endpoints import Endpoints
from .net.http import DEFAULT_RETRIES, DEFAULT_TIMEOUT, HttpxProbe
from .render.json_report import build_report


def run_onboard(
    *,
    address: str | None = None,
    funder: str | None = None,
    credentials: Credentials | None = None,
    token_id: str | None = None,
    host: str | None = None,
    rpc: str | None = DEFAULT_RPC,
    only: Sequence[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> RunReport:
    """Run the checks in-process and return the report.

    `rpc=None` skips chain reads (the funder kind resolves to unknown). `only`
    runs a subset by check id, pulling in each one's prerequisites.
    """
    endpoints = Endpoints(clob=host) if host else Endpoints()
    with HttpxProbe(timeout=timeout, retries=retries) as probe:
        ctx = Context(
            endpoints=endpoints,
            probe=probe,
            chain=ChainReader(probe, rpc) if rpc else None,
            signer_address=address,
            funder_address=funder,
            token_id=token_id,
            credentials=credentials,
        )
        return Runner(default_registry()).run(ctx, only=only)


def to_json(report: RunReport, *, host: str) -> dict[str, Any]:
    """Render a report as the versioned JSON document (a plain dict)."""
    return build_report(report, host=host)
