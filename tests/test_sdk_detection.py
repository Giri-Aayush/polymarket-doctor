"""SDK detection drives stage 1's ERC-7739 verdict, so it has to be right."""

from __future__ import annotations

import importlib.metadata as metadata

import pytest
from conftest import StubProbe

from polymarket_doctor import issues
from polymarket_doctor.checks import environment
from polymarket_doctor.checks.environment import SdkGeneration
from polymarket_doctor.core.check import Severity
from polymarket_doctor.core.facts import Fact


@pytest.fixture
def installed(monkeypatch):
    """Pretend a given set of distributions is installed."""
    def _install(versions: dict[str, str]):
        def fake_version(name: str) -> str:
            try:
                return versions[name]
            except KeyError:
                raise metadata.PackageNotFoundError(name) from None
        monkeypatch.setattr(environment.metadata, "version", fake_version)
    return _install


def test_no_sdk_warns_but_keeps_going(make_context, installed):
    installed({})
    ctx = make_context(StubProbe())

    finding = SdkGeneration().run(ctx)

    assert finding.severity is Severity.WARN
    assert ctx.facts.get(Fact.SDK) is None


def test_v2_client_passes(make_context, installed):
    installed({"py-clob-client-v2": "1.1.0"})
    ctx = make_context(StubProbe())

    finding = SdkGeneration().run(ctx)

    assert finding.severity is Severity.PASS
    assert ctx.facts.get(Fact.SDK)["module"] == "py_clob_client_v2"


def test_archived_v1_client_fails(make_context, installed):
    # v1 signs the V1 order struct. The V2 exchange rejects it outright, and
    # the repo is archived so there's nowhere to report it.
    installed({"py-clob-client": "0.34.6"})
    ctx = make_context(StubProbe())

    finding = SdkGeneration().run(ctx)

    assert finding.severity is Severity.FAIL
    assert finding.issue is issues.V1_SDK_ARCHIVED
    assert "archived" in finding.detail


def test_unified_sdk_is_preferred_when_several_are_present(make_context, installed):
    installed({"polymarket": "0.3.0", "py-clob-client-v2": "1.1.0"})
    ctx = make_context(StubProbe())

    finding = SdkGeneration().run(ctx)

    assert finding.severity is Severity.WARN
    assert "side by side" in finding.summary
    assert ctx.facts.get(Fact.SDK)["module"] == "polymarket"


def test_v1_alongside_v2_still_fails_on_v1(make_context, installed):
    # Preference order puts v2 first, so this reports the shadowing warning
    # rather than the v1 failure. Documented here because it's a real tradeoff:
    # the import that wins depends on the caller's path, not on our ordering.
    installed({"py-clob-client-v2": "1.1.0", "py-clob-client": "0.34.6"})
    ctx = make_context(StubProbe())

    finding = SdkGeneration().run(ctx)

    assert finding.severity is Severity.WARN
    assert "py-clob-client" in finding.detail
