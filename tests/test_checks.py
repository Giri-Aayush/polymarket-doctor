from __future__ import annotations

import time

import pytest
from conftest import EOA, StubProbe, http_error, ok, unreachable

from polymarket_doctor import issues
from polymarket_doctor.checks.auth import HmacBodyEncoding
from polymarket_doctor.checks.environment import ClobReachable, ClockSkew, ProtocolVersion
from polymarket_doctor.checks.identity import Addresses
from polymarket_doctor.core.check import Severity
from polymarket_doctor.core.facts import Fact


class TestReachability:
    def test_healthy_root_passes(self, make_context):
        ctx = make_context(StubProbe({"clob.polymarket.com": ok("OK")}))
        assert ClobReachable().run(ctx).severity is Severity.PASS
        assert ctx.facts.get(Fact.HOST) == ctx.endpoints.clob

    def test_connect_failure_reports_the_transport_error(self, make_context):
        ctx = make_context(StubProbe({"clob.polymarket.com": unreachable()}))
        finding = ClobReachable().run(ctx)
        assert finding.severity is Severity.FAIL
        assert "ConnectError" in finding.detail

    def test_edge_403_points_at_the_bot_challenge(self, make_context):
        # Cloudflare challenges some datacenter ranges. Surfaces later as a 403
        # on key creation, which reads like auth and isn't.
        ctx = make_context(StubProbe({"clob.polymarket.com": http_error(403, "<html>")}))
        finding = ClobReachable().run(ctx)
        assert finding.severity is Severity.FAIL
        assert finding.issue is issues.CLOUDFLARE_BLOCKS_KEY_CREATION


class TestProtocolVersion:
    def test_v2_passes(self, make_context):
        ctx = make_context(StubProbe({"/version": ok({"version": 2})}))
        ctx.facts.set(Fact.HOST, ctx.endpoints.clob)
        assert ProtocolVersion().run(ctx).severity is Severity.PASS

    def test_v1_host_is_a_blocking_failure(self, make_context):
        ctx = make_context(StubProbe({"/version": ok({"version": 1})}))
        ctx.facts.set(Fact.HOST, ctx.endpoints.clob)
        finding = ProtocolVersion().run(ctx)
        assert finding.severity is Severity.FAIL
        assert finding.issue is issues.ORDER_VERSION_MISMATCH


class TestClockSkew:
    def test_synced_clock_passes(self, make_context):
        ctx = make_context(StubProbe({"/time": ok(int(time.time()))}))
        finding = ClockSkew().run(ctx)
        assert finding.severity is Severity.PASS
        assert abs(ctx.facts.get(Fact.CLOCK_SKEW_SECONDS)) < 5

    def test_bare_integer_body_is_accepted(self, make_context):
        # /time answers with a plain integer, not JSON. A client that insists on
        # json.loads gets an exception instead of a number.
        ctx = make_context(StubProbe({"/time": ok(str(int(time.time())))}))
        assert ClockSkew().run(ctx).severity is Severity.PASS

    def test_large_drift_fails(self, make_context):
        ctx = make_context(StubProbe({"/time": ok(int(time.time()) - 120)}))
        finding = ClockSkew().run(ctx)
        assert finding.severity is Severity.FAIL
        assert "clock" in finding.remedy.lower()

    def test_small_drift_only_warns(self, make_context):
        ctx = make_context(StubProbe({"/time": ok(int(time.time()) - 10)}))
        assert ClockSkew().run(ctx).severity is Severity.WARN

    def test_garbage_body_is_a_failure_not_a_crash(self, make_context):
        ctx = make_context(StubProbe({"/time": ok("<html>nope</html>")}))
        assert ClockSkew().run(ctx).severity is Severity.FAIL


class TestAddresses:
    def test_missing_address_is_actionable(self, make_context):
        finding = Addresses().run(make_context(StubProbe()))
        assert finding.severity is Severity.FAIL
        assert "--address" in finding.remedy

    def test_address_is_checksummed(self, make_context):
        ctx = make_context(StubProbe(), signer_address=EOA.lower())
        assert Addresses().run(ctx).severity is Severity.PASS
        assert ctx.facts.get(Fact.SIGNER_ADDRESS) == EOA

    def test_funder_defaults_to_the_signer(self, make_context):
        ctx = make_context(StubProbe(), signer_address=EOA)
        Addresses().run(ctx)
        assert ctx.facts.get(Fact.FUNDER_ADDRESS) == EOA

    @pytest.mark.parametrize("bad", ["0xdeadbeef", "not-an-address", "", "0x" + "z" * 40])
    def test_malformed_addresses_are_rejected(self, make_context, bad):
        ctx = make_context(StubProbe(), signer_address=bad)
        assert Addresses().run(ctx).severity is Severity.FAIL


class TestHmacBodyEncoding:
    def test_flags_bool_and_null_payloads(self, make_context):
        ctx = make_context(StubProbe())
        ctx.facts.set(Fact.SDK, None)
        finding = HmacBodyEncoding().run(ctx)

        assert finding.severity is Severity.WARN
        assert finding.issue is issues.HMAC_BODY_SERIALIZATION
        assert set(finding.evidence["affected"]) == {"booleans", "nulls"}

    def test_makes_no_network_calls(self, make_context):
        probe = StubProbe()
        ctx = make_context(probe)
        ctx.facts.set(Fact.SDK, None)
        HmacBodyEncoding().run(ctx)
        assert probe.calls == []
