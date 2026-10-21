import os
import time

import pytest

from fraud_engine.features import FEATURE_NAMES
from fraud_engine.rules import (
    ConditionError,
    RulesEngine,
    RulesError,
    RuleSet,
    compile_condition,
    evaluate_ruleset,
)
from fraud_engine.rules.engine import default_rules_text


def check(condition: str, **features) -> bool:
    test, _ = compile_condition(condition, FEATURE_NAMES)
    return test(features)


def base(**overrides) -> dict:
    f = dict.fromkeys(FEATURE_NAMES)
    f.update(overrides)
    return f


def test_comparisons_and_boolean_logic():
    assert check("cards_added_3h >= 3", cards_added_3h=3)
    assert not check("cards_added_3h >= 3", cards_added_3h=2)
    assert check(
        "card_failed_attempts_1h >= 5 and payment_approved",
        card_failed_attempts_1h=6,
        payment_approved=True,
    )
    assert check("datacenter_ip or new_device", datacenter_ip=False, new_device=True)
    assert check("not impossible_travel", impossible_travel=False)
    assert check("1 < amount_zscore <= 3", amount_zscore=2.5)
    assert not check("1 < amount_zscore <= 3", amount_zscore=3.5)
    assert check("amount_zscore > -1", amount_zscore=0)
    assert check('merchant_category in ("gift_cards", "crypto")', merchant_category="crypto")
    assert check('merchant_category not in ["grocery"]', merchant_category="crypto")
    assert check(
        "new_device == true and datacenter_ip == false", new_device=True, datacenter_ip=False
    )


def test_missing_values_never_match_ordering_comparisons():
    assert not check("amount_zscore > 5", amount_zscore=None)
    assert not check("amount_zscore < 5", amount_zscore=None)
    assert check("amount_zscore is None", amount_zscore=None)
    assert check("amount_zscore == null", amount_zscore=None)
    assert not check("impossible_travel", impossible_travel=None)
    # A type mismatch is a non-match, not a crash.
    assert not check("merchant_category > 3", merchant_category="grocery")


def test_compile_reports_features_read():
    _, reads = compile_condition("datacenter_ip and new_device", FEATURE_NAMES)
    assert reads == {"datacenter_ip", "new_device"}


@pytest.mark.parametrize(
    "condition",
    [
        "__import__('os').system('id')",  # function call
        "amount.__class__",  # attribute access
        "amount.real > 1",
        "amount[0] > 1",  # subscript
        "amount * 2 > 10",  # arithmetic
        "(lambda: 1)()",
        "[x for x in (1, 2)]",
        "amount_zscore > (1, amount)",  # non-literal inside a list
        "not_a_feature > 1",  # typo
        "amount >",  # syntax error
        "amount := 5",
    ],
)
def test_unsafe_or_invalid_conditions_are_rejected(condition):
    with pytest.raises(ConditionError):
        compile_condition(condition, FEATURE_NAMES)


RULES = """
rules:
  - id: R100
    name: Spike
    condition: amount_zscore > 3
    action: REVIEW
    weight: 60
    reason: "z={amount_zscore:.1f} on a card {card_age_hours:.1f} h old"
  - id: R101
    name: Testing
    condition: cards_added_3h >= 3
    action: DENY
    weight: 70
    reason: "{cards_added_3h} cards"
  - id: R102
    name: Off
    condition: amount > 0
    action: DENY
    weight: 100
    reason: never
    enabled: false
"""


def test_ruleset_evaluates_hits_action_and_score():
    rs = RuleSet.from_yaml(RULES)
    assert [r.id for r in rs.rules] == ["R100", "R101"]  # disabled rule skipped
    result = rs.evaluate(base(amount_zscore=4.25, cards_added_3h=3, card_age_hours=None))
    assert [h.id for h in result.hits] == ["R100", "R101"]
    assert result.action == "DENY"  # strongest action wins
    assert result.score == 100  # 60 + 70, capped
    assert result.hits[0].reason == "z=4.2 on a card n/a h old"
    assert rs.evaluate(base(amount_zscore=1)).action is None


def test_ruleset_version_tracks_content():
    assert RuleSet.from_yaml(RULES).version == RuleSet.from_yaml(RULES).version
    assert RuleSet.from_yaml(RULES).version != RuleSet.from_yaml(RULES + "\n# edited").version


