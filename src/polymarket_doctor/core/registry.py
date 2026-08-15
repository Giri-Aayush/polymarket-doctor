"""Check registration and run ordering.

Ordering comes from the fact graph, not from stage numbers. Stages are how we
present results; dependencies are what actually constrain execution. Keeping
them separate means `check auth.key-identity` can pull in the two stage-1 checks
it needs without me maintaining a hand-written prerequisite list.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Sequence

from .check import Check
from .facts import Fact


class CheckGraphError(RuntimeError):
    """Raised when the registered checks can't be ordered."""


class Registry:
    """Holds every check and hands back a runnable order."""

    def __init__(self) -> None:
        self._checks: dict[str, Check] = {}

    def register(self, check: Check) -> Check:
        if check.id in self._checks:
            raise CheckGraphError(f"duplicate check id: {check.id}")
        self._checks[check.id] = check
        return check

    def add_all(self, checks: Iterable[Check]) -> None:
        for check in checks:
            self.register(check)

    def __len__(self) -> int:
        return len(self._checks)

    def __iter__(self):
        return iter(self._checks.values())

    def get(self, check_id: str) -> Check:
        try:
            return self._checks[check_id]
        except KeyError:
            raise CheckGraphError(f"no such check: {check_id}") from None

    def resolve(self, only: Sequence[str] | None = None) -> list[Check]:
        """Return checks in an order where every read happens after its write.

        With `only`, walks back through the graph and includes prerequisites —
        running one check in isolation is useless if the facts it reads were
        never produced.
        """
        selected = self._checks if not only else self._close_over_deps(only)
        return self._topological_sort(selected)

    def _producers(self) -> dict[Fact, str]:
        producers: dict[Fact, str] = {}
        for check in self._checks.values():
            for fact in check.writes:
                if fact in producers:
                    raise CheckGraphError(
                        f"{fact.value} is written by both {producers[fact]} "
                        f"and {check.id}; facts are write-once"
                    )
                producers[fact] = check.id
        return producers

    def _close_over_deps(self, roots: Sequence[str]) -> dict[str, Check]:
        producers = self._producers()
        wanted: dict[str, Check] = {}
        queue = deque(roots)
        while queue:
            check_id = queue.popleft()
            if check_id in wanted:
                continue
            check = self.get(check_id)
            wanted[check_id] = check
            for fact in check.reads:
                producer = producers.get(fact)
                # A fact with no producer is supplied by the CLI (an address,
                # say). Not an error — just nothing to schedule.
                if producer is not None and producer not in wanted:
                    queue.append(producer)
        return wanted

    def _topological_sort(self, selected: dict[str, Check]) -> list[Check]:
        producers = {
            fact: check.id
            for check in selected.values()
            for fact in check.writes
        }

        dependents: dict[str, set[str]] = defaultdict(set)
        indegree: dict[str, int] = {check_id: 0 for check_id in selected}
        for check in selected.values():
            for fact in check.reads:
                producer = producers.get(fact)
                if producer is None or producer == check.id:
                    continue
                if check.id not in dependents[producer]:
                    dependents[producer].add(check.id)
                    indegree[check.id] += 1

        # Kahn's, but pop the lowest (stage, id) so runs are deterministic and
        # results come out roughly in the order a partner experiences them.
        ready = sorted(
            (cid for cid, deg in indegree.items() if deg == 0),
            key=lambda cid: (selected[cid].stage, cid),
        )
        ordered: list[Check] = []
        while ready:
            check_id = ready.pop(0)
            ordered.append(selected[check_id])
            for dependent in sorted(dependents[check_id]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
            ready.sort(key=lambda cid: (selected[cid].stage, cid))

        if len(ordered) != len(selected):
            stuck = sorted(set(selected) - {c.id for c in ordered})
            raise CheckGraphError(f"cycle in check dependencies: {', '.join(stuck)}")
        return ordered
