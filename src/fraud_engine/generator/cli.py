"""Command line: generate a dataset, or replay one against the running API.

fraud-gen generate --users 500 --days 30 --out data/
fraud-gen replay --file data/events.jsonl --url http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

from ..jsonl import read_jsonl, write_jsonl
from .simulate import generate_dataset


def cmd_generate(args: argparse.Namespace) -> int:
    ds = generate_dataset(
        users=args.users, days=args.days, fraud_rate=args.fraud_rate, seed=args.seed
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(out / "events.jsonl", ds.events)
    write_jsonl(out / "labels.jsonl", ds.labels)
    (out / "stats.json").write_text(json.dumps(ds.stats, indent=2))
    print(json.dumps(ds.stats, indent=2))
    print(f"\nWrote {len(ds.events):,} events to {out / 'events.jsonl'}")
    print(f"Ground-truth labels in {out / 'labels.jsonl'} (never sent to the API)")
    return 0


def _summarise_errors(resp: httpx.Response) -> str:
    try:
        return json.dumps(resp.json()["detail"])[:500]
    except Exception:
        return resp.text[:500]


def cmd_replay(args: argparse.Namespace) -> int:
    events = read_jsonl(Path(args.file))[args.offset :]
    if args.limit:
        events = events[: args.limit]
    accepted = duplicates = failed = 0
    latencies: list[float] = []
    started = time.perf_counter()

    with httpx.Client(base_url=args.url, timeout=30) as client:
        if args.single or args.rate:
            # One request per event: slower, but gives per-request latency numbers. With
            # --rate, requests are paced to a steady arrival rate, like live traffic.
            interval = 1 / args.rate if args.rate else 0.0
            for i, event in enumerate(events):
                if interval:
                    delay = started + i * interval - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                t0 = time.perf_counter()
                resp = client.post(f"/events/{event['event_type']}", json=event)
                latencies.append((time.perf_counter() - t0) * 1000)
                if resp.status_code == 202:
                    accepted += 1
                elif resp.status_code == 409:
                    duplicates += 1
                else:
                    failed += 1
                    if failed <= 3:
                        msg = f"HTTP {resp.status_code}: {_summarise_errors(resp)}"
                        print(msg, file=sys.stderr)
        else:
            # Sequential batches keep events in time order for downstream consumers.
            for i in range(0, len(events), args.batch_size):
                chunk = events[i : i + args.batch_size]
                t0 = time.perf_counter()
                resp = client.post("/events/batch", json={"events": chunk})
                latencies.append((time.perf_counter() - t0) * 1000)
                if resp.status_code == 202:
                    body = resp.json()
                    accepted += body["accepted"]
                    duplicates += len(body["duplicates"])
                else:
                    failed += len(chunk)
                    print(f"HTTP {resp.status_code}: {_summarise_errors(resp)}", file=sys.stderr)

    elapsed = time.perf_counter() - started
    report = {
        "sent": len(events),
        "accepted": accepted,
        "duplicates": duplicates,
        "failed": failed,
        "seconds": round(elapsed, 2),
        "events_per_second": round(len(events) / elapsed, 1) if elapsed else None,
        "mode": (
            f"paced({args.rate}/s)"
            if args.rate
            else "single"
            if args.single
            else f"batch({args.batch_size})"
        ),
    }
    if latencies:
        qs = statistics.quantiles(latencies, n=100) if len(latencies) >= 2 else latencies * 99
        report["request_ms_p50"] = round(statistics.median(latencies), 2)
        report["request_ms_p95"] = round(qs[94], 2)
    print(json.dumps(report, indent=2))
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fraud-gen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="create a synthetic labelled dataset")
    gen.add_argument("--users", type=int, default=500)
    gen.add_argument("--days", type=int, default=30)
    gen.add_argument("--fraud-rate", type=float, default=0.05)
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--out", default="data")
    gen.set_defaults(func=cmd_generate)

    rep = sub.add_parser("replay", help="send a generated dataset to the ingest API")
    rep.add_argument("--file", default="data/events.jsonl")
    rep.add_argument("--url", default="http://localhost:8000")
    rep.add_argument("--batch-size", type=int, default=500)
    rep.add_argument("--single", action="store_true", help="one request per event")
    rep.add_argument("--limit", type=int, default=0, help="only send the first N events")
    rep.add_argument("--offset", type=int, default=0, help="skip the first N events")
    rep.add_argument(
        "--rate", type=float, default=0, help="events per second, one request each (paced)"
    )
    rep.set_defaults(func=cmd_replay)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
