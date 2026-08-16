"""Direct unit tests for the small helper functions and edge branches.

These are the None/fallback paths inside each check module: the shapes a real
API can return that the happy-path tests don't exercise. Testing the helpers
directly is faster and clearer than contriving a stub for each.
"""

from __future__ import annotations

from decimal import Decimal

from conftest import StubProbe, http_error, ok

from polymarket_doctor.checks.environment import (
    ClockSkew,
    ProtocolVersion,
    _coerce_epoch_seconds,
)
from polymarket_doctor.checks.funding import _approvals, _parse_balance
from polymarket_doctor.checks.identity import SignatureType
from polymarket_doctor.checks.order_dryrun import _best_bid, _floor_size
from polymarket_doctor.checks.websocket_feed import _first_event
from polymarket_doctor.core.check import Severity
from polymarket_doctor.core.facts import Fact
from polymarket_doctor.net.endpoints import CLOB, DEPRECATED_HOSTS, Endpoints


class TestEpochCoercion:
    def test_dict_wrapper_is_unwrapped(self):
        assert _coerce_epoch_seconds({"time": 1786800000}) == 1786800000

    def test_uncoercible_value_is_none(self):
        assert _coerce_epoch_seconds(object()) is None
        assert _coerce_epoch_seconds(["nope"]) is None
        assert _coerce_epoch_seconds(True) is None  # bool is not a timestamp


class TestFundingParsers:
    def test_non_mapping_body_parses_to_none(self):
        assert _parse_balance("not a dict") is None
        assert _approvals(["nope"]) is None

    def test_single_allowance_is_normalized_to_a_dict(self):
        assert _approvals({"allowance": "1000"}) == {"exchange": "1000"}


class TestFloorSize:
    def test_none_falls_back_to_the_exchange_minimum(self):
        assert _floor_size(None) == Decimal(5)

    def test_a_larger_market_minimum_wins(self):
        assert _floor_size("10") == Decimal(10)


class TestBestBid:
    def test_non_dict_book_has_no_bid(self):
        assert _best_bid("nope") is None

    def test_bids_that_are_not_a_list(self):
        assert _best_bid({"bids": "oops"}) is None

    def test_skips_malformed_entries_and_takes_the_max(self):
        book = {"bids": ["junk", {"price": "not-a-number"}, {"price": "0.03"},
                          {"price": "0.16"}, {"price": "0"}]}
        assert _best_bid(book) == Decimal("0.16")


class TestFirstEvent:
    def test_unparseable_frame_is_none(self):
        assert _first_event("this is not json") is None


class TestSignatureTypeLabel:
    def test_label_shows_name_and_value(self):
        assert SignatureType.EOA.label == "EOA (0)"
        assert SignatureType.POLY_GNOSIS_SAFE.label == "POLY_GNOSIS_SAFE (2)"


class TestProtocolVersionBranches:
    def _ctx(self, make_context, body, status=200, host=None):
        endpoints = Endpoints(clob=host) if host else Endpoints()
        probe = StubProbe({"/version": ok(body) if status == 200 else http_error(status, body)})
        ctx = make_context(probe)
        ctx.endpoints = endpoints
        ctx.facts.set(Fact.HOST, endpoints.clob)
        return ctx

    def test_unreadable_version_fails(self, make_context):
        ctx = self._ctx(make_context, "oops", status=500)
        assert ProtocolVersion().run(ctx).severity is Severity.FAIL

    def test_missing_version_field_fails(self, make_context):
        assert ProtocolVersion().run(self._ctx(make_context, {})).severity is Severity.FAIL

    def test_deprecated_host_warns_even_on_v2(self, make_context):
        # A host in the deprecated table still warns, so the user re-points it.
        assert DEPRECATED_HOSTS, "expected at least one deprecated host on record"
        host = next(iter(DEPRECATED_HOSTS))
        finding = ProtocolVersion().run(self._ctx(make_context, {"version": 2}, host=host))
        assert finding.severity is Severity.WARN
        assert CLOB in finding.remedy


class TestClockSkewBranch:
    def test_unreadable_time_fails(self, make_context):
        probe = StubProbe({"/time": http_error(503)})
        ctx = make_context(probe)
        ctx.facts.set(Fact.HOST, ctx.endpoints.clob)
        assert ClockSkew().run(ctx).severity is Severity.FAIL


