from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fakeredis.aioredis import FakeRedis

from fraud_engine.features import FEATURE_NAMES, FeatureEngine
from fraud_engine.features.geo import haversine_km, travel
from fraud_engine.features.reference import BinRiskTable, DatacenterRanges
from fraud_engine.generator import generate_dataset

# A fixed date in the past: windows run on event time, so history from any date works.
T0 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
BENGALURU = {"lat": 12.9716, "lon": 77.5946, "country": "IN", "city": "Bengaluru"}
DELHI = {"lat": 28.6139, "lon": 77.2090, "country": "IN", "city": "Delhi"}
MOSCOW = {"lat": 55.7558, "lon": 37.6173, "country": "RU", "city": "Moscow"}


@pytest.fixture
async def engine():
    client = FakeRedis()
    yield FeatureEngine(client)
    await client.aclose()


def at(**delta) -> str:
    return (T0 + timedelta(**delta)).isoformat()


def pay(ts: str, **kw) -> dict:
    event = {
        "event_type": "payment",
        "event_id": str(uuid4()),
        "user_id": "usr_alice",
        "card_id": "tok_alice0001",
        "device_id": "dev_alice",
        "ip_address": "49.37.12.8",
        "geo": BENGALURU,
        "timestamp": ts,
        "amount": "1000.00",
        "currency": "INR",
        "merchant_id": "mer_grocery_001",
        "merchant_category": "grocery",
        "channel": "card_present",
        "status": "approved",
    }
    event.update(kw)
    return event


def card(ts: str, **kw) -> dict:
    event = {
        "event_type": "instrument",
        "event_id": str(uuid4()),
        "user_id": "usr_alice",
        "card_id": "tok_alice0001",
        "device_id": "dev_alice",
        "ip_address": "49.37.12.8",
        "geo": BENGALURU,
        "timestamp": ts,
        "action": "added",
        "bin": "457173",
        "card_type": "credit",
        "issuing_country": "IN",
        "name_matches_account": True,
    }
    event.update(kw)
    return event


async def test_vector_has_every_feature(engine):
    f = await engine.compute(pay(at()))
    assert tuple(f) == FEATURE_NAMES


async def test_first_ever_transaction(engine):
    f = await engine.compute(pay(at(), card_id="tok_unknown001"))
    assert f["user_payments_90d"] == 0
    assert f["amount_zscore"] is None and f["amount_vs_typical"] is None
    assert f["hour_share"] is None and f["unusual_hour"] is False
    assert f["category_first_time"] is True
    assert f["account_age_hours"] == 0 and f["new_device"] is True
    assert f["distance_from_last_km"] is None and f["impossible_travel"] is False
    # A card we never saw being added: age and BIN are unknown, not zero.
    assert f["card_age_hours"] is None and f["bin_risk"] is None
    assert f["new_card_high_amount"] is False
    assert f["card_txn_count_5m"] == 1  # windows include the event itself


async def test_velocity_windows_use_event_time(engine):
    for minutes in (0, 2, 10):
        f = await engine.compute(pay(at(minutes=minutes)))
    assert f["card_txn_count_5m"] == 1  # the 2-minute payment is 8 minutes earlier
    assert f["card_txn_count_1h"] == 3
    assert f["device_txn_count_1h"] == 3
    f = await engine.compute(pay(at(minutes=75)))
    assert f["card_txn_count_1h"] == 1


async def test_cards_added_and_distinct_cards(engine):
    counts = []
    for i in range(4):
        f = await engine.compute(card(at(minutes=30 * i), card_id=f"tok_stolen000{i}"))
        counts.append(f["cards_added_3h"])
    assert counts == [1, 2, 3, 4]
    assert f["cards_added_1h"] == 3  # added at 30, 60 and 90 minutes
    assert f["device_distinct_cards_24h"] == 4
    assert f["ip_distinct_cards_24h"] == 4

    f = await engine.compute(pay(at(hours=26), card_id="tok_stolen0003"))
    assert f["cards_added_24h"] == 0
    assert f["device_distinct_cards_24h"] == 1


