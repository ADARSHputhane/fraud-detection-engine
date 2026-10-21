"""Engine command line.

fraud-engine worker                  consume the stream and score events
fraud-engine bench-features          time feature computation against a real Redis
fraud-engine build-features          compute feature vectors for a file of events (offline)
fraud-engine evaluate-rules          recall / false-positive rate of a ruleset on labelled data
fraud-engine load-labels             import ground-truth labels into Postgres
fraud-engine train                   train a risk model (XGBoost, or Isolation Forest cold start)
fraud-engine retrain                 retrain on the latest labels, compare with the model in use
fraud-engine audit                   recompute stored decisions and check they match
fraud-engine latency-report          throughput and latency read back from the audit log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import statistics
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from prometheus_client import start_http_server
from redis.asyncio import Redis

from .config import Settings, get_settings
from .db import Database, LabelRecord
from .features import FeatureEngine
from .jsonl import read_jsonl, write_jsonl
from .ml import ModelRegistry
from .offline import build_features, join_labels
from .pipeline import Pipeline
from .rules import RulesEngine, evaluate_ruleset
from .stream import EventConsumer
from .worker import Worker


def build_pipeline(settings: Settings, redis: Redis) -> Pipeline:
    return Pipeline(
        FeatureEngine(redis, prefix=settings.feature_prefix),
        RulesEngine(
            Path(settings.rules_path) if settings.rules_path else None,
            settings.rules_reload_seconds,
        ),
        ModelRegistry(Path(settings.model_dir), settings.model_reload_seconds),
        review_at=settings.review_threshold,
        deny_above=settings.deny_threshold,
    )


def _percentiles(values: list[float]) -> dict[str, float]:
    qs = statistics.quantiles(values, n=100) if len(values) >= 2 else values * 99
    return {
        "p50": round(statistics.median(values), 3),
        "p95": round(qs[94], 3),
        "p99": round(qs[98], 3),
        "max": round(max(values), 3),
    }


async def _worker(args: argparse.Namespace) -> int:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    db = Database(settings.database_url)
    await db.create_tables()
    worker = Worker(
        db=db,
        consumer=EventConsumer(
            redis, settings.stream_name, settings.consumer_group, settings.consumer_name
        ),
        pipeline=build_pipeline(settings, redis),
        deadletter_stream=settings.deadletter_stream,
        batch_size=settings.worker_batch_size,
        block_ms=settings.worker_block_ms,
        latency_budget_ms=settings.latency_budget_ms,
    )
    if not args.drain:
        start_http_server(settings.metrics_port)
        logging.info("metrics on :%d/metrics", settings.metrics_port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        if args.drain:
            # Process everything already in the stream, then exit (benchmarks, demos, CI).
            await worker.consumer.ensure_group()
            while await worker.run_once():
                pass
        else:
            await worker.run(stop)
    finally:
        await db.dispose()
        await redis.aclose()
    logging.info("processed %d events, %d failed", worker.processed, worker.failed)
    return 0


async def _bench_features(args: argparse.Namespace) -> int:
    events = read_jsonl(Path(args.events))
    if args.limit:
        events = events[: args.limit]
    redis = Redis.from_url(args.redis_url or get_settings().redis_url)
    # A throwaway key prefix, so the benchmark never touches live feature state.
    engine = FeatureEngine(redis, prefix=f"bench:{uuid4().hex[:8]}")
    latencies: list[float] = []
    started = perf_counter()
    try:
        for event in events:
            t0 = perf_counter()
            await engine.compute(event)
            latencies.append((perf_counter() - t0) * 1000)
        elapsed = perf_counter() - started
    finally:
        await engine.reset()
        await redis.aclose()
    report = {
        "events": len(events),
        "events_per_second": round(len(events) / elapsed, 1),
        "feature_ms": _percentiles(latencies),
        "target_p95_ms": 10,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["feature_ms"]["p95"] < 10 else 1


async def _build_features(args: argparse.Namespace) -> int:
    events = read_jsonl(Path(args.events))
    redis = Redis.from_url(args.redis_url or get_settings().redis_url)
    started = perf_counter()
    try:
        rows = await build_features(events, redis)
    finally:
        await redis.aclose()
    write_jsonl(Path(args.out), rows)
    print(f"{len(rows):,} feature vectors -> {args.out} in {perf_counter() - started:.1f} s")
    return 0


async def _evaluate_rules(args: argparse.Namespace) -> int:
    ruleset = RulesEngine(Path(args.rules) if args.rules else None).ruleset
    labelled = join_labels(read_jsonl(Path(args.features)), read_jsonl(Path(args.labels)))
    print(json.dumps(evaluate_ruleset(ruleset, labelled), indent=2))
    return 0


async def _load_labels(args: argparse.Namespace) -> int:
    from uuid import UUID

    settings = get_settings()
    db = Database(settings.database_url)
    await db.create_tables()
    rows = [
        {
            "event_id": UUID(lbl["event_id"]),
            "is_fraud": bool(lbl["is_fraud"]),
            "source": "synthetic",
            "pattern": lbl.get("pattern"),
        }
        for lbl in read_jsonl(Path(args.file))
    ]
    async with db.sessionmaker() as session:
        for i in range(0, len(rows), 2_000):
            # Insert-or-ignore: re-running the import is harmless.
            await session.execute(db.insert_ignore(LabelRecord, rows[i : i + 2_000]))
        await session.commit()
    await db.dispose()
    print(f"loaded up to {len(rows):,} labels (existing labels kept)")
    return 0


async def _train(args: argparse.Namespace) -> int:
    from .ml import dataset, train

    settings = get_settings()
    if args.source == "db":
        db = Database(settings.database_url)
        ts = await dataset.from_db(db)
        await db.dispose()
    else:
        ts = dataset.from_files(Path(args.features), Path(args.labels))
    if len(ts) < 100:
        print(f"only {len(ts)} labelled events with features; need at least 100")
        return 1

    model_dir = Path(args.model_dir or settings.model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    ruleset = RulesEngine(Path(settings.rules_path) if settings.rules_path else None).ruleset
    if args.algorithm == "xgboost":
        version = train.new_version("xgb", model_dir)
        model, report = train.train_xgboost(ts, version, ruleset, seed=args.seed)
    else:
        version = train.new_version("iforest", model_dir)
        model, report = train.train_isolation_forest(ts, version, ruleset, seed=args.seed)
    path = train.save(model, report, model_dir)
    print(json.dumps({"version": version, **report}, indent=2))
    if args.promote:
        ModelRegistry.promote(model_dir, version)
        print(f"promoted {version} -> {model_dir / ModelRegistry.POINTER}")
    else:
        print(f"saved to {path}; promote with --promote or by writing {version} to CURRENT")
    return 0


async def _retrain(args: argparse.Namespace) -> int:
    from .ml import dataset, train
    from .ml.compare import compare_models, render_markdown

    settings = get_settings()
    db = Database(settings.database_url)
    try:
        ts = await dataset.from_db(db)
    finally:
        await db.dispose()
    if len(ts) < 100:
        print(f"only {len(ts)} labelled events with features; need at least 100")
        return 1
    model_dir = Path(settings.model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    current = ModelRegistry(model_dir).current
    ruleset = RulesEngine(Path(settings.rules_path) if settings.rules_path else None).ruleset
    candidate, report = train.train_xgboost(ts, train.new_version("xgb", model_dir), ruleset)
    train.save(candidate, report, model_dir)

    comparison = compare_models(current, candidate, ts)
    reports = Path(args.reports_dir)
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{candidate.version}.json").write_text(json.dumps(comparison, indent=2))
    markdown = render_markdown(comparison)
    (reports / f"{candidate.version}.md").write_text(markdown)
    print(markdown)
    if args.promote_if_better and comparison["candidate_wins"]:
        ModelRegistry.promote(model_dir, candidate.version)
        print(f"promoted {candidate.version}")
    return 0


async def _audit(args: argparse.Namespace) -> int:
    from .audit import verify_decisions

    settings = get_settings()
    db = Database(settings.database_url)
    try:
        report = await verify_decisions(db, Path(settings.model_dir), args.limit or None)
    finally:
        await db.dispose()
    print(json.dumps(report, indent=2))
    return 0 if report["mismatched"] == 0 else 1


async def _latency_report(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime, timedelta

    from .reporting import latency_report

    settings = get_settings()
    db = Database(settings.database_url)
    since = datetime.now(UTC) - timedelta(minutes=args.minutes) if args.minutes else None
    try:
        report = await latency_report(db, since)
    finally:
        await db.dispose()
    print(json.dumps(report, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="fraud-engine",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    w = sub.add_parser("worker", help="consume events:incoming and score each event")
    w.add_argument("--drain", action="store_true", help="exit once the stream is caught up")
    w.set_defaults(func=_worker)

    b = sub.add_parser("bench-features", help="time feature computation per event")
    b.add_argument("--events", default="data/events.jsonl")
    b.add_argument("--limit", type=int, default=10_000)
    b.add_argument("--redis-url", default=None, help="defaults to REDIS_URL")
    b.set_defaults(func=_bench_features)

    f = sub.add_parser("build-features", help="compute feature vectors for a file of events")
    f.add_argument("--events", default="data/events.jsonl")
    f.add_argument("--out", default="data/features.jsonl")
    f.add_argument("--redis-url", default=None, help="defaults to REDIS_URL")
    f.set_defaults(func=_build_features)

    e = sub.add_parser("evaluate-rules", help="score a ruleset against labelled events")
    e.add_argument("--features", default="data/features.jsonl")
    e.add_argument("--labels", default="data/labels.jsonl")
    e.add_argument("--rules", default=None, help="rules YAML (default: the packaged ruleset)")
    e.set_defaults(func=_evaluate_rules)

    lb = sub.add_parser("load-labels", help="import ground-truth labels into Postgres")
    lb.add_argument("--file", default="data/labels.jsonl")
    lb.set_defaults(func=_load_labels)

    t = sub.add_parser("train", help="train and evaluate a risk model")
    t.add_argument("--source", choices=["files", "db"], default="files")
    t.add_argument("--features", default="data/features.jsonl")
    t.add_argument("--labels", default="data/labels.jsonl")
    t.add_argument("--algorithm", choices=["xgboost", "isolation-forest"], default="xgboost")
    t.add_argument("--model-dir", default=None, help="defaults to MODEL_DIR")
    t.add_argument("--seed", type=int, default=42)
    t.add_argument("--promote", action="store_true", help="make this the model in use")
    t.set_defaults(func=_train)

    rt = sub.add_parser("retrain", help="retrain on the latest labels and compare")
    rt.add_argument("--reports-dir", default="reports")
    rt.add_argument("--promote-if-better", action="store_true")
    rt.set_defaults(func=_retrain)

    a = sub.add_parser("audit", help="recompute stored decisions and compare")
    a.add_argument("--limit", type=int, default=0, help="most recent N decisions (0 = all)")
    a.set_defaults(func=_audit)

    lr = sub.add_parser("latency-report", help="throughput and latency from the audit log")
    lr.add_argument("--minutes", type=float, default=0, help="only the last N minutes")
    lr.set_defaults(func=_latency_report)

    args = parser.parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
