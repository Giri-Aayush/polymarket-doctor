"""verify-order against a real EIP-712 signature.

The load-bearing test signs an order with a known key using the same
encode_typed_data path the SDK uses, then asserts the verifier recovers that
key. If our domain, struct, or field encoding drifted from the exchange's, this
round trip breaks — which is the whole point.
"""

from __future__ import annotations

from decimal import Decimal

from eth_account import Account
from eth_account.messages import encode_typed_data

from polymarket_doctor.core.check import Severity
from polymarket_doctor.order_verify import (
    DOMAIN_NAME,
    DOMAIN_VERSION,
    EXCHANGE_V2,
    NEG_RISK_EXCHANGE_V2,
    ORDER_TYPE,
    verify_order,
)

# Deterministic test key — never used for anything real.
KEY = "0x" + "11" * 32
ACCOUNT = Account.from_key(KEY)


def _typed(message: dict, *, neg_risk: bool = False) -> dict:
    return {
        "primaryType": "Order",
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": ORDER_TYPE,
        },
        "domain": {
            "name": DOMAIN_NAME,
            "version": DOMAIN_VERSION,
            "chainId": 137,
            "verifyingContract": NEG_RISK_EXCHANGE_V2 if neg_risk else EXCHANGE_V2,
        },
        "message": message,
    }


def signed_order(*, side=0, maker_amount=25000, taker_amount=5000000,
                 signer=None, neg_risk=False, signature_type=0) -> dict:
    """A BUY of 5 tokens at 0.005, signed by ACCOUNT unless told otherwise."""
    signer = signer or ACCOUNT.address
    message = {
        "salt": 1,
        "maker": ACCOUNT.address,
        "signer": signer,
        "tokenId": 123,
        "makerAmount": maker_amount,
        "takerAmount": taker_amount,
        "side": side,
        "signatureType": signature_type,
        "timestamp": 1786800000000,
        "metadata": b"\x00" * 32,
        "builder": b"\x00" * 32,
    }
    encoded = encode_typed_data(full_message=_typed(message, neg_risk=neg_risk))
    signed = ACCOUNT.sign_message(encoded)
    return {
        "salt": "1", "maker": ACCOUNT.address, "signer": signer, "tokenId": "123",
        "makerAmount": str(maker_amount), "takerAmount": str(taker_amount),
        "side": "BUY" if side == 0 else "SELL", "signatureType": signature_type,
        "timestamp": "1786800000000",
        "metadata": "0x" + "00" * 32, "builder": "0x" + "00" * 32,
        "signature": "0x" + signed.signature.hex(),
    }


def _by_name(results, name):
    return next(r.finding for r in results if r.name == name)


def test_a_correctly_signed_order_recovers_to_its_signer():
    results = verify_order(signed_order(), tick_size=Decimal("0.001"))
    sig = _by_name(results, "signature")
    assert sig.severity is Severity.PASS
    assert not any(r.finding.severity is Severity.FAIL for r in results)


def test_tampered_signature_recovers_to_the_wrong_address():
    order = signed_order()
    # Flip one byte of the amounts after signing: same signature, different order.
    order["makerAmount"] = "26000"
    sig = _by_name(verify_order(order, tick_size=Decimal("0.001")), "signature")
    assert sig.severity is Severity.FAIL
    assert "wrong address" in sig.summary


def test_neg_risk_order_verified_against_standard_contract_fails():
    # Signed for neg-risk, checked as standard → recovers to a different address.
    order = signed_order(neg_risk=True)
    sig = _by_name(verify_order(order, neg_risk=False, tick_size=Decimal("0.001")), "signature")
    assert sig.severity is Severity.FAIL
    # And the right contract makes it pass.
    ok = _by_name(verify_order(order, neg_risk=True, tick_size=Decimal("0.001")), "signature")
    assert ok.severity is Severity.PASS


def test_off_grid_price_is_rejected():
    # 25001/5000000 = 0.0050002, not on the 0.001 grid.
    order = signed_order(maker_amount=25001)
    price = _by_name(verify_order(order, tick_size=Decimal("0.001")), "price")
    assert price.severity is Severity.FAIL
    assert "tick grid" in price.summary


def test_sub_minimum_size_is_rejected():
    # 4 tokens: taker 4_000_000.
    order = signed_order(maker_amount=20000, taker_amount=4000000)
    size = _by_name(verify_order(order, tick_size=Decimal("0.001")), "size")
    assert size.severity is Severity.FAIL
    assert "minimum" in size.summary


def test_fractional_base_unit_is_the_float_bug():
    order = signed_order()
    order["takerAmount"] = "5000000.5"
    amounts = _by_name(verify_order(order, tick_size=Decimal("0.001")), "amounts")
    assert amounts.severity is Severity.FAIL