async def test_failed_attempts_then_success(engine):
    for s in range(6):
        await engine.compute(
            pay(at(seconds=30 * s), status="declined", decline_reason="incorrect_cvv")
        )
    f = await engine.compute(pay(at(minutes=4)))
    assert f["card_failed_attempts_1h"] == 6
    assert f["payment_approved"] is True


async def test_user_total_amount_1h(engine):
    await engine.compute(pay(at(), amount="100.00"))
    await engine.compute(pay(at(minutes=30), amount="250.50", status="declined"))
    f = await engine.compute(pay(at(minutes=50), amount="49.50"))
    assert f["user_total_amount_1h"] == 400.0
    f = await engine.compute(pay(at(minutes=100), amount="10.00"))
    assert f["user_total_amount_1h"] == 59.5


async def test_amount_baseline_excludes_current_event_and_flags_spike(engine):
    for day, amount in enumerate(["800", "1200", "950", "1100", "1000", "900", "1050"]):
        await engine.compute(pay(at(days=day), amount=amount))
    normal = await engine.compute(pay(at(days=8), amount="1000"))
    assert normal["user_payments_90d"] == 7
    assert abs(normal["amount_zscore"]) < 1
    spike = await engine.compute(pay(at(days=8, hours=1), amount="50000"))
    assert spike["user_payments_90d"] == 8
    assert spike["amount_zscore"] > 5
    assert 40 < spike["amount_vs_typical"] < 60


async def test_baseline_needs_history_and_ignores_declines_and_old_payments(engine):
    for day in range(4):
        await engine.compute(pay(at(days=day)))
    await engine.compute(pay(at(days=4), status="declined", decline_reason="expired_card"))
    f = await engine.compute(pay(at(days=5)))
    assert f["user_payments_90d"] == 4 and f["amount_zscore"] is None
    f = await engine.compute(pay(at(days=200)))
    assert f["user_payments_90d"] == 0  # everything is older than 90 days


async def test_hour_share_and_unusual_hour(engine):
    for day in range(12):
        await engine.compute(pay(at(days=day)))  # always 10:00 UTC
    late = await engine.compute(pay(at(days=12, hours=-7)))  # 03:00 UTC
    assert late["hour_share"] == 0 and late["unusual_hour"] is True
    usual = await engine.compute(pay(at(days=13, hours=1)))  # 11:00, within an hour of 10:00
    assert usual["hour_share"] > 0.9 and usual["unusual_hour"] is False


async def test_category_novelty_and_high_risk(engine):
    first = await engine.compute(pay(at()))
    again = await engine.compute(pay(at(minutes=10)))
    gift = await engine.compute(pay(at(minutes=20), merchant_category="gift_cards"))
    assert first["category_first_time"] and not again["category_first_time"]
    assert gift["category_first_time"] and gift["high_risk_category"]
    assert not again["high_risk_category"]


async def test_impossible_travel(engine):
    await engine.compute(pay(at()))
    f = await engine.compute(pay(at(minutes=30), geo=MOSCOW))
    assert f["impossible_travel"] is True
    assert 5_700 < f["distance_from_last_km"] < 6_000
    assert f["new_country"] is True


async def test_domestic_trip_is_not_impossible(engine):
    await engine.compute(pay(at()))
    f = await engine.compute(pay(at(hours=6), geo=DELHI))
    assert f["impossible_travel"] is False
    assert 250 < f["travel_speed_kmh"] < 320


async def test_geolocation_jitter_is_not_travel(engine):
    await engine.compute(pay(at()))
    nearby = {**BENGALURU, "lat": BENGALURU["lat"] + 0.15}  # ~17 km away, a minute later
    f = await engine.compute(pay(at(minutes=1), geo=nearby))
    assert f["travel_speed_kmh"] > 900
    assert f["impossible_travel"] is False


async def test_clock_skew_and_out_of_order_events(engine):
    await engine.compute(pay(at(minutes=30)))
    # Arrives later but is timestamped earlier: judged on the absolute gap.
    f = await engine.compute(pay(at(), geo=MOSCOW))
    assert f["impossible_travel"] is True
    # The older event must not replace the card's latest known location.
    f = await engine.compute(pay(at(minutes=40)))
    assert f["distance_from_last_km"] < 1
    # Same timestamp in two cities: the gap is floored instead of dividing by zero.
    f = await engine.compute(pay(at(minutes=40), geo=DELHI))
    assert f["impossible_travel"] is True


