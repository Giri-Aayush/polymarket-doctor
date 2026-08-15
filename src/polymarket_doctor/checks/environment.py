"""Stage 0 — is the client pointed at the right thing, and is its clock sane?

Cheap checks that run before anything needs a key. When one of these fails,
every later stage would fail too, for reasons that look nothing like the cause.
"""

from __future__ import annotations

import importlib.metadata as metadata
import importlib.util
import time

from .. import issues
from ..core.check import Check, Finding, Stage
from ..core.context import Context
from ..core.facts import Fact
from ..net import endpoints

# The server doesn't publish its L2 timestamp tolerance, so these are picked
# from behaviour rather than docs: signing is fast enough that a well-synced
# host sits under a second, and reports of "random" 401s start showing up once
# a machine has drifted tens of seconds. Warn early, fail where it's certainly
# broken.
SKEW_WARN_SECONDS = 5
SKEW_FAIL_SECONDS = 30

# (import name, distribution name, label). These differ for the unified SDK: it
# imports as `polymarket` but ships as `polymarket-client`, and there is an
# unrelated third-party package actually called `polymarket` on PyPI. Deriving
# one name from the other misidentifies that package as Polymarket's.
#
# Order is preference. First one installed is what the partner is most likely
# running; v1 is last because it's archived and can't sign V2 orders at all.
KNOWN_SDKS = (
    ("polymarket", "polymarket-client", "polymarket-client (unified)"),
    ("py_clob_client_v2", "py-clob-client-v2", "py-clob-client-v2"),
    ("py_clob_client", "py-clob-client", "py-clob-client (v1, archived)"),
)


class ClobReachable(Check):
    id = "env.reachable"
    stage = Stage.ENVIRONMENT
    title = "CLOB is reachable"
    writes = frozenset({Fact.HOST})

    def run(self, ctx: Context) -> Finding:
        response = ctx.probe.get(ctx.endpoints.clob_url("/"))

        if not response.reached_server:
            return Finding.fail(
                f"cannot reach {ctx.endpoints.clob}",
                detail=response.error,
                remedy="Check egress rules and any corporate proxy. Every later "
                       "check needs this host.",
            )

        # Cloudflare sits in front and challenges some datacenter ranges. That
        # surfaces later as a 403 on key creation, which reads like an auth
        # problem and isn't.
        if response.status == 403:
            return Finding.fail(
                f"{ctx.endpoints.clob} returned 403 before auth",
                detail="Reached the edge but got blocked ahead of the API. "
                       "Usually a bot challenge on a datacenter IP.",
                remedy="Retry from a residential or allow-listed IP.",
                issue=issues.CLOUDFLARE_BLOCKS_KEY_CREATION,
                status=response.status,
            )

        if not response.ok:
            return Finding.fail(
                f"{ctx.endpoints.clob} returned {response.status}",
                detail=response.error_message() or "unexpected status from the CLOB root",
                status=response.status,
            )

        ctx.facts.set(Fact.HOST, ctx.endpoints.clob)
        return Finding.ok(
            f"{ctx.endpoints.clob} responding",
            latency_ms=response.elapsed_ms,
        )


class ProtocolVersion(Check):
    id = "env.protocol-version"
    stage = Stage.ENVIRONMENT
    title = "Exchange protocol version"
    reads = frozenset({Fact.HOST})
    writes = frozenset({Fact.PROTOCOL_VERSION})

    def run(self, ctx: Context) -> Finding:
        response = ctx.probe.get(ctx.endpoints.clob_url("/version"))
        if not response.ok:
            return Finding.fail(
                "could not read /version",
                detail=response.error or f"HTTP {response.status}",
            )

        version = response.body.get("version") if isinstance(response.body, dict) else None
        if version is None:
            return Finding.fail(
                "/version did not report a version",
                detail=f"body was {response.body!r}",
            )

        ctx.facts.set(Fact.PROTOCOL_VERSION, version)

        if version == 1:
            return Finding.fail(
                "host is serving protocol 1",
                detail="Orders signed for V2 are rejected as order_version_mismatch "
                       "against a V1 exchange, and the reverse.",
                issue=issues.ORDER_VERSION_MISMATCH,
                version=version,
            )

        deprecated = endpoints.DEPRECATED_HOSTS.get(ctx.endpoints.clob)
        if deprecated:
            return Finding.warn(
                f"protocol {version}, but this host is not the documented one",
                detail=deprecated,
                remedy=f"Point at {endpoints.CLOB}.",
                version=version,
            )

        return Finding.ok(f"protocol {version}", version=version)


