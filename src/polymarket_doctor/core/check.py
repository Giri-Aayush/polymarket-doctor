"""Check protocol and the shape of what a check returns."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from .facts import Fact

if TYPE_CHECKING:
    from ..issues import KnownIssue
    from .context import Context


class Stage(int, Enum):
    """Onboarding stages, ordered the way a partner actually hits them.

    The numbering is load-bearing: the runner reports stages in this order and
    a blocking failure in stage N skips everything above N.
    """

    ENVIRONMENT = 0
    IDENTITY = 1
    AUTH = 2
    FUNDING = 3
    MARKET_LIMITS = 4
    ORDER_DRY_RUN = 5
    WEBSOCKET = 6
    RFQ = 7

    @property
    def label(self) -> str:
        return self.name.lower().replace("_", " ")


class Severity(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"

    @property
    def is_blocking(self) -> bool:
        return self is Severity.FAIL


@dataclass(frozen=True, slots=True)
class Finding:
    """What a check concluded.

    `detail` is the paragraph a partner reads when something breaks, so it
    should explain the mechanism, not restate the title. `remedy` is the next
    action they take. Both are optional because a passing check needs neither.
    """

    severity: Severity
    summary: str
    detail: str | None = None
    remedy: str | None = None
    issue: KnownIssue | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, summary: str, **evidence: Any) -> Finding:
        return cls(Severity.PASS, summary, evidence=evidence)

    @classmethod
    def warn(cls, summary: str, detail: str | None = None, remedy: str | None = None,
             issue: KnownIssue | None = None, **evidence: Any) -> Finding:
        return cls(Severity.WARN, summary, detail, remedy, issue, evidence)

    @classmethod
    def fail(cls, summary: str, detail: str | None = None, remedy: str | None = None,
             issue: KnownIssue | None = None, **evidence: Any) -> Finding:
        return cls(Severity.FAIL, summary, detail, remedy, issue, evidence)


class Check(abc.ABC):
    """One gate a partner has to get through.

    Subclasses set `reads`/`writes` to whatever facts they touch. The registry
    uses those to order the run, so a check that forgets to declare a read will
    see `None` from the store rather than a stale value from a previous run.
    """

    id: str
    stage: Stage
    title: str
    reads: frozenset[Fact] = frozenset()
    writes: frozenset[Fact] = frozenset()

    @abc.abstractmethod
    def run(self, ctx: Context) -> Finding:
        """Do the work. Raising is fine — the runner turns it into a FAIL."""

    def __repr__(self) -> str:  # keeps pytest failure output readable
        return f"<{type(self).__name__} {self.id}>"
