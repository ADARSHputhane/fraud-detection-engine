"""Synthetic traffic: normal persona behaviour plus injected fraud.

Output is deterministic for a given seed, so a dataset can be regenerated exactly.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from ..schemas import Event
from ..schemas import MerchantCategory as MC
from . import events as ev
from .patterns import PATTERNS, Context
from .personas import (
    CATEGORY_SPEND,
    DOMESTIC_CITIES,
    TRAVEL_CITIES,
    Card,
    Persona,
    datacenter_ip,
    domestic_bin,
    jitter,
    make_persona,
    new_token,
    residential_ip,
)

IST = ZoneInfo("Asia/Kolkata")
_EVENT_ADAPTER: TypeAdapter = TypeAdapter(Event)

# First-party ("friendly") fraud: a genuine purchase the cardholder later disputes. It looks
# exactly like normal traffic, so no model can catch it; it caps achievable recall the way
# it does in real data.
FRIENDLY_FRAUD = "friendly_fraud"
FRIENDLY_FRAUD_RATE = 0.02
_DISPUTABLE = {MC.ELECTRONICS.value, MC.FASHION.value, MC.TRAVEL.value, MC.ENTERTAINMENT.value}


@dataclass
class Dataset:
    events: list[dict]
    labels: list[dict]
    stats: dict = field(default_factory=dict)


def _poisson(rng: random.Random, lam: float) -> int:
    # Knuth's method — fine for the small rates used here.
    threshold, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1


def _local_ts(rng: random.Random, day: datetime, p: Persona) -> datetime:
    start, end = p.active_hours
    if rng.random() < 0.95:
        hour = rng.randrange(start, end)
    else:  # the occasional late-night order is normal, not fraud
        hour = rng.randrange(0, 24)
    local = datetime.combine(day.date(), time(hour % 24), tzinfo=IST)
    return (local + timedelta(minutes=rng.uniform(0, 60))).astimezone(UTC)


def _normal_traffic(
    rng: random.Random, p: Persona, start: datetime, end: datetime, days: int
) -> list[dict]:
    out: list[dict] = []
    # Long-standing cards: their "added" events predate the window.
    for card in p.cards:
        out.append(
            ev.instrument(
                rng,
                user_id=p.user_id,
                card=card,
                device_id=p.devices[0],
                ip=p.home_ip,
                ts=card.added_at,
                location=ev.geo(*jitter(p.lat, p.lon, rng), "IN", p.city),
            )
        )

    cards = list(p.cards)
    devices = list(p.devices)
    categories = list(p.category_weights)
    weights = list(p.category_weights.values())
    trip_until: datetime | None = None
    trip_city = None
    abroad_until: datetime | None = None
    abroad_city = None
    first_day = start.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)

    for d in range(days + 1):
        day = first_day + timedelta(days=d)

        # ~2% of days start a 1-3 day domestic trip: real travel, plausible speeds.
        if trip_until is None and abroad_until is None and rng.random() < 0.02:
            trip_city = rng.choice([c for c in DOMESTIC_CITIES if c[0] != p.city])
            trip_until = day + timedelta(days=rng.randint(1, 3))
        if trip_until is not None and day >= trip_until:
            trip_until, trip_city = None, None

        # A holiday abroad now and then. The flight days have no payments, so the first
        # payment abroad is a plausible distance in time from the last one at home.
        if abroad_until is None and trip_until is None and rng.random() < 0.0025:
            abroad_city = rng.choice(TRAVEL_CITIES)
            abroad_until = day + timedelta(days=rng.randint(4, 8))
            continue  # outbound flight
        if abroad_until is not None and day >= abroad_until:
            abroad_until, abroad_city = None, None
            continue  # flight home

        # People replace their phones: a new device becomes the main one from today.
        if rng.random() < 0.004:
            devices.insert(0, new_token(rng, "dev_", 12))

        # A genuine new card now and then, used normally afterwards. New card != fraud.
        if rng.random() < 0.003:
            ts = _local_ts(rng, day, p)
            if start <= ts < end:
                card = Card(
                    card_id=new_token(rng, "tok_"),
                    bin=domestic_bin(rng),
                    card_type=rng.choice(["debit", "credit"]),
                    issuing_country="IN",
                    added_at=ts,
                )
                cards.append(card)
                out.append(
                    ev.instrument(
                        rng,
                        user_id=p.user_id,
                        card=card,
                        device_id=devices[0],
                        ip=p.home_ip,
                        ts=ts,
                        location=ev.geo(*jitter(p.lat, p.lon, rng), "IN", p.city),
                        # A spouse's or parent's card: the name legitimately differs.
                        name_matches_account=rng.random() >= 0.15,
                    )
                )

        for _ in range(_poisson(rng, p.txns_per_day)):
            ts = _local_ts(rng, day, p)
            if not (start <= ts < end):
                continue
            usable = [c for c in cards if c.added_at <= ts]
            if not usable:
                continue
            card = rng.choices(usable, weights=[3] + [1] * (len(usable) - 1))[0]
            category = rng.choices(categories, weights=weights)[0]
            median, sigma = CATEGORY_SPEND[category]
            amount = rng.lognormvariate(0, sigma) * median * p.spend_multiplier

            country = "IN"
            if abroad_city is not None:
                city, country, lat, lon = abroad_city
                ip = residential_ip(rng)  # hotel / local SIM
            elif trip_city is not None:
                city, lat, lon = trip_city
                ip = p.mobile_ip
            else:
                city, lat, lon = p.city, p.lat, p.lon
                ip = p.home_ip if rng.random() < 0.6 else p.mobile_ip
            if ev.channel_for(category) != "card_present" and rng.random() < p.vpn_share:
                ip = datacenter_ip(rng)  # privacy-conscious users pay through a VPN
            location = ev.geo(*jitter(lat, lon, rng), country, city)

            if rng.random() < 0.985 or len(devices) == 1:
                device = devices[0]
            else:
                device = devices[1]

            common = dict(
                user_id=p.user_id,
                card_id=card.card_id,
                device_id=device,
                ip=ip,
                amount=amount,
                category=category,
                location=location,
                merchant=ev.merchant_id(rng, category),
            )

            roll = rng.random()
            if roll < 0.01:
                # Mistyped CVV, then a successful retry — must not look like brute force.
                out.append(
                    ev.payment(
                        rng,
                        ts=ts - timedelta(seconds=rng.uniform(20, 90)),
                        status="declined",
                        decline_reason="incorrect_cvv",
                        **common,
                    )
                )
                out.append(ev.payment(rng, ts=ts, **common))
            elif roll < 0.006:
                # Insufficient funds, retried a couple of times, then a smaller amount works.
                t = ts - timedelta(minutes=rng.uniform(3, 10))
                for _ in range(rng.randint(2, 3)):
                    out.append(
                        ev.payment(
                            rng,
                            ts=t,
                            status="declined",
                            decline_reason="insufficient_funds",
                            **common,
                        )
                    )
                    t += timedelta(seconds=rng.uniform(30, 120))
                out.append(
                    ev.payment(rng, ts=ts, **{**common, "amount": amount * rng.uniform(0.4, 0.8)})
                )
            elif roll < 0.03:
                out.append(
                    ev.payment(
                        rng,
                        ts=ts,
                        status="declined",
                        decline_reason=rng.choice(["insufficient_funds", "expired_card"]),
                        **common,
                    )
                )
            else:
                out.append(ev.payment(rng, ts=ts, **common))
    return out


def generate_dataset(
    users: int = 500,
    days: int = 30,
    fraud_rate: float = 0.05,
    seed: int = 42,
    end: datetime | None = None,
) -> Dataset:
    """Build a labelled, time-ordered dataset of payment and instrument events."""
    if not 0 <= fraud_rate < 1:
        raise ValueError("fraud_rate must be in [0, 1)")
    rng = random.Random(seed)
    # Finish slightly in the past so every timestamp passes the API's future-time check.
    end = (end or datetime.now(UTC) - timedelta(minutes=10)).astimezone(UTC)
    start = end - timedelta(days=days)

    personas = [make_persona(rng, start) for _ in range(users)]
    labelled: list[tuple[dict, bool, str | None, str | None]] = []
    attacks: Counter[str] = Counter()
    for p in personas:
        for e in _normal_traffic(rng, p, start, end, days):
            disputed = (
                e["event_type"] == "payment"
                and e["status"] == "approved"
                and e["merchant_category"] in _DISPUTABLE
                and rng.random() < FRIENDLY_FRAUD_RATE
            )
            if disputed:
                attacks[FRIENDLY_FRAUD] += 1
                labelled.append((e, True, FRIENDLY_FRAUD, new_token(rng, "atk_", 10)))
            else:
                labelled.append((e, False, None, None))

    ctx = Context(rng=rng, personas=personas, window_start=start, window_end=end)
    names = list(PATTERNS)
    pattern_weights = [PATTERNS[n][1] for n in names]
    target = round(len(labelled) * fraud_rate / (1 - fraud_rate))
    fraud_count = attacks[FRIENDLY_FRAUD]
    while fraud_count < target:
        name = rng.choices(names, weights=pattern_weights)[0]
        attack_id = new_token(rng, "atk_", 10)
        attacks[name] += 1
        for event, is_fraud in PATTERNS[name][0](ctx):
            labelled.append((event, is_fraud, name if is_fraud else None, attack_id))
            fraud_count += is_fraud

    labelled.sort(key=lambda row: datetime.fromisoformat(row[0]["timestamp"]))

    events, labels = [], []
    for event, is_fraud, pattern, attack_id in labelled:
        _EVENT_ADAPTER.validate_python(event)  # fail loudly here, not at replay time
        events.append(event)
        labels.append(
            {
                "event_id": event["event_id"],
                "is_fraud": is_fraud,
                "pattern": pattern,
                "attack_id": attack_id if is_fraud else None,
            }
        )

    by_pattern = Counter(lbl["pattern"] for lbl in labels if lbl["is_fraud"])
    stats = {
        "users": users,
        "days": days,
        "seed": seed,
        "window": [start.isoformat(), end.isoformat()],
        "events": len(events),
        "payments": sum(e["event_type"] == "payment" for e in events),
        "instrument_events": sum(e["event_type"] == "instrument" for e in events),
        "fraud_events": sum(lbl["is_fraud"] for lbl in labels),
        "fraud_rate": round(sum(lbl["is_fraud"] for lbl in labels) / max(len(labels), 1), 4),
        "attacks": dict(attacks),
        "fraud_events_by_pattern": dict(by_pattern),
        "high_risk_category_share_in_normal_payments": _high_risk_share(events, labels),
    }
    return Dataset(events=events, labels=labels, stats=stats)


def _high_risk_share(events: list[dict], labels: list[dict]) -> float:
    high = {MC.GIFT_CARDS.value, MC.CRYPTO.value, MC.GAMBLING.value, MC.MONEY_TRANSFER.value}
    normal = [
        e
        for e, lbl in zip(events, labels, strict=True)
        if e["event_type"] == "payment" and not lbl["is_fraud"]
    ]
    return round(sum(e["merchant_category"] in high for e in normal) / max(len(normal), 1), 4)
