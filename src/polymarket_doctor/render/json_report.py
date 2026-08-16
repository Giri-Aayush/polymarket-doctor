"""Machine-readable output for automated use.

A market maker doesn't read terminal colors in their pre-trade startup check.
They parse a document, gate on a field, and alert on a change. This renders a
run into a stable, versioned JSON shape built for exactly that.

The schema is versioned so a parser can rely on it. Bump SCHEMA_VERSION only for
a breaking change to the shape, never for a new optional field.
"""

from __future__ import annotations

import dataclasses
import datetime
from enum import Enum
from typing import Any

from ..core.check import Severity
from ..core.runner import Outcome, RunReport

SCHEMA_VERSION = "1.0"


def build_report(report: RunReport, *, host: str,
                 now: datetime.datetime | None = None) -> dict[str, Any]:
    """Turn a run into the JSON document. `now` is injectable for tests."""
    stamp = (now or datetime.datetime.now(datetime.timezone.utc))
    counts = _counts(report.outcomes)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "polymarket-doctor",
        "generated_at": stamp.isoformat(),
        "host": host,
        "protocol_version": report.facts.get("env.protocol_version"),
        "ok": not report.blocked,
        "exit_code": report.exit_code(),
        "summary": {
            "total": len(report.outcomes),
            "passed": counts.get(Severity.PASS, 0),
            "warnings": counts.get(Severity.WARN, 0),
            "failures": counts.get(Severity.FAIL, 0),
            "skipped": counts.get(Severity.SKIP, 0),
        },
        "checks": [_check(o) for o in report.outcomes],
        "facts": _safe(dict(report.facts)),
    }


def _counts(outcomes: list[Outcome]) -> dict[Severity, int]:
    counts: dict[Severity, int] = {}
    for outcome in outcomes:
        counts[outcome.severity] = counts.get(outcome.severity, 0) + 1
    return counts


def _check(outcome: Outcome) -> dict[str, Any]:
    finding = outcome.finding
    entry: dict[str, Any] = {
        "id": outcome.check.id,
        "stage": outcome.check.stage.value,
        "stage_name": outcome.check.stage.label,
        "title": outcome.check.title,
        "severity": finding.severity.value,
        "summary": finding.summary,
        "duration_ms": outcome.duration_ms,
    }
    if finding.detail:
        entry["detail"] = finding.detail
    if finding.remedy:
        entry["remedy"] = finding.remedy
    if finding.issue:
        issue = finding.issue
        entry["issue"] = {
            "slug": issue.slug,
            "url": issue.url,
            "state": "closed" if issue.closed else "open",
        }
    if finding.evidence:
        entry["evidence"] = _safe(finding.evidence)
    return entry


def _safe(value: Any) -> Any:
    """Coerce anything a check stored into JSON-friendly values.

    Facts and evidence can hold IntEnums (signature types), dataclasses
    (contract profiles), and nested dicts. None of them are JSON out of the box,
    and a str() fallback keeps a novel value from ever crashing the report.
    Secrets never reach here: credentials are redacted at the Finding boundary.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return str(value)
