"""Injected fraud patterns.

Each pattern models a real attack and returns `(event, is_fraud)` pairs. Some patterns also
emit a genuine event (e.g. the cardholder's real purchase right before a cloned card is used
abroad) — those are labelled legitimate, because the engine has to tell them apart.
"""

from __future__ import annotations

import random
import string
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..schemas import MerchantCategory as MC
from . import events as ev
from .personas import (
    DOMESTIC_CITIES,
    FOREIGN_CITIES,
    Card,
    Persona,
    datacenter_ip,
    jitter,
    new_token,
    residential_ip,
)

Labelled = list[tuple[dict, bool]]


@dataclass
class Context:
    rng: random.Random
    personas: list[Persona]
    window_start: datetime
    window_end: datetime

    def attack_time(self, room: timedelta = timedelta(hours=6)) -> datetime:
        """A start time that leaves `room` for the attack to finish inside the window."""
        span = (self.window_end - room - self.window_start).total_seconds()
        return self.window_start + timedelta(seconds=self.rng.uniform(0, span))

    def victim(self) -> Persona:
        return self.rng.choice(self.personas)


def typical_spend(p: Persona) -> float:
    return 900 * p.spend_multiplier


def _foreign_location(rng: random.Random) -> dict:
    city, country, lat, lon = rng.choice(FOREIGN_CITIES)
    lat, lon = jitter(lat, lon, rng)
    return ev.geo(lat, lon, country, city)


def _home_location(p: Persona, rng: random.Random) -> dict:
    lat, lon = jitter(p.lat, p.lon, rng)
    return ev.geo(lat, lon, "IN", p.city)


def _stolen_card(rng: random.Random, ts: datetime) -> Card:
    country = rng.choice(["IN", "IN", "US", "GB", "BR", "NG"])
    return Card(
        card_id=new_token(rng, "tok_"),
        bin="".join(rng.choices(string.digits, k=6)),
        card_type=rng.choice(["prepaid", "credit", "credit", "debit"]),
        issuing_country=country,
        added_at=ts,
    )


def card_testing(ctx: Context) -> Labelled:
    """A throwaway account adds a batch of stolen cards and probes each with tiny charges to
    find the live ones, then cashes out the ones that work."""
    rng = ctx.rng
    user_id = new_token(rng, "usr_", 12)
    device = new_token(rng, "dev_", 12)
    ip = datacenter_ip(rng)
    if rng.random() < 0.6:
        location = _foreign_location(rng)
    else:  # fraud farms also route through domestic proxies
        city, lat, lon = rng.choice(DOMESTIC_CITIES)
        location = ev.geo(*jitter(lat, lon, rng), "IN", city)
    t = ctx.attack_time()
    out: Labelled = []
    for _ in range(rng.randint(3, 6)):
        t += timedelta(minutes=rng.uniform(4, 40))
        card = _stolen_card(rng, t)
        out.append(
            (
                ev.instrument(
                    rng,
                    user_id=user_id,
                    card=card,
                    device_id=device,
                    ip=ip,
                    ts=t,
                    location=location,
                    name_matches_account=False,
                ),
                True,
            )
        )
        live = False
        probe_t = t
        for _ in range(rng.randint(1, 4)):
            probe_t += timedelta(seconds=rng.uniform(20, 300))
            declined = rng.random() < 0.6
            live = live or not declined
            out.append(
                (
                    ev.payment(
                        rng,
                        user_id=user_id,
                        card_id=card.card_id,
                        device_id=device,
                        ip=ip,
                        ts=probe_t,
                        amount=rng.uniform(1, 99),
                        category=rng.choice([MC.ENTERTAINMENT, MC.GIFT_CARDS]),
                        location=location,
                        channel="online",
                        status="declined" if declined else "approved",
                        decline_reason=(
                            rng.choice(["do_not_honor", "incorrect_cvv", "expired_card"])
                            if declined
                            else None
                        ),
                    ),
                    True,
                )
            )
        if live and rng.random() < 0.7:
            out.append(
                (
                    ev.payment(
                        rng,
                        user_id=user_id,
                        card_id=card.card_id,
                        device_id=device,
                        ip=ip,
                        ts=probe_t + timedelta(minutes=rng.uniform(1, 15)),
                        amount=rng.uniform(5_000, 20_000),
                        category=MC.GIFT_CARDS,
                        location=location,
                        channel="online",
                    ),
                    True,
                )
            )
    return out


def cvv_brute_force(ctx: Context) -> Labelled:
    """Card number and expiry are known; the attacker cycles CVVs until one is accepted."""
    rng = ctx.rng
    victim = ctx.victim()
    card = rng.choice(victim.cards)
    device = new_token(rng, "dev_", 12)
    ip = datacenter_ip(rng) if rng.random() < 0.7 else residential_ip(rng)
    location = _foreign_location(rng)
    amount = rng.uniform(15_000, 60_000)
    merchant = ev.merchant_id(rng, MC.ELECTRONICS)
    t = ctx.attack_time()
    out: Labelled = []
    for _ in range(rng.randint(5, 10)):
        t += timedelta(seconds=rng.uniform(5, 60))
        out.append(
            (
                ev.payment(
                    rng,
                    user_id=victim.user_id,
                    card_id=card.card_id,
                    device_id=device,
                    ip=ip,
                    ts=t,
                    amount=amount,
                    category=MC.ELECTRONICS,
                    location=location,
                    channel="online",
                    status="declined",
                    decline_reason="incorrect_cvv",
                    merchant=merchant,
                ),
                True,
            )
        )
    t += timedelta(seconds=rng.uniform(5, 60))
    out.append(
        (
            ev.payment(
                rng,
                user_id=victim.user_id,
                card_id=card.card_id,
                device_id=device,
                ip=ip,
                ts=t,
                amount=amount,
                category=MC.ELECTRONICS,
                location=location,
                channel="online",
                merchant=merchant,
            ),
            True,
        )
    )
    return out


