"""The library entry point, exercised end to end with a stub probe.

api.run_onboard is the seam a market maker embeds, and it had no tests. These
drive it in-process with a canned transport so nothing touches the network.
"""

from __future__ import annotations

import httpx

from polymarket_doctor import run_onboard, to_json
from polymarket_doctor.render.json_report import build_report


def _transport(routes: dict[str, tuple[int, dict]]):
    """MockTransport routing by URL substring to (status, json)."""
    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, (status, body) in routes.items():
            if fragment in str(request.url):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": "no route"})
    return httpx.MockTransport(handler)


def _patch_probe(monkeypatch, transport):
    # api.run_onboard builds its own HttpxProbe; inject our transport into it.
    import polymarket_doctor.api as api
    from polymarket_doctor.net.http import HttpxProbe

    original = HttpxProbe.__init__

    def patched(self, *a, **k):
        k.pop("transport", None)
        original(self, *a, transport=transport, **k)

    monkeypatch.setattr(api.HttpxProbe, "__init__", patched)


def test_run_onboard_returns_a_report_in_process(monkeypatch):
    _patch_probe(monkeypatch, _transport({
        "/version": (200, {"version": 2}),
        "/time": (200, {}),  # non-epoch body -> clock check fails, fine here
    }))
    report = run_onboard(address="0x" + "1" * 40, rpc=None)

    assert report.outcomes  # it actually ran checks
    # rpc=None means no chain reader, so the funder kind is unknowable, not EOA.
    assert any(o.check.id == "identity.account-kind" for o in report.outcomes)


def test_rpc_none_skips_chain_reads(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "/version" in str(request.url):
            return httpx.Response(200, json={"version": 2})
        return httpx.Response(200, json={})

    _patch_probe(monkeypatch, httpx.MockTransport(handler))
    run_onboard(address="0x" + "1" * 40, funder="0x" + "2" * 40, rpc=None)

    # No JSON-RPC POST to a Polygon node when rpc is None.
    assert not any("publicnode" in u or "eth_" in u for u in seen)


def test_to_json_matches_build_report(monkeypatch):
    _patch_probe(monkeypatch, _transport({"/version": (200, {"version": 2})}))
    report = run_onboard(address="0x" + "1" * 40, rpc=None)

    host = "https://clob.polymarket.com"
    # Same content (timestamps aside, which build_report stamps internally).
    a = to_json(report, host=host)
    b = build_report(report, host=host)
    assert a["checks"] == b["checks"]
    assert a["summary"] == b["summary"]
    assert a["schema_version"] == b["schema_version"]


def test_two_concurrent_runs_are_independent(monkeypatch):
    # The library API builds fresh state per call; prove two runs don't share it.
    import threading

    _patch_probe(monkeypatch, _transport({"/version": (200, {"version": 2})}))
    results = []

    def go():
        results.append(run_onboard(address="0x" + "3" * 40, rpc=None))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    assert all(r.outcomes for r in results)
