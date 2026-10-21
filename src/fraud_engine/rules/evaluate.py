"""Measure a ruleset against labelled events: what it catches and what it costs."""

from __future__ import annotations

from collections import Counter

from .engine import RuleSet


def _rate(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


def evaluate_ruleset(ruleset: RuleSet, labelled: list[tuple[dict, dict]]) -> dict:
    fraud = legit = 0
    flagged = Counter()  # (is_fraud, action) -> count
    by_pattern_total: Counter[str] = Counter()
    by_pattern_caught: Counter[str] = Counter()
    rule_hits: Counter[str] = Counter()
    rule_fraud_hits: Counter[str] = Counter()

    for features, label in labelled:
        is_fraud = bool(label["is_fraud"])
        result = ruleset.evaluate(features)
        fraud += is_fraud
        legit += not is_fraud
        flagged[is_fraud, result.action] += 1
        if is_fraud:
            by_pattern_total[label["pattern"]] += 1
            if result.action:
                by_pattern_caught[label["pattern"]] += 1
        for hit in result.hits:
            rule_hits[hit.id] += 1
            rule_fraud_hits[hit.id] += is_fraud

    caught = flagged[True, "DENY"] + flagged[True, "REVIEW"]
    false_alarms = flagged[False, "DENY"] + flagged[False, "REVIEW"]
    return {
        "ruleset_version": ruleset.version,
        "events": fraud + legit,
        "fraud_events": fraud,
        "any_action": {
            "recall": _rate(caught, fraud),
            "false_positive_rate": _rate(false_alarms, legit),
            "precision": _rate(caught, caught + false_alarms),
        },
        "deny_only": {
            "recall": _rate(flagged[True, "DENY"], fraud),
            "false_positive_rate": _rate(flagged[False, "DENY"], legit),
        },
        "recall_by_pattern": {
            p: _rate(by_pattern_caught[p], n) for p, n in sorted(by_pattern_total.items())
        },
        "rules": {
            r.id: {
                "name": r.name,
                "hits": rule_hits[r.id],
                "precision": _rate(rule_fraud_hits[r.id], rule_hits[r.id]),
                "false_positives": rule_hits[r.id] - rule_fraud_hits[r.id],
            }
            for r in ruleset.rules
        },
    }
