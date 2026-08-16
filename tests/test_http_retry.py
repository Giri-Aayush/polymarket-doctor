"""Transient failures get retried; real answers don't. Both matter in prod.

Uses httpx.MockTransport so nothing touches the network and the retry path is
exercised deterministically. sleep is stubbed so the backoff adds no wall time.
"""

from __future__ import annotations

import httpx

from polymarket_doctor.net.http import HttpxProbe


def _probe(responses, retries=2):
    """A probe whose transport yields the given responses in order."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        item = responses[i]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item, json={"ok": True})

    probe = HttpxProbe(retries=retries, transport=httpx.MockTransport(handler),
                       sleep=lambda _s: None)
    return probe, calls


def test_a_transient_502_is_retried_then_succeeds():
    probe, calls = _probe([502, 502, 200])
    with probe:
        response = probe.get("https://x.test/thing")
    assert response.ok
    assert response.attempts == 3
    assert calls["n"] == 3


def test_a_connect_error_is_retried():
    probe, calls = _probe([httpx.ConnectError("refused"), 200])
    with probe:
        response = probe.get("https://x.test/thing")
    assert response.ok
    assert response.attempts == 2


def test_retries_are_bounded_and_then_it_gives_up():
    probe, _ = _probe([503, 503, 503, 503, 503], retries=2)
    with probe:
        response = probe.get("https://x.test/thing")
    assert not response.ok
    assert response.status == 503
    assert response.attempts == 3  # initial + 2 retries


def test_a_real_4xx_is_not_retried():
    # A 401 is the exchange's answer, not a blip. Retrying it wastes time and
    # could mask the diagnosis.
    probe, calls = _probe([401, 200])
    with probe:
        response = probe.get("https://x.test/thing")
    assert response.status == 401
    assert response.attempts == 1
    assert calls["n"] == 1


def test_posts_are_never_retried():
    # A blind POST retry could double-submit an order or quote.
    probe, calls = _probe([503, 200])
    with probe:
        response = probe.post("https://x.test/order", json_body={})
    assert response.status == 503
    assert calls["n"] == 1


def test_transport_error_then_5xx_then_success_all_retried():
    # The two retry triggers in one sequence, which the isolated tests missed.
    probe, _ = _probe([httpx.ConnectError("refused"), 503, 200], retries=2)
    with probe:
        response = probe.get("https://x.test/thing")
    assert response.ok
    assert response.attempts == 3


def test_non_json_body_is_returned_as_truncated_text():
    # Cloudflare/nginx HTML error pages: a check relies on getting text, not a
    # JSON-decode crash.
    html = "<html>" + "x" * 1000 + "</html>"

    def handler(request):
        return httpx.Response(503, text=html)

    probe = HttpxProbe(retries=0, transport=httpx.MockTransport(handler),
                       sleep=lambda _s: None)
    with probe:
        response = probe.get("https://x.test/thing")
    assert isinstance(response.body, str)
    assert len(response.body) <= 500
