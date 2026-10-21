"""Audit: prove that stored decisions can be reconstructed from the audit log alone.

For each decision, the stored feature vector is re-run through the exact ruleset version
(stored in `rulesets`) and model version (on disk) that made it, with the thresholds recorded
on the row, and the result is compared field by field with what was stored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from .db import Database, DecisionRecord, FeatureRecord, RulesetRecord
from .decision import decide
from .ml import ModelRegistry, RiskModel, load_model
from .rules import RuleSet

SCORE_TOLERANCE = 0.05


async def verify_decisions(
    db: Database, model_dir: Path, limit: int | None = None
) -> dict[str, Any]:
    query = (
        select(DecisionRecord, FeatureRecord.vector)
        .join(FeatureRecord, FeatureRecord.event_id == DecisionRecord.event_id)
        .order_by(DecisionRecord.decided_at.desc())
    )
    if limit:
        query = query.limit(limit)
    async with db.sessionmaker() as session:
        rows = (await session.execute(query)).all()
        sources = dict(
            (await session.execute(select(RulesetRecord.version, RulesetRecord.source))).all()
        )

    rulesets: dict[str, RuleSet] = {}
    models: dict[str, RiskModel | None] = {}
    checked = matched = 0
    unverifiable: dict[str, int] = {}
    mismatches: list[dict[str, Any]] = []

    for decision, vector in rows:
        if decision.ruleset_version not in sources:
            unverifiable["ruleset not stored"] = unverifiable.get("ruleset not stored", 0) + 1
            continue
        ruleset = rulesets.get(decision.ruleset_version)
        if ruleset is None:
            ruleset = rulesets[decision.ruleset_version] = RuleSet.from_yaml(
                sources[decision.ruleset_version]
            )
        model = None
        if decision.model_version:
            if decision.model_version not in models:
                try:
                    if not ModelRegistry.VERSION_PATTERN.match(decision.model_version):
                        raise ValueError("invalid version name")  # stay inside model_dir
                    models[decision.model_version] = load_model(model_dir / decision.model_version)
                except (OSError, ValueError):
                    models[decision.model_version] = None
            model = models[decision.model_version]
            if model is None:
                unverifiable["model not on disk"] = unverifiable.get("model not on disk", 0) + 1
                continue

        rules = ruleset.evaluate(vector)
        ml = model.score(vector) if model else None
        final, decided_by = decide(
            rules,
            ml,
            review_at=decision.thresholds["review_at"],
            deny_above=decision.thresholds["deny_above"],
        )
        expected = {
            "rules_fired": [h.id for h in rules.hits],
            "rule_score": rules.score,
            "final_decision": final,
            "decided_by": decided_by,
        }
        stored = {
            "rules_fired": [h["id"] for h in decision.rules_fired],
            "rule_score": decision.rule_score,
            "final_decision": decision.final_decision,
            "decided_by": decision.decided_by,
        }
        diffs = {k: (stored[k], expected[k]) for k in expected if stored[k] != expected[k]}
        if ml is not None and abs(ml.score - (decision.ml_score or 0)) > SCORE_TOLERANCE:
            diffs["ml_score"] = (decision.ml_score, ml.score)
        checked += 1
        if diffs:
            if len(mismatches) < 20:
                mismatches.append(
                    {"event_id": str(decision.event_id), "stored_vs_recomputed": diffs}
                )
        else:
            matched += 1

    return {
        "decisions": len(rows),
        "verified": checked,
        "matched": matched,
        "mismatched": checked - matched,
        "unverifiable": unverifiable,
        "rulesets": sorted(rulesets),
        "models": sorted(v for v, m in models.items() if m is not None),
        "mismatch_examples": mismatches,
    }
