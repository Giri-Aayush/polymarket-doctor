from __future__ import annotations

import base64

import pytest

from polymarket_doctor.signing import (
    SecretFormatError,
    body_is_hmac_safe,
    build_hmac_signature,
    build_l2_headers,
    canonical_body,
)

# 32 bytes so the value is stable and readable if a vector ever needs re-deriving.
SECRET = base64.urlsafe_b64encode(b"polymarket-doctor-test-secret-32").decode()
TIMESTAMP = 1786808048


def test_signature_matches_pinned_vector():
    # Pinned so a refactor of the message layout can't pass silently. Derived
    # from py-clob-client-v2's signing/hmac.py, which is what the server checks
    # against.
    assert build_hmac_signature(SECRET, TIMESTAMP, "GET", "/auth/api-keys") == (
        "4dsCjuiLVZ5-z4oEfG9O0dDoQ6zP8B5qMwlCKPP0ZJ0="
    )


def test_body_participates_in_the_signature():
    assert build_hmac_signature(SECRET, TIMESTAMP, "POST", "/order", {"orderID": "0xabc"}) == (
        "P7f6GwjcBklO-_MvpDpZJVKGN0bLuSh9V_uUAHqfuOU="
    )


def test_signature_is_url_safe_base64():
    # A '+' or '/' in a header value is what breaks people who route through a
    # proxy that re-encodes; url-safe alphabet is the whole point.
    signature = build_hmac_signature(SECRET, TIMESTAMP, "GET", "/auth/api-keys")
    assert "+" not in signature and "/" not in signature


def test_empty_body_is_omitted_not_signed_as_none():
    assert (
        build_hmac_signature(SECRET, TIMESTAMP, "GET", "/x")
        == build_hmac_signature(SECRET, TIMESTAMP, "GET", "/x", None)
        == build_hmac_signature(SECRET, TIMESTAMP, "GET", "/x", {})
    )


def test_non_base64_secret_is_reported_clearly():
    with pytest.raises(SecretFormatError, match="url-safe base64"):
        build_hmac_signature("not base64 at all!!", TIMESTAMP, "GET", "/x")


class TestBodyEncoding:
    """py-clob-client-v2#108: repr-with-swapped-quotes is not JSON."""

    def test_python_literals_leak_into_the_payload(self):
        assert canonical_body({"a": True}) == '{"a": True}'

    @pytest.mark.parametrize("body", [
        {"orderID": "0xabc"},
        {"nested": {"a": "b"}},
        [{"a": "b"}],
        {"size": 5},
    ])
    def test_json_compatible_bodies_are_safe(self, body):
        assert body_is_hmac_safe(body)

    @pytest.mark.parametrize("body", [
        {"negRisk": True},
        {"owner": None},
        {"a": "b", "flag": False},
    ])
    def test_bools_and_nulls_are_not(self, body):
        assert not body_is_hmac_safe(body)

    def test_no_body_is_safe(self):
        assert body_is_hmac_safe(None)


def test_l2_headers_carry_the_address_not_the_api_key():
    # Putting the api_key in POLY_ADDRESS is the classic 401 that tells you
    # nothing. Pin the mapping so it can't regress.
    headers = build_l2_headers(
        address="0x9F49475F9496c77fa95f76c7C5Bc57467B336792",
        api_key="key-123",
        secret=SECRET,
        passphrase="pass-123",
        timestamp=TIMESTAMP,
        method="GET",
        request_path="/auth/api-keys",
    )
    assert headers["POLY_ADDRESS"] == "0x9F49475F9496c77fa95f76c7C5Bc57467B336792"
    assert headers["POLY_API_KEY"] == "key-123"
    assert headers["POLY_TIMESTAMP"] == str(TIMESTAMP)
    assert "POLY_SIGNATURE" in headers


def test_timestamp_header_is_seconds_not_milliseconds():
    # The order struct and /book use milliseconds; this header does not. Wrong
    # unit is accepted at the client and rejected later with a bare 401.
    headers = build_l2_headers(
        address="0x0000000000000000000000000000000000000001",
        api_key="k", secret=SECRET, passphrase="p",
        timestamp=TIMESTAMP, method="GET", request_path="/x",
    )
    assert len(headers["POLY_TIMESTAMP"]) == 10
