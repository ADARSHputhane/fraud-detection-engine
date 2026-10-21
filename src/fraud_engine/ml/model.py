"""Risk models: load, score one event with an explanation, and hot-swap versions.

A model version is a directory under MODEL_DIR:

    models/
      CURRENT                         <- name of the version in use
      xgb-20261021T101500Z/
        model.json                    <- XGBoost's own JSON format (no pickle)
        calibration.json              <- Platt scaling parameters
        metadata.json                 <- features, metrics, training data summary
      iforest-20261021T101200Z/
        iforest.joblib                <- pickle: only ever load models you trained yourself
        metadata.json
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

import joblib
import numpy as np
import xgboost as xgb
from sklearn.ensemble import IsolationForest

from ..features import NUMERIC_FEATURES
from .dataset import to_matrix, vectorize

log = logging.getLogger(__name__)

TOP_K = 5


@dataclass(frozen=True)
class MLScore:
    score: float  # 0-100
    model_version: str
    explanation: list[dict[str, Any]]  # top contributing features, strongest first


class RiskModel(Protocol):
    version: str
    metadata: dict

    def score_matrix(self, X: np.ndarray) -> np.ndarray: ...

    def score(self, features: Mapping[str, Any]) -> MLScore: ...


def _top(features: Mapping[str, Any], contributions: np.ndarray) -> list[dict[str, Any]]:
    order = np.argsort(-np.abs(contributions))[:TOP_K]
    return [
        {
            "feature": NUMERIC_FEATURES[i],
            "value": features.get(NUMERIC_FEATURES[i]),
            "contribution": round(float(contributions[i]), 4),
        }
        for i in order
        if contributions[i] != 0
    ]


class XGBRiskModel:
    """Gradient-boosted trees with Platt-scaled probabilities and exact TreeSHAP explanations."""

    kind = "xgboost"

    def __init__(self, booster: xgb.Booster, platt: tuple[float, float], metadata: dict) -> None:
        self.booster = booster
        # Single-row scoring: a thread pool costs more than it saves.
        self.booster.set_param({"nthread": 1})
        self.a, self.b = platt
        self.metadata = metadata
        self.version = metadata["version"]

    def margin(self, X: np.ndarray) -> np.ndarray:
        return self.booster.inplace_predict(X, predict_type="margin")

    def probability(self, margin: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-(self.a * margin + self.b)))

    def score_matrix(self, X: np.ndarray) -> np.ndarray:
        return np.round(100 * self.probability(self.margin(X)), 1)

    def score(self, features: Mapping[str, Any]) -> MLScore:
        x = np.asarray([vectorize(features)], dtype=np.float32)
        # SHAP values are in log-odds (margin) space; the last column is the bias term.
        contribs = self.booster.predict(xgb.DMatrix(x, missing=np.nan), pred_contribs=True)[0]
        margin = float(contribs.sum())
        score = round(float(100 * self.probability(np.asarray(margin))), 1)
        return MLScore(score, self.version, _top(features, contribs[:-1]))

    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.booster.save_model(path / "model.json")
        (path / "calibration.json").write_text(
            json.dumps({"method": "platt", "a": self.a, "b": self.b})
        )
        (path / "metadata.json").write_text(json.dumps(self.metadata, indent=2))

    @classmethod
    def load(cls, path: Path) -> XGBRiskModel:
        booster = xgb.Booster()
        booster.load_model(path / "model.json")
        cal = json.loads((path / "calibration.json").read_text())
        metadata = json.loads((path / "metadata.json").read_text())
        return cls(booster, (cal["a"], cal["b"]), metadata)


class IsolationForestModel:
    """Unsupervised cold-start fallback: scores how unusual an event is, no labels needed.

    Anomaly scores are mapped to 0-100 through quantiles of the training data: the median
    event scores 10, the top 3% reach the REVIEW threshold (40), the top 1% the DENY threshold
    (75). Explanations use occlusion: how much less anomalous the event becomes when one
    feature is reset to its typical (median) value.
    """

    kind = "isolation_forest"
    ANCHOR_QUANTILES = (0.5, 0.97, 0.99, 0.999)
    ANCHOR_SCORES = (10.0, 40.0, 75.0, 95.0)

    def __init__(
        self, forest: IsolationForest, medians: np.ndarray, anchors: np.ndarray, metadata: dict
    ) -> None:
        self.forest = forest
        self.medians = medians
        self.anchors = anchors  # anomaly scores at [min, quantiles..., max]
        self.metadata = metadata
        self.version = metadata["version"]

    @staticmethod
    def _impute(X: np.ndarray, medians: np.ndarray) -> np.ndarray:
        return np.where(np.isnan(X), medians, X)

    def anomaly(self, X: np.ndarray) -> np.ndarray:
        # Higher = more anomalous.
        return -self.forest.decision_function(self._impute(X, self.medians))

    @classmethod
    def anchors_for(cls, anomaly: np.ndarray) -> np.ndarray:
        points = [anomaly.min(), *np.quantile(anomaly, cls.ANCHOR_QUANTILES), anomaly.max()]
        # np.interp needs increasing x; nudge ties apart.
        return np.maximum.accumulate(np.asarray(points) + np.arange(len(points)) * 1e-9)

    def _to_score(self, anomaly: np.ndarray) -> np.ndarray:
        ys = (0.0, *self.ANCHOR_SCORES, 100.0)
        return np.round(np.interp(anomaly, self.anchors, ys), 1)

    def score_matrix(self, X: np.ndarray) -> np.ndarray:
        return self._to_score(self.anomaly(X))

    def score(self, features: Mapping[str, Any]) -> MLScore:
        x = to_matrix([features])
        variants = np.repeat(x, len(NUMERIC_FEATURES) + 1, axis=0)
        for i in range(len(NUMERIC_FEATURES)):
            variants[i + 1, i] = self.medians[i]
        anomaly = self.anomaly(variants)
        contribs = anomaly[0] - anomaly[1:]
        return MLScore(
            float(self._to_score(anomaly[:1])[0]), self.version, _top(features, contribs)
        )

    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"forest": self.forest, "medians": self.medians, "anchors": self.anchors},
            path / "iforest.joblib",
        )
        (path / "metadata.json").write_text(json.dumps(self.metadata, indent=2))

    @classmethod
    def load(cls, path: Path) -> IsolationForestModel:
        state = joblib.load(path / "iforest.joblib")
        metadata = json.loads((path / "metadata.json").read_text())
        return cls(state["forest"], state["medians"], state["anchors"], metadata)


def load_model(path: Path) -> RiskModel:
    kind = json.loads((path / "metadata.json").read_text())["kind"]
    if kind == XGBRiskModel.kind:
        return XGBRiskModel.load(path)
    if kind == IsolationForestModel.kind:
        return IsolationForestModel.load(path)
    raise ValueError(f"unknown model kind {kind!r} in {path}")


class ModelRegistry:
    """The model named in MODEL_DIR/CURRENT, reloaded when the pointer changes.

    Promoting a new version is an atomic rename of CURRENT, so a running worker or API picks
    it up on its next check without a restart.
    """

    POINTER = "CURRENT"
    _VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

    def __init__(self, model_dir: Path, check_interval: float = 10.0) -> None:
        self.model_dir = model_dir
        self.check_interval = check_interval
        self.current: RiskModel | None = None
        self.last_error: str | None = None
        self._failed: str | None = None
        self._checked = float("-inf")
        self.maybe_reload()

    def maybe_reload(self) -> bool:
        now = monotonic()
        if now - self._checked < self.check_interval:
            return False
        self._checked = now
        try:
            version = (self.model_dir / self.POINTER).read_text().strip()
        except OSError:
            return False  # nothing promoted yet: decisions run on rules alone
        if version in (getattr(self.current, "version", None), self._failed):
            return False
        try:
            if not self._VERSION.match(version):
                raise ValueError("invalid version name")  # never follow ../ out of model_dir
            model = load_model(self.model_dir / version)
        except Exception as exc:  # keep the previous model rather than scoring with nothing
            self._failed = version
            self.last_error = f"cannot load model {version!r}: {exc}"
            log.error(self.last_error)
            return False
        self.current, self.last_error, self._failed = model, None, None
        log.info("loaded model %s", version)
        return True

    def score(self, features: Mapping[str, Any]) -> MLScore | None:
        self.maybe_reload()
        return self.current.score(features) if self.current else None

    @classmethod
    def promote(cls, model_dir: Path, version: str) -> None:
        if not (model_dir / version / "metadata.json").exists():
            raise FileNotFoundError(f"no model version {version} in {model_dir}")
        tmp = model_dir / f".{cls.POINTER}.tmp"
        tmp.write_text(version + "\n")
        os.replace(tmp, model_dir / cls.POINTER)