class TestMarketLimitHelpers:
    def test_first_token_id_rejects_non_list_and_missing(self):
        from polymarket_doctor.checks.market_limits import _first_token_id
        assert _first_token_id({"clobTokenIds": "not-a-json-list"}) is None
        assert _first_token_id({"clobTokenIds": "[]"}) is None  # list, but empty
        assert _first_token_id({"clobTokenIds": "[123, \"tok\"]"}) == "tok"  # first string

    def test_number_rejects_bool_and_non_numeric(self):
        from polymarket_doctor.checks.market_limits import _number
        assert _number(ok({"x": True}), "x") is None
        assert _number(ok({"x": "0.01"}), "x") is None
        assert _number(http_error(500), "x") is None
        assert _number(ok({"x": 0.01}), "x") == 0.01


class TestDryRunMakerRounding:
    def test_a_fractional_maker_amount_is_a_violation(self):
        from polymarket_doctor.checks.order_dryrun import evaluate
        # price 0.0000005 * size 5 * 1e6 = 2.5 base units — not whole.
        candidate = {"price": "0.0000005", "size": "5", "makerAmount": "2",
                     "takerAmount": "5000000"}
        titles = [v.invariant for v in evaluate(candidate, tick=Decimal("0.001"),
                                           min_size=Decimal(5))]
        assert any("maker amount" in t for t in titles)


class TestIdentityBadFunder:
    def test_an_unparseable_funder_is_reported(self, make_context):
        from polymarket_doctor.checks.identity import Addresses
        ctx = make_context(StubProbe(), signer_address="0x" + "1" * 40,
                           funder_address="not-an-address")
        assert Addresses().run(ctx).severity is Severity.FAIL


class TestHmacBodyAllSafe:
    def test_all_safe_samples_pass(self, make_context, monkeypatch):
        from polymarket_doctor.checks import auth
        check = auth.HmacBodyEncoding()
        # Replace the samples with only JSON-safe ones so the pass branch runs.
        monkeypatch.setattr(check, "SAMPLES", (("strings", {"a": "b"}),))
        ctx = make_context(StubProbe())
        ctx.facts.set(Fact.SDK, None)
        assert check.run(ctx).severity is Severity.PASS


class TestResolveTokenMinSizeFallback:
    def test_discovered_market_with_bad_min_size_falls_back(self, make_context):
        from polymarket_doctor.checks.market_limits import ResolveToken
        market = {"slug": "m", "clobTokenIds": "[\"tok\"]", "orderMinSize": True}
        ctx = make_context(StubProbe({"/markets": ok([market])}))
        finding = ResolveToken().run(ctx)
        assert finding.severity is Severity.PASS
        assert ctx.facts.get(Fact.MIN_ORDER_SIZE) == 5


class TestCoreSmallBranches:
    def test_check_repr_is_readable(self):
        from polymarket_doctor.checks import default_registry
        check = next(iter(default_registry()))
        assert check.id in repr(check)

    def test_factstore_is_iterable(self):
        from polymarket_doctor.core.facts import Fact, FactStore
        store = FactStore()
        store.set(Fact.HOST, "h")
        assert list(store) == [Fact.HOST]

    def test_registry_get_unknown_id_raises_check_graph_error(self):
        import pytest

        from polymarket_doctor.core.registry import CheckGraphError, Registry
        with pytest.raises(CheckGraphError, match="no such check"):
            Registry().get("nope")

    def test_first_failure_is_none_when_all_pass(self):
        from polymarket_doctor.core.runner import RunReport
        assert RunReport().first_failure() is None

    def test_data_url_joins_the_path(self):
        from polymarket_doctor.net.endpoints import Endpoints
        assert Endpoints().data_url("/trades") == "https://data-api.polymarket.com/trades"


class TestRegistryDependencyClosure:
    def test_selecting_a_check_twice_is_idempotent(self):
        # Exercises the "already wanted" continue in _close_over_deps.
        from polymarket_doctor.checks import default_registry
        registry = default_registry()
        resolved = registry.resolve(only=["auth.key-identity", "auth.key-identity"])
        assert [c.id for c in resolved].count("auth.key-identity") == 1


class TestRemainingBranches:
    def test_sdk_present_but_not_importable_is_skipped(self, monkeypatch):
        # A distribution can be installed while its module isn't importable
        # (broken install / name collision). That entry must be skipped.
        import importlib.metadata as md

        from polymarket_doctor.checks import environment
        monkeypatch.setattr(environment.metadata, "version",
                            lambda name: "1.0" if name == "polymarket-client" else _raise(md, name))
        monkeypatch.setattr(environment.importlib.util, "find_spec", lambda module: None)
        assert environment._installed_sdks() == []

    def test_first_token_id_rejects_valid_json_that_is_not_a_list(self):
        from polymarket_doctor.checks.market_limits import _first_token_id
        assert _first_token_id({"clobTokenIds": "{\"a\": 1}"}) is None


def _raise(md, name):
    raise md.PackageNotFoundError(name)
