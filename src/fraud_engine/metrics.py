"""Prometheus metrics.

Defined once at import time, as the client library expects; the API serves them on /metrics
and the worker on its own port (METRICS_PORT, default 9100).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_FAST = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
_SLOW = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

INGESTED = Counter(
    "fraud_ingest_events_total",
    "Events received by the ingest API",
    ["event_type", "outcome"],  # accepted | duplicate | unavailable
)
PROCESSED = Counter("fraud_events_processed_total", "Events scored by the worker", ["event_type"])
FAILED = Counter("fraud_worker_failures_total", "Events moved to the dead-letter stream")
DECISIONS = Counter(
    "fraud_decisions_total",
    "Final decisions",
    ["decision", "source"],  # source: rule | model | default
)
RULE_HITS = Counter(
    "fraud_rule_hits_total", "Rule hits (several rules can fire per event)", ["rule"]
)
STAGE_SECONDS = Histogram(
    "fraud_stage_seconds", "Time spent in each pipeline stage", ["stage"], buckets=_FAST
)
END_TO_END_SECONDS = Histogram(
    "fraud_end_to_end_seconds",
    "From publish to the stream until the decision is made (queue wait + processing)",
    buckets=_SLOW,
)
OVER_BUDGET = Counter(
    "fraud_latency_budget_exceeded_total", "Decisions slower end to end than the latency budget"
)
PERSIST_SECONDS = Histogram(
    "fraud_persist_seconds", "Writing one batch of features and decisions", buckets=_SLOW
)
MODEL_IN_USE = Gauge("fraud_model_in_use", "1 for the model version currently scoring", ["version"])