async def test_card_age_bin_and_new_card_high_amount(engine):
    await engine.compute(
        card(at(), card_type="prepaid", issuing_country="US", name_matches_account=False)
    )
    f = await engine.compute(pay(at(hours=2), amount="20000.00", merchant_category="gift_cards"))
    assert f["card_age_hours"] == 2.0
    assert f["bin_risk"] == 0.6
    assert f["card_name_mismatch"] is True
    assert f["geo_bin_country_mismatch"] is True
    assert f["new_card_high_amount"] is True
    later = await engine.compute(pay(at(hours=30), amount="20000.00"))
    assert later["new_card_high_amount"] is False


async def test_card_age_is_clamped_under_clock_skew(engine):
    await engine.compute(card(at()))
    f = await engine.compute(pay(at(seconds=-30)))  # client clock 30 s behind
    assert f["card_age_hours"] == 0


async def test_new_device_and_account_age(engine):
    await engine.compute(pay(at()))
    same = await engine.compute(pay(at(hours=48)))
    other = await engine.compute(pay(at(hours=48, minutes=5), device_id="dev_unknown"))
    assert same["account_age_hours"] == 48 and same["new_device"] is False
    assert other["new_device"] is True and other["device_age_hours"] == 0


async def test_new_country_expires_after_a_day(engine):
    await engine.compute(pay(at(), geo=MOSCOW))
    f = await engine.compute(pay(at(hours=2), geo=MOSCOW))
    assert f["new_country"] is True
    f = await engine.compute(pay(at(hours=30), geo=MOSCOW))
    assert f["new_country"] is False


async def test_missing_geo(engine):
    f = await engine.compute(pay(at(), geo=None))
    assert f["new_country"] is None and f["geo_bin_country_mismatch"] is None
    assert f["impossible_travel"] is False


async def test_reprocessing_an_event_is_idempotent(engine):
    event = pay(at())
    first = await engine.compute(event)
    again = await engine.compute(event)
    assert first == again
    f = await engine.compute(pay(at(minutes=1)))
    assert f["card_txn_count_5m"] == 2  # not 3
    for day in range(1, 6):
        f = await engine.compute(pay(at(days=day)))
    assert f["user_payments_90d"] == 6  # the retried event counted once


async def test_replay_is_deterministic():
    ds = generate_dataset(users=15, days=4, seed=7)
    client = FakeRedis()
    engine = FeatureEngine(client)
    first = [await engine.compute(e) for e in ds.events]
    assert await engine.reset() > 0
    second = [await engine.compute(e) for e in ds.events]
    assert first == second
    await client.aclose()


def test_haversine_known_distance():
    km = haversine_km(BENGALURU["lat"], BENGALURU["lon"], DELHI["lat"], DELHI["lon"])
    assert 1_730 < km < 1_760


def test_travel_floors_tiny_gaps():
    distance, speed, impossible = travel(0, 0, 100.0, 0, 1, 100.0)
    assert speed == pytest.approx(distance * 60)
    assert impossible


def test_datacenter_ranges():
    ranges = DatacenterRanges.default()
    assert ranges.contains("54.12.1.9") and ranges.contains("104.131.7.7")
    assert not ranges.contains("49.37.12.8")
    assert not ranges.contains("2001:db8::1")
    assert not ranges.contains("not-an-ip")


def test_generator_datacenter_ips_are_detected():
    import random

    from fraud_engine.generator.personas import datacenter_ip, residential_ip

    rng = random.Random(3)
    ranges = DatacenterRanges.default()
    assert all(ranges.contains(datacenter_ip(rng)) for _ in range(500))
    assert not any(ranges.contains(residential_ip(rng)) for _ in range(500))


def test_bin_risk_lookup_and_fallback():
    table = BinRiskTable.default()
    assert table.risk("00000012", "debit") == 0.95  # listed prefix wins
    assert table.risk("457173", "prepaid") == 0.6
    assert table.risk("457173", "debit") == 0.2
    assert table.risk(None, None) == 0.3
