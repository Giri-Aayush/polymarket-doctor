"""The JSON contract market makers parse. Shape and safety both matter."""

from __future__ import annotations

import datetime
import json

from conftest import StubProbe

from polymarket_doctor.core.check import Check, Finding, Stage
from polymarket_doctor.core.context import Credentials
from polymarket_doctor.core.facts import Fact
from polymarket_doctor.core.registry import Registry
from polymarket_doctor.core.runner import Runner
from polymarket_doctor.render.json_report import SCHEMA_VERSION, build_report

FROZEN = datetime.datetime(2026, 8, 16, 12, 0, tzinfo=datetime.timezone.utc)


def _check(check_id, finding, *, writes=frozenset(), stage=Stage.ENVIRONMENT):
    return type(
        f"C_{check_id.replace('.', '_')}",
        (Check,),
        {"id": check_id, "stage": stage, "title": check_id,
         "reads": frozenset(), "writes": writes, "run": lambda self, ctx: finding},
    )()


def _report(make_context, *checks):
    registry = Registry()
    for c in checks:
        registry.register(c)
    return Runner(registry).run(make_context(StubProbe()))


def test_document_has_the_stable_top_level_shape(make_context):
    report = _report(make_context, _check("env.reachable", Finding.ok("up")))
    doc = build_report(report, host="https://clob.polymarket.com", now=FROZEN)

    assert doc["schema_version"] == SCHEMA_VERSION
    assert doc["tool"] == "polymarket-doctor"
    assert doc["generated_at"] == "2026-08-16T12:00:00+00:00"
    assert doc["host"] == "https://clob.polymarket.com"
    assert doc["ok"] is True
    assert doc["exit_code"] == 0
    assert doc["summary"] == {
        "total": 1, "passed": 1, "warnings": 0, "failures": 0, "skipped": 0}


def test_a_failure_flips_ok_and_exit_code(make_context):
    report = _report(make_context, _check("x", Finding.fail("boom")))
    doc = build_report(report, host="h", now=FROZEN)
    assert doc["ok"] is False
    assert doc["exit_code"] == 1
    assert doc["summary"]["failures"] == 1


def test_the_whole_document_is_json_serializable(make_context):
    # Facts hold enums and dataclasses; a naive json.dumps would raise. This is
    # the property an MM's parser depends on.
    from polymarket_doctor.checks.identity import SignatureType
    from polymarket_doctor.net.chain import ContractProfile

    finding = Finding.ok("classified", signature_type=SignatureType.POLY_GNOSIS_SAFE)

    def run(self, ctx):
        ctx.facts.set(Fact.SIGNATURE_TYPE, SignatureType.POLY_GNOSIS_SAFE)
        ctx.facts.set(Fact.FUNDER_PROFILE,
                      ContractProfile(has_code=True, safe_version="1.3.0", owners=("0xabc",)))
        return finding

    check = type("C", (Check,), {
        "id": "identity.account-kind", "stage": Stage.IDENTITY, "title": "t",
        "reads": frozenset(), "writes": frozenset({Fact.SIGNATURE_TYPE, Fact.FUNDER_PROFILE}),
        "run": run})()

    doc = build_report(_report(make_context, check), host="h", now=FROZEN)
    encoded = json.dumps(doc)  # must not raise

    reparsed = json.loads(encoded)
    assert reparsed["facts"]["identity.signature_type"] == 2  # enum -> value
    assert reparsed["facts"]["identity.funder_profile"]["safe_version"] == "1.3.0"


def test_credentials_never_reach_the_json(make_context):
    # The redacted() evidence is what checks store; assert the raw secret is
    # nowhere in the serialized document.
    creds = Credentials(api_key="k", secret="TOP-SECRET-VALUE", passphrase="PASS-PHRASE-VALUE")
    finding = Finding.ok("creds ok", **creds.redacted())
    doc = build_report(_report(make_context, _check("auth.credentials", finding)),
                       host="h", now=FROZEN)
    blob = json.dumps(doc)
    assert "TOP-SECRET-VALUE" not in blob
    assert "PASS-PHRASE-VALUE" not in blob


def test_issue_citation_is_rendered_with_state(make_context):
    from polymarket_doctor import issues

    finding = Finding.warn("hmac", issue=issues.HMAC_BODY_SERIALIZATION)
    doc = build_report(_report(make_context, _check("auth.hmac-body", finding)),
                       host="h", now=FROZEN)
    issue = doc["checks"][0]["issue"]
    assert issue["slug"] == "py-clob-client-v2#108"
    assert issue["url"].endswith("/108")
    assert issue["state"] in ("open", "closed")


def test_safe_coerces_bytes_plain_enums_and_cycles():
    # The three cases the direct tests missed: bytes -> hex, a non-int Enum,
    # and a self-referential container that must not RecursionError.
    from polymarket_doctor.core.check import Stage  # a plain (int) Enum is Stage
    from polymarket_doctor.render.json_report import _safe

    assert _safe(b"\x00\x01\xff") == "0x0001ff"
    assert _safe(Stage.AUTH) == Stage.AUTH.value

    cycle: dict = {}
    cycle["self"] = cycle
    out = _safe(cycle)
    assert out["self"] == "<circular>"
    json.dumps(out)  # must be serializable

    deep = {"a": [{"b": (1, b"\x2a")}]}
    assert _safe(deep) == {"a": [{"b": [1, "0x2a"]}]}
