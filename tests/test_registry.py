from __future__ import annotations

import pytest

from polymarket_doctor.core.check import Check, Finding, Stage
from polymarket_doctor.core.facts import Fact, FactStore
from polymarket_doctor.core.registry import CheckGraphError, Registry


def make_check(check_id: str, *, reads=frozenset(), writes=frozenset(),
               stage=Stage.ENVIRONMENT) -> Check:
    return type(
        f"Check_{check_id.replace('.', '_').replace('-', '_')}",
        (Check,),
        {
            "id": check_id,
            "stage": stage,
            "title": check_id,
            "reads": reads,
            "writes": writes,
            "run": lambda self, ctx: Finding.ok("ok"),
        },
    )()


def test_reader_runs_after_writer_regardless_of_registration_order():
    registry = Registry()
    registry.register(make_check("reader", reads={Fact.HOST}, stage=Stage.AUTH))
    registry.register(make_check("writer", writes={Fact.HOST}))

    assert [c.id for c in registry.resolve()] == ["writer", "reader"]


def test_cycle_is_rejected():
    registry = Registry()
    registry.register(make_check("a", reads={Fact.HOST}, writes={Fact.SDK}))
    registry.register(make_check("b", reads={Fact.SDK}, writes={Fact.HOST}))

    with pytest.raises(CheckGraphError, match="cycle"):
        registry.resolve()


def test_two_checks_cannot_write_the_same_fact():
    registry = Registry()
    registry.register(make_check("a", writes={Fact.HOST}))
    registry.register(make_check("b", writes={Fact.HOST}))

    with pytest.raises(CheckGraphError, match="write-once"):
        registry.resolve(only=["a"])


def test_duplicate_ids_are_rejected_at_registration():
    registry = Registry()
    registry.register(make_check("a"))
    with pytest.raises(CheckGraphError, match="duplicate"):
        registry.register(make_check("a"))


def test_selecting_one_check_pulls_in_its_prerequisites():
    registry = Registry()
    registry.register(make_check("root", writes={Fact.HOST}))
    registry.register(make_check("middle", reads={Fact.HOST}, writes={Fact.SDK}))
    registry.register(make_check("leaf", reads={Fact.SDK}, stage=Stage.AUTH))
    registry.register(make_check("unrelated", stage=Stage.RFQ))

    resolved = [c.id for c in registry.resolve(only=["leaf"])]

    assert resolved == ["root", "middle", "leaf"]
    assert "unrelated" not in resolved


def test_unproduced_fact_is_not_an_error():
    # SIGNER_ADDRESS comes from the CLI, so nothing in the graph writes it.
    registry = Registry()
    registry.register(make_check("solo", reads={Fact.SIGNER_ADDRESS}))
    assert [c.id for c in registry.resolve()] == ["solo"]


def test_ordering_is_stable_across_runs():
    def build() -> list[str]:
        registry = Registry()
        for name in ("d", "c", "b", "a"):
            registry.register(make_check(name))
        return [c.id for c in registry.resolve()]

    assert build() == build() == ["a", "b", "c", "d"]


def test_facts_are_write_once():
    store = FactStore()
    store.set(Fact.HOST, "https://clob.polymarket.com")
    store.set(Fact.HOST, "https://clob.polymarket.com")  # idempotent, fine

    with pytest.raises(ValueError, match="refusing to overwrite"):
        store.set(Fact.HOST, "https://example.invalid")
