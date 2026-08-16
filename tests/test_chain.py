"""The on-chain ABI decoders and funder classification.

The identity tests inject pre-built ContractProfiles, so the real hex decoding
never ran. A wrong offset here would silently misclassify a Gnosis Safe as a
plain wallet and hand out the wrong signature type, which the module's docstring
calls the single biggest source of rejected orders. These feed canned eth_call
return data through a stub and check the decode.
"""

from __future__ import annotations

from conftest import StubProbe, ok

from polymarket_doctor.net.chain import (
    ChainReader,
    _decode_address_array,
    _decode_string,
)


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
