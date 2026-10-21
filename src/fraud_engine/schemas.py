"""Event contracts.

Every downstream stage (feature engine, rules, ML, dashboard) reads these shapes, so they are
strict on purpose: unknown fields are rejected, timestamps must carry a timezone, and nothing
that looks like a real card number is ever accepted.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    IPvAnyAddress,
    field_validator,
)

# Clients' clocks drift; allow a little skew before calling a timestamp "in the future".
MAX_CLOCK_SKEW = timedelta(minutes=5)


class MerchantCategory(StrEnum):
    GROCERY = "grocery"
    FOOD_DELIVERY = "food_delivery"
    RESTAURANTS = "restaurants"
    FUEL = "fuel"
    TRANSPORT = "transport"
    UTILITIES = "utilities"
    PHARMACY = "pharmacy"
    FASHION = "fashion"
    ELECTRONICS = "electronics"
    ENTERTAINMENT = "entertainment"
    TRAVEL = "travel"
    # Categories that fraudsters favour because the value is easy to cash out.
    GIFT_CARDS = "gift_cards"
    CRYPTO = "crypto"
    GAMBLING = "gambling"
    MONEY_TRANSFER = "money_transfer"


HIGH_RISK_CATEGORIES = frozenset(
    {
        MerchantCategory.GIFT_CARDS,
        MerchantCategory.CRYPTO,
        MerchantCategory.GAMBLING,
        MerchantCategory.MONEY_TRANSFER,
    }
)


def luhn_valid(digits: str) -> bool:
    """Return True if a digit string passes the Luhn checksum used by card numbers."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_DIGIT_RUN = re.compile(r"\d{13,19}")


def contains_pan(value: str) -> bool:
    """Detect a primary account number (real card number) hidden anywhere in a string."""
    compact = re.sub(r"[\s-]", "", value)
    return any(luhn_valid(run) for run in _DIGIT_RUN.findall(compact))


def _reject_pan(value: object) -> object:
    if isinstance(value, str) and contains_pan(value):
        raise ValueError(
            "looks like a raw card number; send a token (tok_...) instead — "
            "this service never stores card numbers"
        )
    return value


def _not_in_future(ts: datetime) -> datetime:
    if ts > datetime.now(UTC) + MAX_CLOCK_SKEW:
        raise ValueError("timestamp is in the future")
    return ts


EntityId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_\-]{1,64}$")]
CardToken = Annotated[
    str,
    BeforeValidator(_reject_pan),
    Field(pattern=r"^tok_[A-Za-z0-9]{8,64}$", examples=["tok_8fj2k3m9q0zx"]),
]
CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$", examples=["IN"])]
EventTime = Annotated[AwareDatetime, AfterValidator(_not_in_future)]


class Geo(BaseModel):
    """Where the event happened, as resolved upstream from device GPS or IP geolocation."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    country: CountryCode
    city: str | None = Field(default=None, max_length=100)


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: UUID = Field(default_factory=uuid4)
    user_id: EntityId
    card_id: CardToken
    device_id: EntityId
    ip_address: IPvAnyAddress
    geo: Geo | None = None
    timestamp: EventTime


class PaymentEvent(_EventBase):
    """A payment attempt — approved or declined."""

    event_type: Literal["payment"] = "payment"
    amount: Decimal = Field(gt=0, le=10_000_000, decimal_places=2, examples=["1499.00"])
    currency: str = Field(pattern=r"^[A-Z]{3}$", examples=["INR"])
    merchant_id: EntityId
    merchant_category: MerchantCategory
    channel: Literal["online", "in_app", "card_present"] = "online"
    status: Literal["approved", "declined"] = "approved"
    decline_reason: (
        Literal["insufficient_funds", "incorrect_cvv", "expired_card", "do_not_honor"] | None
    ) = None

    @field_validator("decline_reason")
    @classmethod
    def _reason_only_when_declined(cls, v: str | None, info) -> str | None:
        if v is not None and info.data.get("status") != "declined":
            raise ValueError("decline_reason is only allowed when status is 'declined'")
        return v


class InstrumentEvent(_EventBase):
    """A payment instrument (card) being added to, updated on, or removed from an account."""

    event_type: Literal["instrument"] = "instrument"
    action: Literal["added", "updated", "removed"]
    bin: str = Field(
        pattern=r"^\d{6,8}$",
        description="Bank Identification Number: first 6-8 digits, identifies the issuer",
        examples=["457173"],
    )
    card_type: Literal["credit", "debit", "prepaid"]
    issuing_country: CountryCode
    name_matches_account: bool | None = Field(
        default=None, description="Does the cardholder name match the account holder?"
    )


Event = Annotated[PaymentEvent | InstrumentEvent, Field(discriminator="event_type")]


class EventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[Event] = Field(min_length=1, max_length=1_000)


class EventAccepted(BaseModel):
    event_id: UUID
    stream_id: str
    status: Literal["accepted"] = "accepted"


class BatchAccepted(BaseModel):
    accepted: int
    duplicates: list[UUID]


class StoredEvent(BaseModel):
    event_id: UUID
    event_type: str
    received_at: datetime
    payload: dict


class StoredFeatures(BaseModel):
    event_id: UUID
    feature_version: int
    computed_at: datetime
    features: dict[str, Any]