def test_eoa_order_with_mismatched_maker_and_signer_is_flagged():
    stranger = "0x000000000000000000000000000000000000dEaD"
    order = signed_order(signer=stranger)  # signatureType 0 but signer != maker
    identity = _by_name(verify_order(order, tick_size=Decimal("0.001")), "identity")
    assert identity.severity is Severity.FAIL


def test_poly_1271_is_reported_as_on_chain_only_not_failed():
    order = signed_order(signature_type=3)
    sig = _by_name(verify_order(order, tick_size=Decimal("0.001")), "signature")
    assert sig.severity is Severity.WARN
    assert "1271" in sig.summary or "POLY_1271" in sig.summary


def test_missing_fields_stop_early_with_a_clear_message():
    results = verify_order({"maker": ACCOUNT.address}, tick_size=Decimal("0.001"))
    shape = _by_name(results, "shape")
    assert shape.severity is Severity.FAIL
    assert "missing" in shape.summary


def test_accepts_the_full_post_body_shape_too():
    # POST /order wraps the order under an "order" key; the verifier unwraps it.
    wrapped = {"order": signed_order(), "owner": "x", "orderType": "GTC"}
    results = verify_order(wrapped, tick_size=Decimal("0.001"))
    assert _by_name(results, "signature").severity is Severity.PASS


def test_cli_verify_order_exit_codes(tmp_path, capsys):
    # End-to-end through main(): a good order exits 0, a tampered one exits 1,
    # and neither touches the network (no --token given).
    import json

    from polymarket_doctor.cli import main

    good = tmp_path / "good.json"
    good.write_text(json.dumps(signed_order()))
    assert main(["verify-order", "--file", str(good)]) == 0

    bad = tmp_path / "bad.json"
    order = signed_order()
    order["makerAmount"] = "26000"  # tamper after signing
    bad.write_text(json.dumps(order))
    assert main(["verify-order", "--file", str(bad)]) == 1


def test_cli_verify_order_rejects_unparseable_json(tmp_path):
    from polymarket_doctor.cli import main

    junk = tmp_path / "junk.json"
    junk.write_text("not json")
    assert main(["verify-order", "--file", str(junk)]) == 2


def test_cli_verify_order_json_output(tmp_path, capsys):
    import json

    from polymarket_doctor.cli import main

    good = tmp_path / "good.json"
    good.write_text(json.dumps(signed_order()))
    code = main(["verify-order", "--file", str(good), "--format", "json"])
    doc = json.loads(capsys.readouterr().out)

    assert doc["schema_version"] == "1.0"
    assert doc["command"] == "verify-order"
    assert doc["ok"] is True and doc["exit_code"] == code == 0
    assert any(c["name"] == "signature" and c["severity"] == "pass" for c in doc["checks"])


def test_cli_verify_order_json_flags_a_rejected_order(tmp_path, capsys):
    import json

    from polymarket_doctor.cli import main

    bad = tmp_path / "bad.json"
    order = signed_order()
    order["makerAmount"] = "26000"  # tamper after signing
    bad.write_text(json.dumps(order))
    code = main(["verify-order", "--file", str(bad), "--format", "json"])
    doc = json.loads(capsys.readouterr().out)

    assert doc["ok"] is False and doc["exit_code"] == code == 1
    assert any(c["severity"] == "fail" for c in doc["checks"])


def test_cli_verify_order_token_fetches_real_tick_and_neg_risk(tmp_path, capsys, monkeypatch):
    # --token replaces inference with live /tick-size and /neg-risk. Stub the
    # probe so it's offline but the branch runs.
    import json as _json

    import httpx

    from polymarket_doctor import cli
    from polymarket_doctor.net.http import HttpxProbe

    def handler(request):
        url = str(request.url)
        if "/tick-size" in url:
            return httpx.Response(200, json={"minimum_tick_size": "0.01"})
        if "/neg-risk" in url:
            return httpx.Response(200, json={"neg_risk": True})
        return httpx.Response(404, json={})

    original = HttpxProbe.__init__

    def patched(self, *a, **k):
        original(self, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli.HttpxProbe, "__init__", patched)

    order = tmp_path / "o.json"
    order.write_text(_json.dumps(signed_order(maker_amount=1600, taker_amount=5000000)))
    # 1600/5_000_000 = 0.00032, off the 0.01 grid the token reports -> price fails.
    cli.main(["verify-order", "--file", str(order), "--token", "123", "--format", "json"])
    doc = _json.loads(capsys.readouterr().out)

    assert doc["exchange"] == "neg-risk"  # picked up from the live neg-risk flag
    price = next(c for c in doc["checks"] if c["name"] == "price")
    assert price["severity"] == "fail"  # graded against the fetched 0.01 tick
