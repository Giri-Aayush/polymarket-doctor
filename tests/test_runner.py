from __future__ import annotations

from conftest import StubProbe

from polymarket_doctor.core.check import Check, Finding, Severity, Stage
from polymarket_doctor.core.facts import Fact
from polymarket_doctor.core.registry import Registry
from polymarket_doctor.core.runner import Runner


def make_check(check_id, finding, *, reads=frozenset(), writes=frozenset(),
               stage=Stage.ENVIRONMENT, on_run=None):
    def run(self, ctx):
        if on_run is not None:
            on_run()
        return finding

    return type(
        f"Check_{check_id.replace('.', '_')}",
        (Check,),
        {"id": check_id, "stage": stage, "title": check_id,
         "reads": reads, "writes": writes, "run": run},
    )()


def test_dependents_of_a_failure_are_skipped_not_run(make_context):
    ran = []
    registry = Registry()
    registry.register(make_check("upstream", Finding.fail("boom"), writes={Fact.HOST}))
    registry.register(make_check(
        "downstream", Finding.ok("never"), reads={Fact.HOST}, stage=Stage.AUTH,
        on_run=lambda: ran.append("downstream"),
    ))

    report = Runner(registry).run(make_context(StubProbe()))

    assert ran == []  # the point: we don't run it and report a bogus failure
    severities = {o.check.id: o.severity for o in report.outcomes}
    assert severities == {"upstream": Severity.FAIL, "downstream": Severity.SKIP}


def test_skip_propagates_transitively(make_context):
    registry = Registry()
    registry.register(make_check("a", Finding.fail("boom"), writes={Fact.HOST}))
    registry.register(make_check("b", Finding.ok("x"), reads={Fact.HOST},
                                 writes={Fact.SDK}, stage=Stage.IDENTITY))
    registry.register(make_check("c", Finding.ok("x"), reads={Fact.SDK}, stage=Stage.AUTH))

    report = Runner(registry).run(make_context(StubProbe()))

    assert [o.severity for o in report.outcomes] == [
        Severity.FAIL, Severity.SKIP, Severity.SKIP,
    ]


def test_independent_checks_still_run_after_a_failure(make_context):
    registry = Registry()
    registry.register(make_check("failing", Finding.fail("boom"), writes={Fact.HOST}))
    registry.register(make_check("independent", Finding.ok("fine"), stage=Stage.AUTH))

    report = Runner(registry).run(make_context(StubProbe()))

    by_id = {o.check.id: o.severity for o in report.outcomes}
    assert by_id["independent"] is Severity.PASS


def test_a_crashing_check_becomes_a_failure(make_context):
    def explode():
        raise RuntimeError("kaboom")

    registry = Registry()
    registry.register(make_check("crasher", Finding.ok("unused"), on_run=explode))

    report = Runner(registry).run(make_context(StubProbe()))
    finding = report.outcomes[0].finding

    assert finding.severity is Severity.FAIL
    assert "RuntimeError" in finding.summary
    assert "bug in polymarket-doctor" in finding.remedy


def test_warnings_do_not_set_a_failing_exit_code(make_context):
    registry = Registry()
    registry.register(make_check("warner", Finding.warn("heads up")))

    report = Runner(registry).run(make_context(StubProbe()))

    assert report.warnings and not report.blocked
    assert report.exit_code() == 0


def test_failure_sets_a_failing_exit_code(make_context):
    registry = Registry()
    registry.register(make_check("failer", Finding.fail("nope")))

    assert Runner(registry).run(make_context(StubProbe())).exit_code() == 1
