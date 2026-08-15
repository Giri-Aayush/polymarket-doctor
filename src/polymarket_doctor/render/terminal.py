"""Terminal output.

Shape of it: one line per check so a clean run stays scannable, and the full
explanation only for what actually broke. A partner runs this while someone
waits on Slack — the first failure and its fix should be readable without
scrolling.
"""

from __future__ import annotations

from datetime import date

from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

from ..checks import PENDING_STAGES
from ..core.check import Severity, Stage
from ..core.runner import Outcome, RunReport

GLYPH = {
    Severity.PASS: ("✓", "green"),
    Severity.WARN: ("!", "yellow"),
    Severity.FAIL: ("✗", "red"),
    Severity.SKIP: ("·", "bright_black"),
}


class TerminalReport:
    def __init__(self, console: Console | None = None, today: date | None = None) -> None:
        self._console = console or Console()
        self._today = today or date.today()

    def render(self, report: RunReport, *, host: str) -> None:
        self._header(host, report)
        for stage, outcomes in report.by_stage():
            self._stage(stage, outcomes)
        self._pending()
        self._footer(report)

    def _header(self, host: str, report: RunReport) -> None:
        version = report.facts.get("env.protocol_version")
        suffix = f" · protocol v{version}" if version is not None else ""
        self._console.print()
        self._console.print(
            Text("Polymarket Integration Doctor", style="bold"),
            Text(f"   {host}{suffix}", style="bright_black"),
        )
        self._console.print()

    def _stage(self, stage: Stage, outcomes: list[Outcome]) -> None:
        self._console.print(
            Text(f" {stage.value}  {stage.label}", style="bold bright_black")
        )
        for outcome in outcomes:
            self._line(outcome)
            if outcome.severity in (Severity.FAIL, Severity.WARN):
                self._explain(outcome)
        self._console.print()

    def _line(self, outcome: Outcome) -> None:
        glyph, colour = GLYPH[outcome.severity]
        line = Text("    ")
        line.append(f"{glyph} ", style=colour)
        line.append(outcome.finding.summary)
        if outcome.severity is Severity.PASS and outcome.duration_ms >= 1:
            line.append(f"  {outcome.duration_ms:.0f}ms", style="bright_black")
        self._console.print(line)

    def _explain(self, outcome: Outcome) -> None:
        finding = outcome.finding
        body: list[Text] = []

        if finding.detail:
            body.append(Text(finding.detail))
        if finding.remedy:
            body.append(Text.assemble(("Fix: ", "bold"), finding.remedy))
        if finding.issue:
            issue = finding.issue
            body.append(
                Text.assemble(
                    ("Known issue: ", "bold"),
                    (issue.summarize(self._today), ""),
                    ("\n", ""),
                    (issue.url, "bright_black underline"),
                )
            )
        if not body:
            return

        border = "red" if outcome.severity is Severity.FAIL else "yellow"
        self._console.print(
            Padding(
                Panel(Group(*_spaced(body)), border_style=border, expand=True),
                (0, 0, 1, 6),
            )
        )

    def _pending(self) -> None:
        # Widest label decides the column so the descriptions line up.
        width = max(len(stage.label) for stage, _ in PENDING_STAGES)
        for stage, description in PENDING_STAGES:
            self._console.print(
                Text(f" {stage.value}  {stage.label:<{width}}   not implemented, {description}",
                     style="bright_black"),
                overflow="ellipsis",
                no_wrap=True,
            )
        self._console.print()

    def _footer(self, report: RunReport) -> None:
        failures, warnings = report.failures, report.warnings
        if failures:
            first = failures[0]
            plural = "s" if len(failures) > 1 else ""
            summary = Text.assemble(
                (f" {len(failures)} blocking failure{plural}", "bold red"),
                (f", first at {first.check.stage.label}. ", ""),
                ("Later stages were skipped because they depend on it.", "bright_black"),
            )
        elif warnings:
            summary = Text.assemble(
                (f" {len(warnings)} warning{'s' if len(warnings) > 1 else ''}", "bold yellow"),
                (", nothing blocking.", ""),
            )
        else:
            summary = Text(" All implemented gates passed.", style="bold green")

        self._console.print(summary)
        self._console.print(
            Text(" Stages 3-7 are not implemented, so this is not a green light "
                 "to trade.", style="bright_black")
        )
        self._console.print()


def _spaced(blocks: list[Text]) -> list[Text]:
    """Blank line between blocks, none trailing."""
    spaced: list[Text] = []
    for index, block in enumerate(blocks):
        if index:
            spaced.append(Text(""))
        spaced.append(block)
    return spaced