class ClockSkew(Check):
    """Compare local time to the server's.

    /time returns unix seconds as a bare integer, not JSON — worth knowing
    because a client that json-decodes it gets an exception rather than a
    number. Note the mixed units across the API: this is seconds, while the
    timestamp on /book is milliseconds.
    """

    id = "env.clock-skew"
    stage = Stage.ENVIRONMENT
    title = "Local clock matches the exchange"
    reads = frozenset({Fact.HOST})
    writes = frozenset({Fact.CLOCK_SKEW_SECONDS})

    def run(self, ctx: Context) -> Finding:
        before = time.time()
        response = ctx.probe.get(ctx.endpoints.clob_url("/time"))
        after = time.time()

        if not response.ok:
            return Finding.fail(
                "could not read /time",
                detail=response.error or f"HTTP {response.status}",
            )

        server_seconds = _coerce_epoch_seconds(response.body)
        if server_seconds is None:
            return Finding.fail(
                "/time returned something that isn't an epoch",
                detail=f"body was {response.body!r}",
            )

        # Midpoint of the request window is the fairest local reading; using
        # `after` alone charges the whole round trip to skew.
        skew = round(((before + after) / 2) - server_seconds, 1)
        ctx.facts.set(Fact.CLOCK_SKEW_SECONDS, skew)

        magnitude = abs(skew)
        evidence = {"skew_seconds": skew, "server_epoch": server_seconds}

        if magnitude >= SKEW_FAIL_SECONDS:
            return Finding.fail(
                f"local clock is {skew:+.1f}s off the exchange",
                detail="POLY_TIMESTAMP is checked against server time. At this "
                       "drift, L2 requests get rejected as 401s that look "
                       "intermittent and track nothing you changed.",
                remedy="Sync the host clock (NTP/chrony) and re-run.",
                **evidence,
            )
        if magnitude >= SKEW_WARN_SECONDS:
            return Finding.warn(
                f"local clock is {skew:+.1f}s off the exchange",
                detail="Not far enough to reject requests today, but it drifts.",
                remedy="Enable NTP before going live.",
                **evidence,
            )
        return Finding.ok(f"clock within {magnitude:.1f}s of the exchange", **evidence)


class SdkGeneration(Check):
    """Which client library is installed, and can it sign for this exchange?"""

    id = "env.sdk"
    stage = Stage.ENVIRONMENT
    title = "Installed SDK generation"
    writes = frozenset({Fact.SDK})

    def run(self, ctx: Context) -> Finding:
        installed = list(_installed_sdks())
        if not installed:
            ctx.facts.set(Fact.SDK, None)
            return Finding.warn(
                "no Polymarket SDK found in this environment",
                detail="Checks still run against the API directly; SDK-specific "
                       "advice will be missing.",
                remedy="pip install py-clob-client-v2 (or the unified py-sdk).",
            )

        module, label, version = installed[0]
        ctx.facts.set(Fact.SDK, {"module": module, "label": label, "version": version})
        evidence = {"module": module, "version": version}

        if module == "py_clob_client":
            return Finding.fail(
                f"{label} {version} is installed",
                detail="v1 was archived on 2026-05-25 and signs the V1 order "
                       "struct, which the V2 exchange rejects outright.",
                remedy="Move to py-clob-client-v2, or the unified py-sdk.",
                issue=issues.V1_SDK_ARCHIVED,
                **evidence,
            )

        if len(installed) > 1:
            names = ", ".join(f"{lbl} {ver}" for _, lbl, ver in installed)
            return Finding.warn(
                f"{len(installed)} Polymarket SDKs installed side by side",
                detail=f"Found {names}. Both export a ClobClient, so which one "
                       f"an import resolves to depends on your path.",
                remedy="Uninstall the one you're not using.",
                **evidence,
            )

        return Finding.ok(f"{label} {version}", **evidence)


def _installed_sdks() -> list[tuple[str, str, str]]:
    """Installed Polymarket SDKs as (import name, label, version)."""
    found = []
    for module, distribution, label in KNOWN_SDKS:
        try:
            version = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
        # The distribution can be present without its module importable (a
        # broken install, or a name collision). Only count it if both hold.
        if importlib.util.find_spec(module) is None:
            continue
        found.append((module, label, version))
    return found


def _coerce_epoch_seconds(body: object) -> int | None:
    """/time answers with a bare integer today; tolerate a JSON wrapper too."""
    if isinstance(body, bool):
        return None
    if isinstance(body, (int, float)):
        return int(body)
    if isinstance(body, str):
        try:
            return int(body.strip())
        except ValueError:
            return None
    if isinstance(body, dict):
        for key in ("time", "timestamp", "server_time"):
            if key in body:
                return _coerce_epoch_seconds(body[key])
    return None


CHECKS = (ClobReachable(), ProtocolVersion(), ClockSkew(), SdkGeneration())
