"""Executes checks and decides what to skip when something breaks."""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from .check import Check, Finding, Severity, Stage
from .context import Context
from .facts import Fact
from .registry import Registry


@dataclass(frozen=True, slots=True)
class Outcome:
    check: Check
    finding: Finding
    duration_ms: float

    @property
    def severity(self) -> Severity:
        return self.finding.severity


@dataclass(slots=True)
class RunReport:
    outcomes: list[Outcome] = field(default_factory=list)
    facts: dict[str, object] = field(default_factory=dict)

    @property
    def failures(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.severity is Severity.FAIL]

    @property
    def warnings(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.severity is Severity.WARN]

    @property
    def blocked(self) -> bool:
        return bool(self.failures)

    def first_failure(self) -> Outcome | None:
        return self.failures[0] if self.failures else None

    def by_stage(self) -> Iterator[tuple[Stage, list[Outcome]]]:
        grouped: dict[Stage, list[Outcome]] = {}
        for outcome in self.outcomes:
            grouped.setdefault(outcome.check.stage, []).append(outcome)
        for stage in sorted(grouped):
            yield stage, grouped[stage]

    def exit_code(self) -> int:
        # 1 for a blocking failure, 0 otherwise. Warnings don't fail a run —
        # partners put this in CI and a WARN shouldn't break their pipeline.
        return 1 if self.blocked else 0


class Runner:
    """Runs checks in dependency order, short-circuiting past dead branches.

    When a check fails, anything downstream of the facts it was supposed to
    produce is skipped rather than run. Those checks would fail too, for a
    reason that isn't theirs, and a wall of red hides the one line that matters.
    """

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def run(self, ctx: Context, *, only: Sequence[str] | None = None) -> RunReport:
        report = RunReport()
        unavailable: dict[Fact, str] = {}

        for check in self._registry.resolve(only):
            missing = sorted(
                (fact for fact in check.reads if fact in unavailable),
                key=lambda f: f.value,
            )
            if missing:
                blocker = unavailable[missing[0]]
                report.outcomes.append(
                    Outcome(
                        check,
                        Finding(
                            Severity.SKIP,
                            "skipped",
                            detail=f"needs {missing[0].value}, which {blocker} could not determine",
                        ),
                        0.0,
                    )
                )
                self._mark_unavailable(check, unavailable, blocker)
                continue

            outcome = self._execute(check, ctx)
            report.outcomes.append(outcome)
            if outcome.severity is Severity.FAIL:
                self._mark_unavailable(check, unavailable, check.id)

        report.facts = dict(ctx.facts.as_mapping())
        return report

    @staticmethod
    def _mark_unavailable(check: Check, unavailable: dict[Fact, str], blocker: str) -> None:
        for fact in check.writes:
            unavailable.setdefault(fact, blocker)

    @staticmethod
    def _execute(check: Check, ctx: Context) -> Outcome:
        started = time.perf_counter()
        try:
            finding = check.run(ctx)
        except Exception as exc:  # noqa: BLE001 - a crashed check is a finding
            finding = Finding.fail(
                f"{check.id} raised {type(exc).__name__}",
                detail=str(exc),
                remedy="This is a bug in polymarket-doctor. Please file it with "
                       "the command you ran.",
            )
        duration = round((time.perf_counter() - started) * 1000, 1)
        return Outcome(check, finding, duration)
