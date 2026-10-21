import asyncio
import json
import shutil
from datetime import UTC, datetime

import numpy as np
import pytest
from fakeredis.aioredis import FakeRedis

from fraud_engine.decision import decide, score_band
from fraud_engine.features import FEATURE_NAMES, NUMERIC_FEATURES
from fraud_engine.generator import generate_dataset
from fraud_engine.ml import IsolationForestModel, MLScore, ModelRegistry, XGBRiskModel, load_model
from fraud_engine.ml.dataset import TrainingSet, to_matrix, vectorize
from fraud_engine.ml.train import save, train_isolation_forest, train_xgboost
from fraud_engine.offline import build_features
from fraud_engine.rules import RuleResult, RuleSet
from fraud_engine.rules.engine import RuleHit, default_rules_text

END = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def training_set() -> TrainingSet:
    ds = generate_dataset(users=150, days=20, seed=21, end=END)

    async def features():
        client = FakeRedis()
        rows = await build_features(ds.events, client)
        await client.aclose()
        return rows

    rows = asyncio.run(features())
    labels = {lbl["event_id"]: lbl for lbl in ds.labels}
    return TrainingSet(
        vectors=[r["features"] for r in rows],
        y=np.asarray([int(labels[r["event_id"]]["is_fraud"]) for r in rows], dtype=np.int8),
        event_ids=[r["event_id"] for r in rows],
        patterns=[labels[r["event_id"]]["pattern"] for r in rows],
    )


@pytest.fixture(scope="module")
def ruleset() -> RuleSet:
    return RuleSet.from_yaml(default_rules_text())


@pytest.fixture(scope="module")
def xgb_model(training_set, ruleset):
    return train_xgboost(training_set, "xgb-test", ruleset, latency_sample=300)


def test_vectorize_booleans_and_missing_values():
    row = vectorize({"is_payment": True, "amount": 12.5, "amount_zscore": None})
    assert len(row) == len(NUMERIC_FEATURES)
    assert row[NUMERIC_FEATURES.index("is_payment")] == 1.0
    assert row[NUMERIC_FEATURES.index("amount")] == 12.5
    assert np.isnan(row[NUMERIC_FEATURES.index("amount_zscore")])
    assert "merchant_category" not in NUMERIC_FEATURES  # text never reaches the model
    assert to_matrix([]).shape == (0, len(NUMERIC_FEATURES))


def test_time_split_keeps_order(training_set):
    tr, ca, te = training_set.split()
    assert tr.stop == ca.start and ca.stop == te.start and te.stop == len(training_set)


def test_xgboost_is_accurate_calibrated_and_fast(xgb_model):
    model, report = xgb_model
    assert report["test"]["auc_roc"] >= 0.9
    assert 0 <= report["test"]["brier"] < 0.05
    assert report["scoring_ms"]["p95"] < 30
    assert set(report["vs_rules"]) == {
        "rules_only",
        "model_only",
        "combined_any_action",
        "combined_deny",
    }
    assert model.metadata["features"] == list(NUMERIC_FEATURES)


def test_score_explains_with_exact_shap_values(xgb_model, training_set):
    model, _ = xgb_model
    fraud_row = training_set.vectors[int(np.argmax(training_set.y))]
    result = model.score(fraud_row)
    assert 0 <= result.score <= 100
    assert 1 <= len(result.explanation) <= 5
    magnitudes = [abs(c["contribution"]) for c in result.explanation]
    assert magnitudes == sorted(magnitudes, reverse=True)
    assert all(c["feature"] in NUMERIC_FEATURES for c in result.explanation)
    # TreeSHAP is additive: contributions plus the bias equal the raw model output.
    import xgboost as xgb

    x = to_matrix([fraud_row])
    contribs = model.booster.predict(xgb.DMatrix(x, missing=np.nan), pred_contribs=True)[0]
    assert contribs.sum() == pytest.approx(float(model.margin(x)[0]), abs=1e-3)
    # The batch path and the single-event path agree.
    assert model.score_matrix(x)[0] == pytest.approx(result.score, abs=0.11)


