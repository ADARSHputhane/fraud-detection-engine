# Payment Fraud Detection Engine

A real-time payment fraud detection engine built from scratch: event ingestion, streaming
feature computation, an explainable rules engine and an ML risk scorer, aiming to return
`ALLOW` / `REVIEW` / `DENY` for every payment in under 100 ms.

> **Status:** Milestones 1–4 of 7 complete — event ingestion, a labelled synthetic traffic
> generator, a streaming feature engine, an explainable rules engine and a calibrated ML risk
> scorer with SHAP explanations. See the [roadmap](#roadmap).

![CI](https://github.com/ADARSHputhane/fraud-detection-engine/actions/workflows/ci.yml/badge.svg)

## Why I'm building this

I work in fraud and risk operations, investigating suspicious transactions by hand. This
project is the system I'd want behind that work: one that catches card testing, cloned
cards and account takeovers automatically, and explains *why* it flagged something so an
analyst can act on it in seconds.

## Architecture

```mermaid
flowchart LR
    C[Client / PSP] -->|POST /events| API[FastAPI ingest]
    API -->|source of truth| PG[(Postgres<br/>events)]
    API -->|XADD| RS[[Redis Stream<br/>events:incoming]]
    RS --> FE[Feature engine<br/>velocity · geo · behaviour]
    FE --> RE[Rules engine<br/>YAML, explainable]
    FE --> ML[ML scorer<br/>XGBoost + SHAP]
    RE --> D{Decision<br/>ALLOW / REVIEW / DENY}
    ML --> D
    D --> AUD[(Audit log)]
    AUD --> DASH[Analyst dashboard]
    DASH -->|labels| ML

    classDef done fill:#d1fae5,stroke:#059669,color:#064e3b;
    class API,PG,RS,FE,RE,ML,D,AUD done;
```

Green = built. The rest is on the roadmap below.

## Milestone 1 results

Measured on a single development machine against real PostgreSQL and Redis:

| Check | Result |
|---|---|
| 10,000-event replay (batches of 500) | 10,000 accepted, **0 validation errors**, ~4,100 events/s |
| Single-event ingest latency | **3.9 ms p50 · 5.5 ms p95** |
| Re-sending the same events | All detected as duplicates, nothing published twice |
| Test suite | 58 tests, runs in ~1 s without Docker |

## Milestone 2 results

A worker reads `events:incoming` through a Redis consumer group and computes 35 signals per
event: velocity counters, per-user spending baselines, geo and network checks, and card
history.

| Check | Result |
|---|---|
| Feature computation, all 35,961 events of a 30-day dataset, real Redis | **2.0 ms p50 · 4.7 ms p95** (target < 10 ms) |
| Worker throughput, 10,000 events end to end into Postgres | ~420 events/s, one worker |
| Replay of the same events on fresh state | Identical vectors (windows use event time, not wall clock) |
| Worker crash before acknowledging | Entries stay pending and are reprocessed; no double counting |

How it works:

- **Velocity windows are Redis sorted sets scored by event time.** "Payments on this card
  in the last 5 minutes" is a `ZCOUNT` over `[t − 300 s, t]`, where `t` is the event's own
  timestamp. Replaying last month's data gives the same answers it would have given live.
- **Two pipelined round trips per event.** The first records the event in the velocity sets
  and reads prior state; the second folds the event into the user's baselines. Baselines are
  read *before* the update, so an amount is compared with history that doesn't include it.
- **Idempotent.** Set members are event or card ids, and each computed vector is cached
  under the event id, so a redelivered event returns the same vector without being counted
  twice.
- **Out-of-order and skewed clocks are handled explicitly:** impossible travel uses the
  absolute time gap with a one-minute floor, first-seen times only ever move earlier, and a
  card's age can't go negative.

## Milestone 3 results

Rules live in [`default_rules.yml`](src/fraud_engine/rules/default_rules.yml): readable by
an analyst, hot-reloaded when the file changes, and every hit carries a sentence explaining
it, e.g. *"Card used 4540 km from its last payment, at 5988 km/h, from a new device or
country"*.

Measured on all 35,421 labelled events (1,775 fraudulent):

| Ruleset | Recall | False-positive rate | DENY false-positive rate |
|---|---|---|---|
| Starter rules R001–R006 from the plan | 74.3% | 0.30% | 0.15% |
| **After error analysis (R001–R010)** | **90.9%** | **0.17%** | **0.01%** |

Target: ≥ 80% recall at < 2% false positives. The starter rules miss it; error analysis
closes the gap. The remaining misses are account takeovers on the victim's own device
(76% caught) and friendly fraud (0%, by design).

What the error analysis found:

- **Most false alarms from the impossible-travel rule were genuine cardholders paying at
  home minutes after a fraudster used their card abroad.** Relative to the fraud, the genuine
  purchase looks like impossible travel. The rule is right that the card is compromised, but
  denying the real customer is the wrong call. Impossible travel is now split: to a *new*
  device or country is DENY (R002); back to a familiar device and country is REVIEW (R007).
- **"Four cards on one device" also fired on people who simply own four cards.** It now
  needs a device first seen in the last 72 hours (R005), which is what a carding rig looks
  like.
- **The misses shared one fingerprint**: a device new to the account combined with a new
  country and a foreign-issued card (R009), repeated declines (R008), or a cardholder-name
  mismatch (R010).
- `amount_zscore` is computed on log amounts, because spending is heavy-tailed; a threshold
  of 5 on that scale missed most spend spikes, so R004 uses 3 or "≥ 5× typical spend".

Safety: conditions are parsed with Python's `ast` and checked against an allow-list
(comparisons, `and`/`or`/`not`, feature names, literals); nothing reaches `eval()`. Unknown
feature names fail at load time, and a broken edit leaves the last good ruleset running.

## Milestone 4 results

An XGBoost classifier scores every event 0–100, with the five features that pushed the score
most (exact TreeSHAP values). Trained on the oldest 60% of events, calibrated on the next 20%,
and **every number below is from the newest 20%, which the model never saw** — the way it
would be deployed.

| On the 7,085 held-out events (316 fraudulent) | Recall | False-positive rate | Precision |
|---|---|---|---|
| Rules alone | 87.0% | 0.21% | 95.2% |
| **XGBoost alone** (score ≥ 40) | **90.2%** | **0.01%** | 99.7% |
| Combined decision, DENY only | 89.6% | 0.00% | 100% |
| Isolation Forest, no labels (cold-start fallback) | 63.3% | 0.00% | 100% |

| Check | Result | Target |
|---|---|---|
| AUC-ROC, held out | **0.984** (PR-AUC 0.941) | ≥ 0.90 |
| Scoring latency incl. SHAP, single event | **0.33 ms p95** | < 30 ms |
| Calibration (Brier score) | 0.0043 | — |

- **Class imbalance:** fraud is 5% of events; `scale_pos_weight` ≈ 19 stops the model from
  scoring everything as legitimate.
- **Calibrated scores:** reweighting inflates raw probabilities on purpose, so they are
  re-mapped with Platt scaling on the held-out calibration period. On the test period,
  events scored 5–20 averaged 8.4 and were 9.4% fraud; events scored 90+ averaged 99.4 and
  were all fraud. The middle of the range holds too few events (2–7 per band) to verify, a
  consequence of how cleanly the model separates the classes.
- **Where the model beats the rules:** account takeovers on the victim's own device
  (83% vs 76%), which no device or location rule can see.
- **Explanations:** XGBoost's built-in TreeSHAP (`pred_contribs=True`), exact and additive
  (contributions + bias = the model's output, checked in the tests), without pulling in the
  `shap` package.
- **Safe model files:** XGBoost's own JSON format and calibration parameters as JSON; no
  pickle for the main model. Versions live side by side in `models/`; `CURRENT` names the
  active one and running services swap to a newly promoted version without a restart.

## Design decisions

- **No real card data, ever.** `card_id` must be a token (`tok_…`). The schema detects raw
  card numbers (13–19 digits passing the Luhn checksum) even with spaces, dashes or a
  `tok_` prefix, and rejects them. Validation errors deliberately **don't echo the input
  back**, so a mistakenly sent card number never ends up in client logs.
- **Strict contract.** Unknown fields are rejected (`extra="forbid"`), timestamps must be
  timezone-aware, and events more than 5 minutes in the future are refused (clock-skew
  tolerance).
- **Idempotent ingestion.** `event_id` is the primary key; retries are safe.
- **Persist first, then publish.** Postgres is the source of truth. If Redis is down the
  insert is rolled back and the API returns 503, so the client's retry isn't mistaken for a
  duplicate.
- **One ordered stream for all event types.** Detecting card testing ("3 cards added, then a
  burst of tiny charges") needs card-added and payment events in true arrival order.
- **Labels never travel with events.** The generator writes ground truth to a separate file,
  so the engine can't learn from leaked labels.

## Synthetic data

No public dataset has the mix of signals this engine needs, so `fraud-gen` simulates it.
500 users over 30 days gives about 35,000 events, 5% of them fraudulent.

Normal users get a home city, one to three long-standing cards, active hours, a personal
spending mix and realistic noise: domestic trips and holidays abroad, new phones, VPNs, a
mistyped CVV followed by a successful retry, insufficient-funds retries, a family member's
card, occasional late-night orders, the odd legitimate gift-card purchase. That noise
matters, because a good engine has to tell it apart from real attacks.

The noise was added after the first model scored a perfect AUC of 1.0: every attack used a
brand-new device and genuine users almost never did, so one feature gave the label away.
Perfect scores on held-out data usually mean the data is too easy, not that the model is
good.

| Injected pattern | What happens |
|---|---|
| **Card testing** | Throwaway account on a datacenter IP (or a residential proxy) adds 3–6 stolen cards, probes each with ₹1–₹99 charges (mostly declined), cashes out the live ones on gift cards |
| **CVV brute force** | 5–10 `incorrect_cvv` declines on one card within minutes, then an approved high-value electronics purchase |
| **Impossible travel** | Cardholder pays at home; 10–90 minutes later the cloned card is swiped abroad |
| **Amount spike** | New card added to a victim's account between 1 and 5 am IST, then a purchase 8–60× their normal spend on gift cards or crypto |
| **Account takeover** | New device on a foreign hosting IP drains the victim's existing card via transfers and gift cards — or, a third of the time, the attacker is on the victim's *own* phone and home connection |
| **Friendly fraud** | A genuine purchase the cardholder later disputes. Indistinguishable from normal traffic, so it caps achievable recall, as in real data |

The output is deterministic for a given `--seed`.

## Quickstart

**With Docker:**

```bash
docker compose up -d --build              # Postgres + Redis + API on :8000 + worker
pip install -e .                          # installs the fraud-gen CLI
fraud-gen generate --users 500 --days 30 --out data/
fraud-gen replay --file data/events.jsonl --limit 10000

# Train a model (no services needed: features are computed with the live engine code)
fraud-engine build-features --events data/events.jsonl --out data/features.jsonl
fraud-engine train --promote              # the running worker picks it up within seconds
```

Open http://localhost:8000/docs for interactive API docs.

**Tests only (no Docker needed):**

```bash
pip install -e ".[dev]"
pytest
```

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/events/payment` | Ingest one payment attempt |
| `POST` | `/events/instrument` | Ingest a card added / updated / removed |
| `POST` | `/events/batch` | Up to 1,000 mixed events, order preserved |
| `GET` | `/events/{event_id}` | Fetch a stored event |
| `GET` | `/features/{event_id}` | The feature vector the worker computed for an event |
| `GET` | `/decisions/{event_id}` | Audit-log entry: decision, fired rules with reasons, timings |
| `GET` | `/rules` | The ruleset currently in force |
| `POST` | `/score` | Score a feature vector: calibrated 0–100 risk + top-5 SHAP contributions |
| `GET` | `/models/current` | The model in use: version, features, test metrics |
| `GET` | `/health` | Postgres and Redis status |

```bash
curl -X POST localhost:8000/events/payment -H 'content-type: application/json' -d '{
  "user_id": "usr_alice01", "card_id": "tok_a1b2c3d4e5f6", "device_id": "dev_phone01",
  "ip_address": "49.37.12.8", "timestamp": "2026-10-20T09:30:00+05:30",
  "amount": "1499.00", "currency": "INR",
  "merchant_id": "mer_grocery_001", "merchant_category": "grocery",
  "geo": {"lat": 12.97, "lon": 77.59, "country": "IN", "city": "Bengaluru"}
}'
# → 202 {"event_id": "...", "stream_id": "1729416600000-0", "status": "accepted"}
```

## Roadmap

- [x] **1. Event ingestion & schema** — FastAPI, Postgres, Redis Streams, synthetic generator
- [x] **2. Feature engine** — velocity counters (Redis sorted sets), amount z-scores,
      impossible travel, IP/BIN country mismatch, card age
- [x] **3. Rules engine** — YAML rules with human-readable reasons
- [x] **4. ML risk scorer** — XGBoost + Isolation Forest, SHAP explanations
- [ ] **5. Decision service & audit log** — <100 ms end to end, Prometheus metrics
- [ ] **6. Analyst dashboard** — Next.js review queue with a label feedback loop
- [ ] **7. Write-up & demo**

## Project layout

```
src/fraud_engine/
  schemas.py      event contracts + card-number detection
  api.py          ingest routes
  scoring_api.py  read-side routes (features, decisions, rules, score, model)
  db.py           Postgres tables
  stream.py       Redis Streams publisher and consumer-group reader
  features/       feature catalogue, Redis feature engine, geo, IP/BIN reference data
  rules/          YAML ruleset, safe condition compiler, hot reload, evaluation
  ml/             training set, XGBoost + Isolation Forest models, registry, training
  decision.py     combines rules and model score into ALLOW / REVIEW / DENY
  offline.py      the live feature engine run over a file (rule evaluation, training)
  pipeline.py     per-event stages, timed
  worker.py       stream worker: batch, process in order, persist, acknowledge
  cli.py          fraud-engine worker | bench-features | build-features | evaluate-rules |
                  load-labels | train
  main.py         app factory
  generator/      personas, fraud patterns, CLI
tests/            schema, API, generator, feature and worker tests
```

## Tech

Python 3.11+ · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) · PostgreSQL · Redis Streams ·
XGBoost · scikit-learn · pytest · Ruff · Docker · GitHub Actions

---

Built by [Adarsh Puthane](https://adarshputhane.com) ·
[LinkedIn](https://www.linkedin.com/in/adarshputhane/)
