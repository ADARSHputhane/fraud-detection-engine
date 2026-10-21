"""Model training and evaluation.

Rows are split by time: the oldest 60% train the model, the next 20% are held out for early
stopping and probability calibration, and the newest 20% are the test set every reported
number comes from.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import numpy as np
import xgboost as xgb
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from ..decision import DENY_THRESHOLD, REVIEW_THRESHOLD, decide, score_band
from ..features import FEATURE_VERSION, NUMERIC_FEATURES
from ..rules import RuleSet
from .dataset import TrainingSet
from .model import IsolationForestModel, MLScore, RiskModel, XGBRiskModel

XGB_PARAMS = {
    "objective": "binary:logistic",
    "eval_metric": "logloss",
    "tree_method": "hist",
    "max_depth": 5,
    "eta": 0.05,
    "subsample": 0.9,
    "colsample_bytree": 0.8,
    "min_child_weight": 2,
}
MAX_ROUNDS = 600
EARLY_STOPPING = 50


def new_version(prefix: str, model_dir: Path) -> str:
    base = f"{prefix}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    version, n = base, 1
    while (model_dir / version).exists():
        n += 1
        version = f"{base}-{n}"
    return version


def _rate(n: float, d: float) -> float:
    return round(float(n) / float(d), 4) if d else 0.0


def _confusion(y: np.ndarray, flagged: np.ndarray) -> dict:
    tp = int(np.sum(flagged & (y == 1)))
    fp = int(np.sum(flagged & (y == 0)))
    return {
        "precision": _rate(tp, tp + fp),
        "recall": _rate(tp, int(np.sum(y == 1))),
        "false_positive_rate": _rate(fp, int(np.sum(y == 0))),
        "flagged": int(np.sum(flagged)),
    }


def score_metrics(y: np.ndarray, scores: np.ndarray) -> dict:
    out = {"events": len(y), "fraud_events": int(y.sum())}
    if 0 < y.sum() < len(y):
        out["auc_roc"] = round(float(roc_auc_score(y, scores)), 4)
        out["pr_auc"] = round(float(average_precision_score(y, scores)), 4)
    out["brier"] = round(float(brier_score_loss(y, scores / 100)), 5)
    out[f"score_ge_{REVIEW_THRESHOLD:g}"] = _confusion(y, scores >= REVIEW_THRESHOLD)
    out[f"score_gt_{DENY_THRESHOLD:g}"] = _confusion(y, scores > DENY_THRESHOLD)
    return out


def compare_with_rules(
    vectors: list[dict], y: np.ndarray, scores: np.ndarray, version: str, ruleset: RuleSet
) -> dict:
    """Rules alone, model alone and the combined decision, on the same test events."""
    rules_flag, model_flag, combined_flag, combined_deny = [], [], [], []
    for features, score in zip(vectors, scores, strict=True):
        rules = ruleset.evaluate(features)
        ml = MLScore(float(score), version, [])
        decision, _ = decide(rules, ml)
        rules_flag.append(rules.action is not None)
        model_flag.append(score_band(float(score)) != "ALLOW")
        combined_flag.append(decision != "ALLOW")
        combined_deny.append(decision == "DENY")
    return {
        "rules_only": _confusion(y, np.asarray(rules_flag)),
        "model_only": _confusion(y, np.asarray(model_flag)),
        "combined_any_action": _confusion(y, np.asarray(combined_flag)),
        "combined_deny": _confusion(y, np.asarray(combined_deny)),
    }


def recall_by_pattern(y: np.ndarray, flagged: np.ndarray, patterns: list[str | None]) -> dict:
    total, caught = Counter(), Counter()
    for is_fraud, hit, pattern in zip(y, flagged, patterns, strict=True):
        if is_fraud:
            total[pattern] += 1
            caught[pattern] += bool(hit)
    return {p: _rate(caught[p], n) for p, n in sorted(total.items())}


def scoring_latency(model: RiskModel, vectors: list[dict], n: int = 2_000) -> dict:
    """Single-event scoring including the explanation, the way the worker calls it."""
    sample = vectors[:n]
    for v in sample[:50]:
        model.score(v)  # warm-up
    times = []
    for v in sample:
        t0 = perf_counter()
        model.score(v)
        times.append((perf_counter() - t0) * 1000)
    qs = statistics.quantiles(times, n=100)
    return {
        "p50": round(statistics.median(times), 3),
        "p95": round(qs[94], 3),
        "p99": round(qs[98], 3),
    }


def _metadata(version: str, kind: str, ts: TrainingSet, splits: tuple[slice, ...]) -> dict:
    tr, ca, te = splits
    return {
        "version": version,
        "kind": kind,
        "created_at": datetime.now(UTC).isoformat(),
        "feature_version": FEATURE_VERSION,
        "features": list(NUMERIC_FEATURES),
        "thresholds": {"review_at": REVIEW_THRESHOLD, "deny_above": DENY_THRESHOLD},
        "training": {
            "events": len(ts),
            "label_sources": dict(ts.label_sources),
            "train": [tr.start, tr.stop],
            "calibration": [ca.start, ca.stop],
            "test": [te.start, te.stop],
            "fraud_in_train": int(ts.y[tr].sum()),
            # Lets a later comparison check the old model never trained on its test events.
            "last_train_event_id": ts.event_ids[tr.stop - 1] if tr.stop else None,
        },
    }


def _evaluate(
    model: RiskModel, ts: TrainingSet, test: slice, ruleset: RuleSet, latency_sample: int
) -> dict:
    scores = model.score_matrix(ts.X[test])
    y, vectors = ts.y[test], ts.vectors[test]
    return {
        "test": score_metrics(y, scores),
        "test_recall_by_pattern": recall_by_pattern(
            y, scores >= REVIEW_THRESHOLD, ts.patterns[test]
        ),
        "vs_rules": compare_with_rules(vectors, y, scores, model.version, ruleset),
        "scoring_ms": scoring_latency(model, vectors, latency_sample),
    }


def train_xgboost(
    ts: TrainingSet, version: str, ruleset: RuleSet, seed: int = 42, latency_sample: int = 2_000
) -> tuple[XGBRiskModel, dict]:
    tr, ca, te = ts.split()
    if ts.y[tr].sum() == 0 or ts.y[ca].sum() == 0:
        raise ValueError("need fraud labels in both the training and calibration periods")
    pos = float(ts.y[tr].sum())
    scale_pos_weight = (len(ts.y[tr]) - pos) / pos
    dtrain = xgb.DMatrix(ts.X[tr], label=ts.y[tr], missing=np.nan)
    dcal = xgb.DMatrix(ts.X[ca], label=ts.y[ca], missing=np.nan)
    booster = xgb.train(
        {**XGB_PARAMS, "scale_pos_weight": scale_pos_weight, "seed": seed},
        dtrain,
        num_boost_round=MAX_ROUNDS,
        evals=[(dcal, "calibration")],
        early_stopping_rounds=EARLY_STOPPING,
        verbose_eval=False,
    )
    booster = booster[: booster.best_iteration + 1]

    # Platt scaling on the held-out period: scale_pos_weight inflates raw probabilities on
    # purpose, so they are re-mapped to match observed fraud rates.
    margin = booster.inplace_predict(ts.X[ca], predict_type="margin").reshape(-1, 1)
    platt = LogisticRegression(C=1e6).fit(margin, ts.y[ca])
    a, b = float(platt.coef_[0][0]), float(platt.intercept_[0])

    metadata = _metadata(version, XGBRiskModel.kind, ts, (tr, ca, te))
    model = XGBRiskModel(booster, (a, b), metadata)
    report = _evaluate(model, ts, te, ruleset, latency_sample)
    gain = booster.get_score(importance_type="gain")
    report["top_features_by_gain"] = {
        NUMERIC_FEATURES[int(k[1:])]: round(v, 2)
        for k, v in sorted(gain.items(), key=lambda kv: -kv[1])[:10]
    }
    report["scale_pos_weight"] = round(scale_pos_weight, 2)
    report["trees"] = booster.num_boosted_rounds()
    metadata["metrics"] = report["test"]
    return model, report


def train_isolation_forest(
    ts: TrainingSet, version: str, ruleset: RuleSet, seed: int = 42, latency_sample: int = 2_000
) -> tuple[IsolationForestModel, dict]:
    """Labels are only used to *report* how well it does; the forest never sees them."""
    tr, ca, te = ts.split()
    with np.errstate(all="ignore"):
        medians = np.nan_to_num(np.nanmedian(ts.X[tr], axis=0))
    forest = IsolationForest(n_estimators=200, max_samples=256, random_state=seed)
    forest.fit(IsolationForestModel._impute(ts.X[tr], medians))
    anomaly = -forest.decision_function(IsolationForestModel._impute(ts.X[tr], medians))
    metadata = _metadata(version, IsolationForestModel.kind, ts, (tr, ca, te))
    model = IsolationForestModel(
        forest, medians, IsolationForestModel.anchors_for(anomaly), metadata
    )
    report = _evaluate(model, ts, te, ruleset, latency_sample)
    metadata["metrics"] = report["test"]
    return model, report


def save(model: XGBRiskModel | IsolationForestModel, report: dict, model_dir: Path) -> Path:
    path = model_dir / model.version
    model.save(path)
    (path / "report.json").write_text(json.dumps(report, indent=2))
    return path