def test_model_save_load_roundtrip(xgb_model, training_set, tmp_path):
    model, report = xgb_model
    path = save(model, report, tmp_path)
    assert {p.name for p in path.iterdir()} == {
        "model.json",
        "calibration.json",
        "metadata.json",
        "report.json",
    }
    loaded = load_model(path)
    assert isinstance(loaded, XGBRiskModel)
    X = training_set.X[:200]
    assert np.array_equal(loaded.score_matrix(X), model.score_matrix(X))


def test_isolation_forest_fallback(training_set, ruleset, tmp_path):
    model, report = train_isolation_forest(
        training_set, "iforest-test", ruleset, latency_sample=100
    )
    scores = model.score_matrix(training_set.X)
    assert scores.min() >= 0 and scores.max() <= 100
    # By construction about 1% of training traffic lands above the DENY threshold.
    assert 0.002 < np.mean(scores > 75) < 0.05
    assert report["test"]["auc_roc"] > 0.7  # never saw a label
    result = model.score(training_set.vectors[0])
    assert result.model_version == "iforest-test" and len(result.explanation) <= 5
    loaded = load_model(save(model, report, tmp_path))
    assert isinstance(loaded, IsolationForestModel)
    assert np.array_equal(loaded.score_matrix(training_set.X[:50]), scores[:50])


def test_registry_loads_promotes_and_survives_bad_pointers(xgb_model, tmp_path):
    model, report = xgb_model
    registry = ModelRegistry(tmp_path, check_interval=0)
    assert registry.current is None and registry.score({}) is None  # nothing promoted yet

    save(model, report, tmp_path)
    ModelRegistry.promote(tmp_path, "xgb-test")
    assert registry.maybe_reload() and registry.current.version == "xgb-test"

    shutil.copytree(tmp_path / "xgb-test", tmp_path / "xgb-test2")
    meta = json.loads((tmp_path / "xgb-test2" / "metadata.json").read_text())
    (tmp_path / "xgb-test2" / "metadata.json").write_text(
        json.dumps({**meta, "version": "xgb-test2"})
    )
    ModelRegistry.promote(tmp_path, "xgb-test2")
    assert registry.maybe_reload() and registry.current.version == "xgb-test2"

    (tmp_path / "CURRENT").write_text("../../etc\n")
    assert not registry.maybe_reload()
    assert registry.current.version == "xgb-test2" and "invalid version" in registry.last_error
    with pytest.raises(FileNotFoundError):
        ModelRegistry.promote(tmp_path, "missing")


def _rules(*actions: str) -> RuleResult:
    hits = [RuleHit(f"R00{i}", "r", a, 50, "why") for i, a in enumerate(actions, 1)]
    return RuleResult(hits=hits, ruleset_version="v")


def _ml(score: float) -> MLScore:
    return MLScore(score, "m", [])


@pytest.mark.parametrize(
    ("rules", "score", "expected"),
    [
        (_rules(), None, ("ALLOW", "default")),
        (_rules(), 39.9, ("ALLOW", "default")),
        (_rules(), 40, ("REVIEW", "model")),
        (_rules(), 75, ("REVIEW", "model")),
        (_rules(), 75.1, ("DENY", "model")),
        (_rules("DENY"), 1, ("DENY", "rule:R001")),  # a rule DENY always wins
        (_rules("REVIEW"), 1, ("REVIEW", "rule:R001")),  # a low score never cancels a review
        (_rules("REVIEW"), 90, ("DENY", "model")),  # the model can escalate
        (_rules("REVIEW", "DENY"), 99, ("DENY", "rule:R002")),
    ],
)
def test_decision_hierarchy(rules, score, expected):
    ml = _ml(score) if score is not None else None
    assert decide(rules, ml) == expected


