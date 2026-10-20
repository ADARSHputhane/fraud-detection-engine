"""Helpers that build event dicts in the exact shape the ingest API expects."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from ..schemas import MerchantCategory as MC
from .personas import Card, new_uuid

MERCHANTS_PER_CATEGORY = 40

# Card-present for physical purchases, in-app for delivery/rides, online for the rest.
_CHANNEL = {
    MC.GROCERY: "card_present",
    MC.FUEL: "card_present",
    MC.RESTAURANTS: "card_present",
    MC.PHARMACY: "card_present",
    MC.FOOD_DELIVERY: "in_app",
    MC.TRANSPORT: "in_app",
}


def channel_for(category: MC) -> str:
    return _CHANNEL.get(category, "online")


def merchant_id(rng: random.Random, category: MC) -> str:
    return f"mer_{category.value}_{rng.randrange(MERCHANTS_PER_CATEGORY):03d}"


def money(value: float) -> str:
    v = max(value, 1.0)
    return str(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def geo(lat: float, lon: float, country: str, city: str) -> dict:
    return {"lat": lat, "lon": lon, "country": country, "city": city}


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def payment(
    rng: random.Random,
    *,
    user_id: str,
    card_id: str,
    device_id: str,
    ip: str,
    ts: datetime,
    amount: float,
    category: MC,
    location: dict,
    channel: str | None = None,
    status: str = "approved",
    decline_reason: str | None = None,
    merchant: str | None = None,
) -> dict:
    event = {
        "event_type": "payment",
        "event_id": new_uuid(rng),
        "user_id": user_id,
        "card_id": card_id,
        "device_id": device_id,
        "ip_address": ip,
        "geo": location,
        "timestamp": iso(ts),
        "amount": money(amount),
        "currency": "INR",
        "merchant_id": merchant or merchant_id(rng, category),
        "merchant_category": category.value,
        "channel": channel or channel_for(category),
        "status": status,
    }
    if decline_reason:
        event["decline_reason"] = decline_reason
    return event


def instrument(
    rng: random.Random,
    *,
    user_id: str,
    card: Card,
    device_id: str,
    ip: str,
    ts: datetime,
    location: dict,
    action: str = "added",
    name_matches_account: bool = True,
) -> dict:
    return {
        "event_type": "instrument",
        "event_id": new_uuid(rng),
        "user_id": user_id,
        "card_id": card.card_id,
        "device_id": device_id,
        "ip_address": ip,
        "geo": location,
        "timestamp": iso(ts),
        "action": action,
        "bin": card.bin,
        "card_type": card.card_type,
        "issuing_country": card.issuing_country,
        "name_matches_account": name_matches_account,
    }
