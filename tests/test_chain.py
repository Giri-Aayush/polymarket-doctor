"""The on-chain ABI decoders and funder classification.

The identity tests inject pre-built ContractProfiles, so the real hex decoding
never ran. A wrong offset here would silently misclassify a Gnosis Safe as a
plain wallet and hand out the wrong signature type, which the module's docstring
calls the single biggest source of rejected orders. These feed canned eth_call
return data through a stub and check the decode.
"""

from __future__ import annotations

from conftest import StubProbe, http_error, ok

from polymarket_doctor.net.chain import (
    SELECTOR_GET_OWNERS,
    SELECTOR_VERSION,
    ChainReader,
    ContractProfile,
    _decode_address_array,
    _decode_string,
)


class MethodProbe:
    """Routes an eth JSON-RPC call by method (and eth_call by selector).

    ChainReader posts every call to the same RPC URL, so StubProbe's URL
    routing can't tell eth_getCode from the two eth_calls apart. This inspects
    the posted body instead, letting a full profile() run be driven offline.
    """

    def __init__(self, *, code: str, version_word: str, owners_word: str) -> None:
        self._code = code
        self._version_word = version_word
        self._owners_word = owners_word

    def post(self, url, *, headers=None, json_body=None):
        method = json_body["method"]
        if method == "eth_getCode":
            return ok({"result": self._code})
        data = json_body["params"][0]["data"]
        if data == SELECTOR_VERSION:
            return ok({"result": self._version_word})
        if data == SELECTOR_GET_OWNERS:
            return ok({"result": self._owners_word})
        raise AssertionError(f"unexpected eth_call selector {data}")

    def get(self, url, *, headers=None, params=None):
        raise AssertionError("ChainReader should only POST")


def _abi_string(text: str) -> str:
    """ABI-encode a string the way eth_call returns it: offset, length, data."""
    body = text.encode()
    offset = (32).to_bytes(32, "big")
    length = len(body).to_bytes(32, "big")
    padded = body + b"\x00" * ((32 - len(body) % 32) % 32)
    return "0x" + (offset + length + padded).hex()


def _abi_address_array(addresses: list[str]) -> str:
    words = [(32).to_bytes(32, "big"), len(addresses).to_bytes(32, "big")]
    for addr in addresses:
        words.append(bytes(12) + bytes.fromhex(addr[2:]))
    return "0x" + b"".join(words).hex()


class TestDecoders:
    def test_reads_a_safe_version_string(self):
        assert _decode_string(_abi_string("1.3.0")) == "1.3.0"

    def test_a_revert_or_empty_word_decodes_to_none(self):
        assert _decode_string(None) is None
        assert _decode_string("0x") is None
        assert _decode_string("0x1234") is None  # too short to hold a string

    def test_reads_an_owner_array(self):
        owners = ["0x" + "ab" * 20, "0x" + "cd" * 20]
        decoded = _decode_address_array(_abi_address_array(owners))
        assert [d.lower() for d in decoded] == [o.lower() for o in owners]

    def test_empty_or_short_owner_data_decodes_to_empty(self):
        assert _decode_address_array(None) == ()
        assert _decode_address_array("0x") == ()

    def test_malformed_string_content_decodes_to_none(self):
        # Full-length word but the data is invalid UTF-8 (0xff), so the decode
        # raises and we get None rather than a crash.
        offset = "00" * 31 + "20"
        length = "00" * 31 + "01"
        bad = offset + length + "ff" + "00" * 31
        assert _decode_string("0x" + bad) is None

    def test_non_hex_owner_count_decodes_to_empty(self):
        # The count word isn't hex, so int(..., 16) raises and we get ().
        offset = "00" * 31 + "20"
        count = "zz" * 32
        assert _decode_address_array("0x" + offset + count) == ()

    def test_truncated_owner_array_stops_at_the_short_word(self):
        # Count claims two owners but only one address of data follows; the
        # loop breaks on the short chunk instead of reading past the buffer.
        offset = "00" * 31 + "20"
        count = "00" * 31 + "02"
        one_owner = "00" * 12 + "ab" * 20
        decoded = _decode_address_array("0x" + offset + count + one_owner)
        assert len(decoded) == 1


class TestContractProfile:
    def test_is_owner_is_case_insensitive(self):
        profile = ContractProfile(has_code=True, safe_version="1.3.0",
                                  owners=("0x" + "AB" * 20,))
        assert profile.is_owner("0x" + "ab" * 20)
        assert not profile.is_owner("0x" + "cd" * 20)


class TestProfile:
    def test_no_code_is_an_eoa_not_a_contract(self, make_context):
        probe = StubProbe({"": ok({"result": "0x"})})
        reader = ChainReader(probe, "https://rpc.test")
        profile = reader.profile("0x" + "1" * 40)
        assert profile is not None
        assert profile.has_code is False
        assert profile.is_gnosis_safe is False

    def test_unusable_rpc_returns_none_not_a_false_eoa(self, make_context):
        # Distinguishing "RPC down" from "EOA" is a documented invariant.
        probe = StubProbe({"": ok({"error": {"code": -1, "message": "down"}})})
        reader = ChainReader(probe, "https://rpc.test")
        assert reader.profile("0x" + "1" * 40) is None

    def test_http_error_from_the_rpc_returns_none(self, make_context):
        # A 500/non-2xx (not a JSON-RPC error body) is also "unusable", not EOA.
        probe = StubProbe({"": http_error(500)})
        reader = ChainReader(probe, "https://rpc.test")
        assert reader.profile("0x" + "1" * 40) is None

    def test_classifies_a_deployed_safe_end_to_end(self, make_context):
        # The real path: eth_getCode returns code, then VERSION() and
        # getOwners() are decoded from live-shaped hex. Exercises profile's
        # contract branch and _eth_call, which the injected-profile identity
        # tests skip.
        eoa = "0x" + "ab" * 20
        probe = MethodProbe(
            code="0x60806040",
            version_word=_abi_string("1.3.0"),
            owners_word=_abi_address_array([eoa]),
        )
        profile = ChainReader(probe, "https://rpc.test").profile("0x" + "3" * 40)

        assert profile is not None
        assert profile.has_code is True
        assert profile.is_gnosis_safe is True
        assert profile.safe_version == "1.3.0"
        assert profile.is_owner(eoa)

    def test_classifies_a_beacon_proxy_deposit_wallet(self, make_context):
        # Has code but doesn't implement the Safe interface: VERSION/getOwners
        # revert, so safe_version is None and it reads as a deposit wallet.
        probe = MethodProbe(code="0x60806040", version_word="0x", owners_word="0x")
        profile = ChainReader(probe, "https://rpc.test").profile("0x" + "4" * 40)

        assert profile is not None
        assert profile.has_code is True
        assert profile.is_gnosis_safe is False
        assert profile.owners == ()
