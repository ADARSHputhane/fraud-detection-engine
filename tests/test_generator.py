import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from fraud_engine.generator import generate_dataset
from fraud_engine.generator.cli import main as cli_main
from fraud_engine.generator.patterns import PATTERNS
from fraud_engine.schemas import contains_pan

END = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def dataset():
    return generate_dataset(users=150, days=14, seed=7, end=END)


def _haversine_km(a: dict, b: dict) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(h))


def test_deterministic_for_a_seed():
    a = generate_dataset(users=20, days=5, seed=3, end=END)
    b = generate_dataset(users=20, days=5, seed=3, end=END)
    c = generate_dataset(users=20, days=5, seed=4, end=END)
    assert a.events == b.events and a.labels == b.labels
    assert a.events != c.events


def test_fraud_rate_near_target(dataset):
    assert 0.04 <= dataset.stats["fraud_rate"] <= 0.065


def test_every_pattern_present(dataset):
    assert set(dataset.stats["fraud_events_by_pattern"]) == set(PATTERNS)


def test_events_time_ordered_and_labels_aligned(dataset):
    times = [datetime.fromisoformat(e["timestamp"]) for e in dataset.events]
    assert times == sorted(times)
    assert [e["event_id"] for e in dataset.events] == [lbl["event_id"] for lbl in dataset.labels]
    assert len({e["event_id"] for e in dataset.events}) == len(dataset.events)


def test_labels_never_leak_into_events(dataset):
    for e in dataset.events:
        assert not {"is_fraud", "pattern", "attack_id", "label"} & e.keys()


def test_no_card_numbers_anywhere(dataset):
    for e in dataset.events:
        assert not contains_pan(e["card_id"])


def test_normal_traffic_has_some_high_risk_categories(dataset):
    # If gift cards / crypto only ever appeared in fraud, the ML model would learn a shortcut.
    assert dataset.stats["high_risk_category_share_in_normal_payments"] > 0.005


def test_amount_spike_card_added_in_small_hours_ist(dataset):
    ist = ZoneInfo("Asia/Kolkata")
    adds = [
        e
        for e, lbl in zip(dataset.events, dataset.labels, strict=True)
        if lbl["pattern"] == "amount_spike" and e["event_type"] == "instrument"
    ]
    assert adds
    for e in adds:
        hour = datetime.fromisoformat(e["timestamp"]).astimezone(ist).hour
        assert 1 <= hour < 5


def test_impossible_travel_is_physically_impossible(dataset):
    """The first cloned-card swipe must imply travel faster than an airliner (~900 km/h)
    from the cardholder's genuine purchase just before it."""
    last_legit_by_card: dict[str, dict] = {}
    checked = 0
    seen_attacks = set()
    for e, lbl in zip(dataset.events, dataset.labels, strict=True):
        if e["event_type"] != "payment":
            continue
        if lbl["pattern"] == "impossible_travel" and lbl["attack_id"] not in seen_attacks:
            seen_attacks.add(lbl["attack_id"])
            prev = last_legit_by_card[e["card_id"]]
            hours = (
                datetime.fromisoformat(e["timestamp"]) - datetime.fromisoformat(prev["timestamp"])
            ).total_seconds() / 3600
            assert _haversine_km(prev["geo"], e["geo"]) / hours > 900
            checked += 1
        elif not lbl["is_fraud"]:
            last_legit_by_card[e["card_id"]] = e
    assert checked > 0


def test_cvv_brute_force_shape(dataset):
    attacks = defaultdict(list)
    for e, lbl in zip(dataset.events, dataset.labels, strict=True):
        if lbl["pattern"] == "cvv_brute_force":
            attacks[lbl["attack_id"]].append(e)
    assert attacks
    for evs in attacks.values():
        declines = [e for e in evs if e.get("decline_reason") == "incorrect_cvv"]
        assert len(declines) >= 5
        assert evs[-1]["status"] == "approved"
        assert len({e["card_id"] for e in evs}) == 1


def test_card_testing_adds_several_cards_from_one_device(dataset):
    attacks = defaultdict(list)
    for e, lbl in zip(dataset.events, dataset.labels, strict=True):
        if lbl["pattern"] == "card_testing":
            attacks[lbl["attack_id"]].append(e)
    for evs in attacks.values():
        adds = [e for e in evs if e["event_type"] == "instrument"]
        assert len(adds) >= 3
        assert len({e["device_id"] for e in evs}) == 1


def test_cli_generate_writes_files(tmp_path):
    out = tmp_path / "data"
    code = cli_main(["generate", "--users", "10", "--days", "3", "--out", str(out)])
    assert code == 0
    events = (out / "events.jsonl").read_text().splitlines()
    labels = (out / "labels.jsonl").read_text().splitlines()
    assert len(events) == len(labels) > 0
    assert json.loads((out / "stats.json").read_text())["events"] == len(events)
