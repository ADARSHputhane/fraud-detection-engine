"""Training data: stored feature vectors joined with labels, as a numeric matrix.

Rows are kept in event-time order so the train / calibration / test split is by time: the
model is always evaluated on events that happened after everything it learned from, the way
it will be used in production.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from sqlalchemy import select

from ..db import Database, EventRecord, FeatureRecord, LabelRecord
from ..features import NUMERIC_FEATURES
from ..jsonl import read_jsonl


def vectorize(features: Mapping) -> list[float]:
    """One model row: booleans as 0/1, missing values as NaN (XGBoost handles them natively)."""
    return [np.nan if (v := features.get(name)) is None else float(v) for name in NUMERIC_FEATURES]


def to_matrix(vectors: Sequence[Mapping]) -> np.ndarray:
    return np.asarray([vectorize(v) for v in vectors], dtype=np.float32).reshape(
        len(vectors), len(NUMERIC_FEATURES)
    )


@dataclass
class TrainingSet:
    vectors: list[dict]
    y: np.ndarray
    event_ids: list[str]
    patterns: list[str | None]
    label_sources: Counter = field(default_factory=Counter)
    X: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self.X = to_matrix(self.vectors)

    def __len__(self) -> int:
        return len(self.vectors)

    def split(self, train: float = 0.6, calibration: float = 0.2) -> tuple[slice, slice, slice]:
        """Contiguous time slices: oldest for training, then calibration, newest for test."""
        n = len(self)
        a, b = int(n * train), int(n * (train + calibration))
        return slice(0, a), slice(a, b), slice(b, n)


def from_files(features_path: Path, labels_path: Path) -> TrainingSet:
    """Feature rows from `fraud-engine build-features` (already in event-time order)."""
    labels = {lbl["event_id"]: lbl for lbl in read_jsonl(labels_path)}
    vectors, ys, ids, patterns = [], [], [], []
    for row in read_jsonl(features_path):
        lbl = labels.get(row["event_id"])
        if lbl is None:
            continue
        vectors.append(row["features"])
        ys.append(int(lbl["is_fraud"]))
        ids.append(row["event_id"])
        patterns.append(lbl.get("pattern"))
    return TrainingSet(
        vectors, np.asarray(ys, dtype=np.int8), ids, patterns, Counter(synthetic=len(ys))
    )


async def from_db(db: Database) -> TrainingSet:
    """Every event with both a stored feature vector and a label, in event-time order."""
    query = (
        select(
            FeatureRecord.vector,
            LabelRecord.is_fraud,
            LabelRecord.pattern,
            LabelRecord.source,
            FeatureRecord.event_id,
        )
        .join(LabelRecord, LabelRecord.event_id == FeatureRecord.event_id)
        .join(EventRecord, EventRecord.event_id == FeatureRecord.event_id)
        .order_by(EventRecord.occurred_at, EventRecord.event_id)
    )
    async with db.sessionmaker() as session:
        rows = (await session.execute(query)).all()
    return TrainingSet(
        vectors=[r.vector for r in rows],
        y=np.asarray([int(r.is_fraud) for r in rows], dtype=np.int8),
        event_ids=[str(r.event_id) for r in rows],
        patterns=[r.pattern for r in rows],
        label_sources=Counter(r.source for r in rows),
    )
