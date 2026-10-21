"""Reference data and user personas for synthetic traffic.

BINs, IPs and IDs here are invented. They look realistic enough to exercise the engine but
do not correspond to real issuers or people.
"""

from __future__ import annotations

import random
import string
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..schemas import MerchantCategory as MC

# (city, lat, lon) — home bases for normal users, all in India.
DOMESTIC_CITIES: list[tuple[str, float, float]] = [
    ("Bengaluru", 12.9716, 77.5946),
    ("Mumbai", 19.0760, 72.8777),
    ("Delhi", 28.6139, 77.2090),
    ("Chennai", 13.0827, 80.2707),
    ("Hyderabad", 17.3850, 78.4867),
    ("Pune", 18.5204, 73.8567),
    ("Kolkata", 22.5726, 88.3639),
    ("Ahmedabad", 23.0225, 72.5714),
    ("Jaipur", 26.9124, 75.7873),
    ("Kochi", 9.9312, 76.2673),
]

# (city, country, lat, lon) — where stolen/cloned cards show up.
FOREIGN_CITIES: list[tuple[str, str, float, float]] = [
    ("Moscow", "RU", 55.7558, 37.6173),
    ("Lagos", "NG", 6.5244, 3.3792),
    ("Sao Paulo", "BR", -23.5505, -46.6333),
    ("Bucharest", "RO", 44.4268, 26.1025),
    ("Jakarta", "ID", -6.2088, 106.8456),
    ("Manila", "PH", 14.5995, 120.9842),
    ("Kyiv", "UA", 50.4501, 30.5234),
]

# (city, country, lat, lon) — where genuine customers go on holiday.
TRAVEL_CITIES: list[tuple[str, str, float, float]] = [
    ("Dubai", "AE", 25.2048, 55.2708),
    ("Singapore", "SG", 1.3521, 103.8198),
    ("Bangkok", "TH", 13.7563, 100.5018),
    ("London", "GB", 51.5072, -0.1276),
    ("Kathmandu", "NP", 27.7172, 85.3240),
]

# First octets of well-known cloud / hosting ranges. Fraud tooling often runs from these;
# real customers almost never pay from a datacenter IP.
DATACENTER_PREFIXES = ["3", "13", "18", "34", "35", "52", "54", "104.131", "159.65", "167.99"]

# Typical spend in INR per category: (lognormal median, sigma).
CATEGORY_SPEND: dict[MC, tuple[float, float]] = {
    MC.GROCERY: (900, 0.6),
    MC.FOOD_DELIVERY: (450, 0.4),
    MC.RESTAURANTS: (1200, 0.6),
    MC.FUEL: (1500, 0.4),
    MC.TRANSPORT: (250, 0.6),
    MC.UTILITIES: (1800, 0.5),
    MC.PHARMACY: (600, 0.7),
    MC.FASHION: (2500, 0.7),
    MC.ELECTRONICS: (8000, 0.9),
    MC.ENTERTAINMENT: (500, 0.5),
    MC.TRAVEL: (6000, 0.8),
    MC.GIFT_CARDS: (1000, 0.5),
    MC.CRYPTO: (5000, 0.8),
    MC.GAMBLING: (1000, 0.7),
    MC.MONEY_TRANSFER: (3000, 0.8),
}

# What everyday spending looks like. High-risk categories appear rarely in normal traffic —
# their mere presence must not be a perfect fraud label, or the ML model learns a shortcut.
EVERYDAY_WEIGHTS: dict[MC, float] = {
    MC.GROCERY: 20,
    MC.FOOD_DELIVERY: 22,
    MC.RESTAURANTS: 8,
    MC.FUEL: 8,
    MC.TRANSPORT: 12,
    MC.UTILITIES: 4,
    MC.PHARMACY: 4,
    MC.FASHION: 5,
    MC.ELECTRONICS: 2,
    MC.ENTERTAINMENT: 6,
    MC.TRAVEL: 1.5,
    MC.GIFT_CARDS: 0.8,
    MC.MONEY_TRANSFER: 1.2,
    MC.CRYPTO: 0.3,
    MC.GAMBLING: 0.2,
}

_TOKEN_ALPHABET = string.ascii_lowercase + string.digits


def new_uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def new_token(rng: random.Random, prefix: str, length: int = 16) -> str:
    return prefix + "".join(rng.choices(_TOKEN_ALPHABET, k=length))


def residential_ip(rng: random.Random) -> str:
    # Avoid reserved/private ranges and the datacenter prefixes above.
    first = rng.choice([27, 49, 59, 103, 106, 110, 115, 117, 122, 157, 171, 182, 223])
    return f"{first}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"


def datacenter_ip(rng: random.Random) -> str:
    parts = rng.choice(DATACENTER_PREFIXES).split(".")
    while len(parts) < 4:
        parts.append(str(rng.randint(1, 254)))
    return ".".join(parts)


def domestic_bin(rng: random.Random) -> str:
    return rng.choice(["4", "5", "6"]) + "".join(rng.choices(string.digits, k=5))


def jitter(lat: float, lon: float, rng: random.Random, km: float = 8) -> tuple[float, float]:
    """Move a point by up to ~km so a user's transactions aren't all at one coordinate."""
    d = km / 111.0
    return round(lat + rng.uniform(-d, d), 5), round(lon + rng.uniform(-d, d), 5)


@dataclass
class Card:
    card_id: str
    bin: str
    card_type: str
    issuing_country: str
    added_at: datetime


@dataclass
class Persona:
    user_id: str
    city: str
    lat: float
    lon: float
    cards: list[Card]
    devices: list[str]
    home_ip: str
    mobile_ip: str
    txns_per_day: float
    spend_multiplier: float
    active_hours: tuple[int, int]  # local (IST) hours, inclusive start / exclusive end
    category_weights: dict[MC, float] = field(default_factory=dict)
    # Share of online sessions routed through a VPN (a datacenter IP). Zero for most users.
    vpn_share: float = 0.0


def make_persona(rng: random.Random, window_start: datetime) -> Persona:
    city, lat, lon = rng.choice(DOMESTIC_CITIES)
    n_cards = rng.choices([1, 2, 3], weights=[60, 32, 8])[0]
    cards = [
        Card(
            card_id=new_token(rng, "tok_"),
            bin=domestic_bin(rng),
            card_type=rng.choices(["debit", "credit"], weights=[55, 45])[0],
            issuing_country="IN",
            # Long-standing cards, added well before the simulated window.
            added_at=window_start - timedelta(days=rng.uniform(30, 720)),
        )
        for _ in range(n_cards)
    ]
    # Personal taste: perturb the everyday mix so users differ from each other.
    weights = {c: w * rng.uniform(0.3, 1.8) for c, w in EVERYDAY_WEIGHTS.items()}
    start = rng.choice([6, 7, 8, 9, 10])
    end = rng.choice([21, 22, 23, 24])
    return Persona(
        user_id=new_token(rng, "usr_", 12),
        city=city,
        lat=lat,
        lon=lon,
        cards=cards,
        devices=[new_token(rng, "dev_", 12) for _ in range(rng.choice([1, 1, 2]))],
        home_ip=residential_ip(rng),
        mobile_ip=residential_ip(rng),
        txns_per_day=rng.uniform(0.4, 4.0),
        spend_multiplier=rng.lognormvariate(0, 0.4),
        active_hours=(start, end),
        category_weights=weights,
        vpn_share=rng.uniform(0.3, 0.7) if rng.random() < 0.05 else 0.0,
    )
