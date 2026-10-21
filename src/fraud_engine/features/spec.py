"""The feature catalogue: every signal the engine computes, in a fixed order.

The rules engine validates rule conditions against these names, the ML model uses this order
for its columns, and the dashboard shows the descriptions next to the values. Add a feature
here first, then compute it in `engine.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Bump when a feature's meaning changes, so stored vectors and trained models can be matched.
FEATURE_VERSION = 1

Kind = Literal["bool", "count", "float", "str"]


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    group: str
    kind: Kind
    description: str


FEATURES: tuple[FeatureSpec, ...] = (
    # --- Event context ----------------------------------------------------------------------
    FeatureSpec("event_type", "context", "str", "payment or instrument"),
    FeatureSpec("merchant_category", "context", "str", "Merchant category of the payment"),
    FeatureSpec("is_payment", "context", "bool", "True for payments, False for card events"),
    FeatureSpec("amount", "context", "float", "Payment amount"),
    FeatureSpec("payment_approved", "context", "bool", "Issuer approved the payment attempt"),
    FeatureSpec("channel_card_present", "context", "bool", "Card was physically swiped/tapped"),
    FeatureSpec(
        "high_risk_category",
        "context",
        "bool",
        "Gift cards, crypto, gambling or money transfer: value that is easy to cash out",
    ),
    # --- Velocity (Redis sorted sets, event-time windows) ------------------------------------
    FeatureSpec("cards_added_1h", "velocity", "count", "Cards added to this account in 1h"),
    FeatureSpec("cards_added_3h", "velocity", "count", "Cards added to this account in 3h"),
    FeatureSpec("cards_added_24h", "velocity", "count", "Cards added to this account in 24h"),
    FeatureSpec("card_txn_count_5m", "velocity", "count", "Payments on this card in 5 min"),
    FeatureSpec("card_txn_count_1h", "velocity", "count", "Payments on this card in 1h"),
    FeatureSpec("device_txn_count_5m", "velocity", "count", "Payments from this device in 5 min"),
    FeatureSpec("device_txn_count_1h", "velocity", "count", "Payments from this device in 1h"),
    FeatureSpec(
        "device_distinct_cards_24h", "velocity", "count", "Distinct cards seen on this device, 24h"
    ),
    FeatureSpec(
        "ip_distinct_cards_24h", "velocity", "count", "Distinct cards seen from this IP, 24h"
    ),
    FeatureSpec(
        "card_failed_attempts_1h", "velocity", "count", "Declined payments on this card in 1h"
    ),
    FeatureSpec(
        "user_total_amount_1h", "velocity", "float", "Total attempted amount by this user in 1h"
    ),
    # --- Behaviour (per-user baselines) --------------------------------------------------------
    FeatureSpec(
        "user_payments_90d", "behaviour", "count", "Approved payments in the user's 90-day baseline"
    ),
    FeatureSpec(
        "amount_zscore",
        "behaviour",
        "float",
        "Std deviations above the user's 90-day mean (log scale); empty under 5 payments",
    ),
    FeatureSpec(
        "amount_vs_typical",
        "behaviour",
        "float",
        "Amount divided by the user's typical (geometric-mean) payment",
    ),
    FeatureSpec(
        "hour_share",
        "behaviour",
        "float",
        "Share of the user's past payments within an hour of this time of day",
    ),
    FeatureSpec(
        "unusual_hour", "behaviour", "bool", "Fewer than 5% of the user's payments at this hour"
    ),
    FeatureSpec(
        "category_first_time", "behaviour", "bool", "First payment by this user in this category"
    ),
    # --- Account and device -------------------------------------------------------------------
    FeatureSpec("account_age_hours", "account", "float", "Hours since the account was first seen"),
    FeatureSpec(
        "device_age_hours", "account", "float", "Hours since this device was first seen on account"
    ),
    FeatureSpec("new_device", "account", "bool", "Device first seen on this account under 24h ago"),
    # --- Geo and network ------------------------------------------------------------------------
    FeatureSpec(
        "distance_from_last_km", "geo", "float", "Distance from this card's previous payment"
    ),
    FeatureSpec(
        "travel_speed_kmh", "geo", "float", "Speed needed to get here from the previous payment"
    ),
    FeatureSpec("impossible_travel", "geo", "bool", "Over 100 km away at a speed above 900 km/h"),
    FeatureSpec("new_country", "geo", "bool", "Country first seen on this account under 24h ago"),
    FeatureSpec(
        "geo_bin_country_mismatch",
        "geo",
        "bool",
        "Event country differs from the card's issuing country",
    ),
    FeatureSpec("datacenter_ip", "geo", "bool", "IP belongs to a cloud or hosting range"),
    # --- Instrument -----------------------------------------------------------------------------
    FeatureSpec("card_age_hours", "instrument", "float", "Hours since the card was added"),
    FeatureSpec("bin_risk", "instrument", "float", "Risk of the card's BIN, 0-1 (table lookup)"),
    FeatureSpec(
        "card_name_mismatch",
        "instrument",
        "bool",
        "Cardholder name did not match the account holder when the card was added",
    ),
    FeatureSpec(
        "new_card_high_amount",
        "instrument",
        "bool",
        "Card added under 24h ago and the amount is 5x the user's typical spend",
    ),
)

FEATURE_NAMES: tuple[str, ...] = tuple(f.name for f in FEATURES)
SPECS: dict[str, FeatureSpec] = {f.name: f for f in FEATURES}

# Columns the ML model sees: everything except free-text context.
NUMERIC_FEATURES: tuple[str, ...] = tuple(f.name for f in FEATURES if f.kind != "str")