def impossible_travel(ctx: Context) -> Labelled:
    """The cardholder pays at home; minutes later a cloned copy of the card is swiped abroad."""
    rng = ctx.rng
    victim = ctx.victim()
    card = rng.choice(victim.cards)
    t = ctx.attack_time()
    category = rng.choice([MC.GROCERY, MC.FOOD_DELIVERY, MC.FUEL, MC.RESTAURANTS])
    median, sigma = (typical_spend(victim) * 0.8, 0.4)
    out: Labelled = [
        (
            ev.payment(
                rng,
                user_id=victim.user_id,
                card_id=card.card_id,
                device_id=victim.devices[0],
                ip=victim.home_ip,
                ts=t,
                amount=rng.lognormvariate(0, sigma) * median,
                category=category,
                location=_home_location(victim, rng),
            ),
            False,  # the genuine purchase
        )
    ]
    location = _foreign_location(rng)
    terminal = new_token(rng, "dev_", 12)
    t += timedelta(minutes=rng.uniform(10, 90))
    for _ in range(rng.randint(1, 3)):
        out.append(
            (
                ev.payment(
                    rng,
                    user_id=victim.user_id,
                    card_id=card.card_id,
                    device_id=terminal,
                    ip=residential_ip(rng),
                    ts=t,
                    amount=typical_spend(victim) * rng.uniform(3, 15),
                    category=rng.choice([MC.ELECTRONICS, MC.FASHION, MC.TRAVEL]),
                    location=location,
                    channel="card_present",
                ),
                True,
            )
        )
        t += timedelta(minutes=rng.uniform(3, 25))
    return out


def amount_spike(ctx: Context) -> Labelled:
    """An attacker in the victim's account adds a new card in the small hours and immediately
    makes a purchase many times the user's normal spend in an easy-to-cash-out category."""
    rng = ctx.rng
    victim = ctx.victim()
    device = new_token(rng, "dev_", 12)
    ip = datacenter_ip(rng) if rng.random() < 0.5 else residential_ip(rng)
    location = _home_location(victim, rng)
    # Shift the attack start to 01:00–05:00 IST (19:30–23:30 UTC the previous day).
    day = ctx.attack_time(room=timedelta(hours=30))
    t = day.replace(hour=19, minute=30, second=0, microsecond=0) + timedelta(
        minutes=rng.uniform(0, 240)
    )
    if t >= ctx.window_end - timedelta(hours=2):
        t -= timedelta(days=1)
    if t < ctx.window_start:
        t += timedelta(days=1)
    card = _stolen_card(rng, t)
    out: Labelled = [
        (
            ev.instrument(
                rng,
                user_id=victim.user_id,
                card=card,
                device_id=device,
                ip=ip,
                ts=t,
                location=location,
                name_matches_account=rng.random() < 0.3,
            ),
            True,
        )
    ]
    for _ in range(rng.randint(1, 3)):
        t += timedelta(minutes=rng.uniform(2, 30))
        out.append(
            (
                ev.payment(
                    rng,
                    user_id=victim.user_id,
                    card_id=card.card_id,
                    device_id=device,
                    ip=ip,
                    ts=t,
                    amount=min(typical_spend(victim) * rng.uniform(20, 80), 200_000),
                    category=rng.choice([MC.GIFT_CARDS, MC.CRYPTO]),
                    location=location,
                    channel="online",
                ),
                True,
            )
        )
    return out


def account_takeover(ctx: Context) -> Labelled:
    """Stolen credentials: a new device on a hosting IP abroad drains the victim's existing
    card through transfers and gift cards."""
    rng = ctx.rng
    victim = ctx.victim()
    card = rng.choice(victim.cards)
    device = new_token(rng, "dev_", 12)
    ip = datacenter_ip(rng)
    location = _foreign_location(rng)
    t = ctx.attack_time()
    out: Labelled = []
    for _ in range(rng.randint(2, 4)):
        t += timedelta(minutes=rng.uniform(2, 20))
        out.append(
            (
                ev.payment(
                    rng,
                    user_id=victim.user_id,
                    card_id=card.card_id,
                    device_id=device,
                    ip=ip,
                    ts=t,
                    amount=typical_spend(victim) * rng.uniform(5, 15),
                    category=rng.choice([MC.MONEY_TRANSFER, MC.GIFT_CARDS]),
                    location=location,
                    channel="online",
                ),
                True,
            )
        )
    return out


PATTERNS: dict[str, tuple[Callable[[Context], Labelled], float]] = {
    "card_testing": (card_testing, 0.25),
    "cvv_brute_force": (cvv_brute_force, 0.20),
    "impossible_travel": (impossible_travel, 0.20),
    "amount_spike": (amount_spike, 0.15),
    "account_takeover": (account_takeover, 0.20),
}
