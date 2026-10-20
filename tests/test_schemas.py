from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fraud_engine.schemas import InstrumentEvent, PaymentEvent, contains_pan, luhn_valid

# Standard public test card numbers — not real accounts.
VISA_TEST_PAN = "4111111111111111"


def test_luhn():
    assert luhn_valid(VISA_TEST_PAN)
    assert luhn_valid("5555555555554444")
    assert not luhn_valid("4111111111111112")


@pytest.mark.parametrize(
    "value",
    [VISA_TEST_PAN, "4111 1111 1111 1111", "4111-1111-1111-1111", f"tok_{VISA_TEST_PAN}"],
)
def test_contains_pan_detects_card_numbers(value):
    assert contains_pan(value)


@pytest.mark.parametrize("value", ["tok_a1b2c3d4e5f6", "tok_4111111111111112", "123456"])
def test_contains_pan_ignores_non_pans(value):
    assert not contains_pan(value)


def test_valid_payment(payment):
    ev = PaymentEvent.model_validate(payment())
    assert ev.event_type == "payment"
    assert ev.amount == Decimal("1499.00")


def test_float_amount_becomes_exact_decimal(payment):
    ev = PaymentEvent.model_validate(payment(amount=1499.99))
    assert ev.amount == Decimal("1499.99")


def test_valid_instrument(instrument):
    ev = InstrumentEvent.model_validate(instrument())
    assert ev.action == "added"


def _error_text(exc: ValidationError) -> str:
    return " | ".join(f"{e['loc']}: {e['msg']}" for e in exc.errors())


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"card_id": VISA_TEST_PAN}, "raw card number"),
        ({"card_id": "4111 1111 1111 1111"}, "raw card number"),
        ({"card_id": f"tok_{VISA_TEST_PAN}"}, "raw card number"),
        ({"card_id": "card_123"}, "pattern"),
        ({"amount": "-10"}, "greater than 0"),
        ({"amount": "0"}, "greater than 0"),
        ({"amount": "10.999"}, "decimal places"),
        ({"currency": "inr"}, "pattern"),
        ({"ip_address": "999.1.1.1"}, "IP"),
        ({"merchant_category": "weapons"}, "merchant_category"),
        ({"timestamp": "2026-01-01T10:00:00"}, "timezone"),
        ({"is_fraud": True}, "Extra inputs"),
        ({"decline_reason": "incorrect_cvv"}, "only allowed when status"),
        ({"geo": {"lat": 120, "lon": 0, "country": "IN"}}, "lat"),
    ],
)
def test_payment_rejections(payment, overrides, expected):
    with pytest.raises(ValidationError) as exc:
        PaymentEvent.model_validate(payment(**overrides))
    assert expected in _error_text(exc.value)


def test_future_timestamp_rejected(payment):
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    with pytest.raises(ValidationError, match="in the future"):
        PaymentEvent.model_validate(payment(timestamp=future))


def test_small_clock_skew_allowed(payment):
    slightly_ahead = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
    PaymentEvent.model_validate(payment(timestamp=slightly_ahead))


def test_declined_with_reason_ok(payment):
    ev = PaymentEvent.model_validate(payment(status="declined", decline_reason="incorrect_cvv"))
    assert ev.decline_reason == "incorrect_cvv"


@pytest.mark.parametrize(
    "overrides",
    [{"bin": "12345"}, {"bin": "45abcd"}, {"issuing_country": "IND"}, {"action": "stolen"}],
)
def test_instrument_rejections(instrument, overrides):
    with pytest.raises(ValidationError):
        InstrumentEvent.model_validate(instrument(**overrides))
