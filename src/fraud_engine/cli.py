"""Engine command line.

fraud-engine worker                  consume the stream and score events
fraud-engine bench-features          time feature computation against a real Redis
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

from redis.asyncio import Redis

from .config import get_settings
from .db import Database
from .features import FeatureEngine
from .jsonl import read_jsonl
from .pipeline import Pipeline
from .stream import EventConsumer
from .worker import Worker


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
        pipeline=Pipeline(FeatureEngine(redis, prefix=settings.feature_prefix)),
        deadletter_stream=settings.deadletter_stream,
        batch_size=settings.worker_batch_size,
        block_ms=settings.worker_block_ms,
    )
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

    args = parser.parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