def rule_yaml(**overrides) -> str:
    rule = {
        "id": "R001",
        "name": "x",
        "condition": "'amount > 1'",
        "action": "DENY",
        "weight": "1",
        "reason": "x",
    }
    rule.update(overrides)
    return "rules:\n  - " + "\n    ".join(f"{k}: {v}" for k, v in rule.items()) + "\n"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (rule_yaml(id="R1"), "id"),
        (rule_yaml(action="BLOCK"), "action"),
        (rule_yaml(weight="101"), "weight"),
        (rule_yaml(extra="1"), "extra"),
        (rule_yaml(condition="'nope > 1'"), "unknown feature"),
        (rule_yaml(reason="'{amount.__class__}'"), "placeholder"),
        (rule_yaml(reason="'{nope}'"), "placeholder"),
        (rule_yaml() + rule_yaml().removeprefix("rules:\n"), "duplicate"),
        ("rules: [", "YAML"),
    ],
)
def test_invalid_rules_files_are_rejected(text, message):
    with pytest.raises(RulesError, match=message):
        RuleSet.from_yaml(text)


def _vector_for(rule_id: str) -> dict:
    return {
        "R001": base(cards_added_3h=3),
        "R002": base(
            impossible_travel=True,
            new_device=True,
            distance_from_last_km=5000.0,
            travel_speed_kmh=9000.0,
        ),
        "R003": base(card_failed_attempts_1h=5, payment_approved=True),
        "R004": base(new_card_high_amount=True, amount_vs_typical=30.0, card_age_hours=0.5),
        "R005": base(device_distinct_cards_24h=4),
        "R006": base(datacenter_ip=True, new_device=True, device_age_hours=0.1),
        "R007": base(
            impossible_travel=True, new_device=False, new_country=False, travel_speed_kmh=5000.0
        ),
        "R008": base(card_failed_attempts_1h=3, new_device=True, device_age_hours=0.0),
        "R009": base(new_country=True, new_device=True, geo_bin_country_mismatch=True),
        "R010": base(card_name_mismatch=True, new_device=True, device_age_hours=1.0),
    }[rule_id]


def test_every_default_rule_fires_on_its_scenario_with_a_readable_reason():
    rs = RuleSet.from_yaml(default_rules_text())
    assert len(rs.rules) == 10
    for rule in rs.rules:
        result = rs.evaluate(_vector_for(rule.id))
        hit = next((h for h in result.hits if h.id == rule.id), None)
        assert hit is not None, rule.id
        assert "{" not in hit.reason and "n/a" not in hit.reason, (rule.id, hit.reason)
    assert rs.evaluate(base()).hits == []  # an empty vector fires nothing


def test_genuine_cardholder_after_clone_is_reviewed_not_denied():
    rs = RuleSet.from_yaml(default_rules_text())
    result = rs.evaluate(_vector_for("R007"))
    assert result.action == "REVIEW"


def test_hot_reload_installs_new_rules_and_survives_a_broken_edit(tmp_path, caplog):
    path = tmp_path / "rules.yml"
    path.write_text(RULES)
    engine = RulesEngine(path, check_interval=0)
    v1 = engine.ruleset.version
    assert engine.evaluate(base(cards_added_3h=3)).action == "DENY"

    def write(text: str) -> None:
        path.write_text(text)
        # Make sure the modification time moves even on coarse-grained filesystems.
        stamp = time.time() + 1
        os.utime(path, (stamp, stamp))

    write(RULES.replace("cards_added_3h >= 3", "cards_added_3h >= 5"))
    assert engine.evaluate(base(cards_added_3h=3)).action is None
    v2 = engine.ruleset.version
    assert v2 != v1

    write("rules: [{id: R001, condition: 'nope > 1'}]")  # broken
    assert engine.evaluate(base(cards_added_3h=5)).action == "DENY"  # still on v2
    assert engine.ruleset.version == v2
    assert engine.last_error and "invalid rules file" in engine.last_error

    write(RULES)
    assert engine.maybe_reload() or engine.ruleset.version == v1
    assert engine.ruleset.version == v1 and engine.last_error is None


def test_evaluate_ruleset_reports_recall_and_false_positives():
    rs = RuleSet.from_yaml(RULES)
    labelled = [
        (base(cards_added_3h=4), {"is_fraud": True, "pattern": "card_testing"}),
        (base(cards_added_3h=1), {"is_fraud": True, "pattern": "card_testing"}),
        (base(amount_zscore=4), {"is_fraud": False, "pattern": None}),
        (base(amount_zscore=0), {"is_fraud": False, "pattern": None}),
    ]
    report = evaluate_ruleset(rs, labelled)
    assert report["any_action"]["recall"] == 0.5
    assert report["any_action"]["false_positive_rate"] == 0.5
    assert report["deny_only"]["false_positive_rate"] == 0.0
    assert report["recall_by_pattern"] == {"card_testing": 0.5}
    assert report["rules"]["R100"]["false_positives"] == 1


def test_yaml_one_point_one_booleans_stay_strings():
    rs = RuleSet.from_yaml(rule_yaml(name="Off"))
    assert rs.rules[0].name == "Off"
    assert RuleSet.from_yaml(rule_yaml(name="NO")).rules[0].name == "NO"
