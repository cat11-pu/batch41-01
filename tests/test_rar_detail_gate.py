import copy

import pytest

import rar_detail_gate as rdg


def payment(**overrides):
    data = {
        "type": "payment",
        "amount": "100.00",
        "currency": "USD",
        "creditor": {"account": "acct-1"},
    }
    data.update(overrides)
    return data


def make_gate(**kwargs):
    return rdg.AuthorizationDetailGate(
        allowed_currencies=("CNY", "USD", "EUR"),
        allowed_creditors=("acct-1", "acct-2"),
        **kwargs,
    )


def test_multiple_amount_fields_use_strictest_value():
    gate = make_gate()
    canonical = gate.normalize([payment(amount="100.00", max_amount=50)])
    assert canonical[0]["amount"] == "50.00"


def test_string_and_numeric_amounts_compare_as_decimals():
    gate = make_gate()
    text = gate.normalize([payment(amount="100.00")])
    number = gate.normalize([payment(amount=100)])
    assert text == number
    assert text[0]["amount"] == "100.00"


def test_missing_currency_is_rejected():
    gate = make_gate()
    detail = payment()
    del detail["currency"]
    with pytest.raises(rdg.DetailError) as caught:
        gate.validate([detail], scopes=("pay",))
    assert caught.value.reason == "currency_missing"


def test_unauthorized_creditor_inside_list_is_rejected():
    gate = make_gate()
    detail = payment(creditor=[{"account": "acct-1"}, {"account": "acct-bad"}])
    with pytest.raises(rdg.DetailError) as caught:
        gate.validate([detail], scopes=("pay",))
    assert caught.value.reason == "creditor_not_allowed"


def test_duplicate_details_are_deduped_before_totalling():
    gate = make_gate()
    result = gate.validate([payment(), payment()], scopes=("pay",))
    assert len(result["details"]) == 1
    assert result["total_amount"] == "100.00"


def test_scope_conflict_is_rejected():
    gate = make_gate()
    with pytest.raises(rdg.DetailError) as caught:
        gate.validate([payment()], scopes=())
    assert caught.value.reason == "scope_missing"


def test_expired_detail_is_rejected():
    gate = make_gate()
    with pytest.raises(rdg.DetailError) as caught:
        gate.validate([payment(expires_at=50)], scopes=("pay",), now=50)
    assert caught.value.reason == "detail_expired"


def test_too_many_items_is_rejected_not_truncated():
    gate = make_gate()
    details = [payment(amount=str(i + 1)) for i in range(101)]
    with pytest.raises(rdg.DetailError) as caught:
        gate.validate(details, scopes=("pay",))
    assert caught.value.reason == "too_many_items"


def test_unknown_field_is_rejected():
    gate = make_gate()
    with pytest.raises(rdg.DetailError) as caught:
        gate.validate([payment(note="extra")], scopes=("pay",))
    assert caught.value.reason == "unknown_field"


def test_unknown_type_is_rejected():
    gate = make_gate()
    with pytest.raises(rdg.DetailError) as caught:
        gate.validate([{"type": "wire", "amount": "10", "currency": "USD"}], scopes=("pay",))
    assert caught.value.reason == "unknown_type"


def test_token_echo_uses_canonical_details():
    gate = make_gate()
    requested = [payment(amount="100.00", max_amount=50)]
    token = gate.issue_token(requested, scopes=("pay",))
    assert token["details"][0]["amount"] == "50.00"
    assert gate.echo_matches(requested, token["details"]) is True
    tampered = copy.deepcopy(token["details"])
    tampered[0]["amount"] = "60.00"
    assert gate.echo_matches(requested, tampered) is False


def test_negative_amount_is_rejected():
    gate = make_gate()
    with pytest.raises(rdg.DetailError) as caught:
        gate.validate([payment(amount="-5")], scopes=("pay",))
    assert caught.value.reason == "invalid_amount"


def test_nested_metadata_depth_is_limited():
    gate = make_gate(max_depth=3)
    detail = payment(metadata={"l1": {"l2": {"l3": {"l4": "deep"}}}})
    with pytest.raises(rdg.DetailError) as caught:
        gate.validate([detail], scopes=("pay",))
    assert caught.value.reason == "too_deep"


def test_empty_details_are_rejected():
    gate = make_gate()
    with pytest.raises(rdg.DetailError) as caught:
        gate.validate([], scopes=("pay",))
    assert caught.value.reason == "empty_details"


def test_gate_state_is_isolated_between_instances():
    gate_a = rdg.AuthorizationDetailGate(
        allowed_currencies=("USD",), allowed_creditors=("acct-1",)
    )
    gate_b = rdg.AuthorizationDetailGate(
        allowed_currencies=("CNY",), allowed_creditors=("acct-2",)
    )
    result = gate_a.validate([payment()], scopes=("pay",))
    assert result["details"][0]["amount"] == "100.00"
    result["details"][0]["amount"] = "999.00"
    assert gate_a.normalize([payment()])[0]["amount"] == "100.00"
    with pytest.raises(rdg.DetailError) as caught:
        gate_b.validate([payment()], scopes=("pay",))
    assert caught.value.reason == "currency_not_allowed"
