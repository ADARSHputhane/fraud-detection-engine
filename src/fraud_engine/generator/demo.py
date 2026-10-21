"""Live demo: give a handful of customers three weeks of normal history, then replay an
attack against the running engine one event at a time and print each decision as it lands,
next to the ground truth.

fraud-gen demo --scenario impossible_travel
fraud-gen demo --scenario all --url http://localhost:8000
"""

from __future__ import annotations

import random
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx

from .patterns import PATTERNS, Context
from .personas import Persona, make_persona
from .simulate import _normal_traffic

HISTORY_DAYS = 21
# Attacks happen in the last two days; history ends before that, so events arrive in order.
ATTACK_WINDOW = timedelta(days=2)

_DECISION_COLOR = {"ALLOW": "\033[32m", "REVIEW": "\033[33m", "DENY": "\033[31m"}
RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"


def _ts(event: dict) -> datetime:
    return datetime.fromisoformat(event["timestamp"])


def build_world(rng: random.Random, users: int, now: datetime) -> tuple[list[Persona], list[dict]]:
    """Personas plus their normal traffic, ending where the attack window begins."""
    end = now - ATTACK_WINDOW
    start = end - timedelta(days=HISTORY_DAYS)
    personas = [make_persona(rng, start) for _ in range(users)]
    events = [e for p in personas for e in _normal_traffic(rng, p, start, end, HISTORY_DAYS)]
    events.sort(key=_ts)
    return personas, events


def describe(event: dict) -> str:
    where = (event.get("geo") or {}).get("city") or "?"
    if event["event_type"] == "payment":
        what = f"{event['currency']} {float(event['amount']):>10,.2f} {event['merchant_category']}"
        if event.get("status") == "declined":
            what += f" (declined: {event.get('decline_reason')})"
        return f"{what}, {where}"
    return f"card added: {event['card_type']}, issued in {event['issuing_country']}, {where}"


def why(decision: dict) -> str:
    rules = decision.get("rules_fired") or []
    if rules:
        top = max(rules, key=lambda h: (h["action"] == "DENY", h["weight"]))
        return f"{'+'.join(h['id'] for h in rules)}: {top['reason']}"
    raisers = [c for c in decision.get("shap_top5") or [] if c["contribution"] > 0]
    if raisers:
        return f"model: {raisers[0]['feature']} raised the score most"
    return ""


def _wait_for_decision(client: httpx.Client, event_id: str, timeout: float = 30.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/decisions/{event_id}")
        if resp.status_code == 200:
            return resp.json()
        time.sleep(0.05)
    return None


def run(
    url: str,
    scenarios: list[str],
    users: int = 25,
    seed: int | None = None,
    delay: float = 0.6,
    color: bool = True,
) -> int:
    def c(code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if color else text

    rng = random.Random(seed)
    now = datetime.now(UTC) - timedelta(minutes=1)
    personas, history = build_world(rng, users, now)

    with httpx.Client(base_url=url, timeout=30) as client:
        print(c(BOLD, f"Building {HISTORY_DAYS} days of normal history for {users} customers"))
        for i in range(0, len(history), 500):
            resp = client.post("/events/batch", json={"events": history[i : i + 500]})
            if resp.status_code != 202:
                print(f"ingest failed: HTTP {resp.status_code} {resp.text[:200]}", file=sys.stderr)
                return 1
        if _wait_for_decision(client, history[-1]["event_id"], timeout=120) is None:
            print("no decisions are coming back: is the worker running?", file=sys.stderr)
            return 1
        print(c(DIM, f"  {len(history):,} events scored\n"))

        ctx = Context(rng=rng, personas=personas, window_start=now - ATTACK_WINDOW, window_end=now)
        caught = fraud = false_alarms = 0
        for name in scenarios:
            fn = PATTERNS[name][0]
            print(c(BOLD, name.replace("_", " ").upper()))
            print(c(DIM, " ".join((fn.__doc__ or "").split(".")[0].split())) + "\n")
            for event, is_fraud in sorted(fn(ctx), key=lambda pair: _ts(pair[0])):
                resp = client.post(f"/events/{event['event_type']}", json=event)
                if resp.status_code != 202:
                    print(f"  rejected: HTTP {resp.status_code}", file=sys.stderr)
                    continue
                decision = _wait_for_decision(client, event["event_id"])
                if decision is None:
                    print("  (no decision within 30 s)")
                    continue
                verdict = decision["final_decision"]
                score = decision["ml_score"]
                truth = c("\033[31m", "fraud  ") if is_fraud else c("\033[32m", "genuine")
                print(
                    f"  {_ts(event):%d %b %H:%M}  {truth}  "
                    f"{c(_DECISION_COLOR[verdict], f'{verdict:<6}')} "
                    f"{'  —' if score is None else f'{score:3.0f}'}  {describe(event)}"
                )
                reason = why(decision)
                if reason:
                    print(c(DIM, f"{'':31}{reason}"))
                flagged = verdict != "ALLOW"
                fraud += is_fraud
                caught += is_fraud and flagged
                false_alarms += (not is_fraud) and flagged
                time.sleep(delay)
            print()
        print(
            c(BOLD, f"Flagged {caught} of {fraud} fraudulent events")
            + f", {false_alarms} genuine event(s) flagged."
        )
        print(c(DIM, f"Open {url.replace(':8000', ':3000')}/queue to triage the reviews."))
    return 0