def test_score_band_custom_thresholds():
    assert score_band(50, review_at=60, deny_above=80) == "ALLOW"
    assert score_band(81, review_at=60, deny_above=80) == "DENY"


async def test_score_endpoint(client, app, xgb_model, training_set):
    body = {"features": {k: training_set.vectors[0].get(k) for k in FEATURE_NAMES}}
    assert (await client.post("/score", json=body)).status_code == 503  # no model yet
    assert (await client.get("/models/current")).status_code == 404

    model, report = xgb_model
    model_dir = app.state.models.model_dir
    save(model, report, model_dir)
    ModelRegistry.promote(model_dir, "xgb-test")
    app.state.models.check_interval = 0

    resp = await client.post("/score", json=body)
    assert resp.status_code == 200
    out = resp.json()
    assert out["model_version"] == "xgb-test"
    assert out["band"] == score_band(out["score"])
    assert len(out["explanation"]) <= 5
    assert (await client.get("/models/current")).json()["version"] == "xgb-test"

    bad = await client.post("/score", json={"features": {"not_a_feature": 1}})
    assert bad.status_code == 422


async def test_worker_records_ml_score_and_shap(client, app, redis, xgb_model, payment):
    from fraud_engine.features import FeatureEngine
    from fraud_engine.pipeline import Pipeline
    from fraud_engine.stream import EventConsumer
    from fraud_engine.worker import Worker

    from .conftest import STREAM

    model, report = xgb_model
    model_dir = app.state.models.model_dir
    save(model, report, model_dir)
    ModelRegistry.promote(model_dir, "xgb-test")
    worker = Worker(
        db=app.state.db,
        consumer=EventConsumer(redis, STREAM, "g", "w"),
        pipeline=Pipeline(FeatureEngine(redis), models=ModelRegistry(model_dir, 0)),
        deadletter_stream="dlq",
    )
    event = payment()
    await client.post("/events/payment", json=event)
    await worker.consumer.ensure_group()
    await worker.run_once()
    decision = (await client.get(f"/decisions/{event['event_id']}")).json()
    assert decision["model_version"] == "xgb-test"
    assert 0 <= decision["ml_score"] <= 100
    assert decision["shap_top5"] and "contribution" in decision["shap_top5"][0]
    assert "ml" in decision["stage_ms"]


async def test_training_set_from_db_joins_and_orders_by_event_time(app):
    from datetime import timedelta
    from uuid import uuid4

    from fraud_engine.db import EventRecord, FeatureRecord, LabelRecord
    from fraud_engine.ml.dataset import from_db

    db = app.state.db
    base = datetime(2026, 9, 1, tzinfo=UTC)
    ids = [uuid4() for _ in range(4)]
    async with db.sessionmaker() as session:
        # Inserted out of order; one event has no label and must be left out.
        for i, minutes in ((0, 30), (1, 10), (2, 20), (3, 0)):
            session.add(
                EventRecord(
                    event_id=ids[i],
                    event_type="payment",
                    user_id="u",
                    card_id="tok_x",
                    device_id="d",
                    occurred_at=base + timedelta(minutes=minutes),
                    payload={},
                )
            )
            session.add(
                FeatureRecord(event_id=ids[i], feature_version=1, vector={"amount": float(i)})
            )
        for i, source in ((0, "synthetic"), (1, "analyst"), (2, "synthetic")):
            session.add(LabelRecord(event_id=ids[i], is_fraud=i == 1, source=source, pattern=None))
        await session.commit()

    ts = await from_db(db)
    assert ts.event_ids == [str(ids[1]), str(ids[2]), str(ids[0])]
    assert ts.y.tolist() == [1, 0, 0]
    assert ts.X[:, NUMERIC_FEATURES.index("amount")].tolist() == [1.0, 2.0, 0.0]
    assert ts.label_sources == {"synthetic": 2, "analyst": 1}
