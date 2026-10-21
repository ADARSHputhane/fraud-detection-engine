# Signals

Every event is turned into 35 numeric signals (plus two text fields, `event_type` and
`merchant_category`, that rules can read but the model does not). This page lists each one:
what it measures, why fraud teams care, and the edge cases the engine handles. The
authoritative list is [`features/spec.py`](../src/fraud_engine/features/spec.py); the
computation is [`features/engine.py`](../src/fraud_engine/features/engine.py).

## How signals are computed

- **Event time, not wall-clock time.** Every window is measured from the event's own
  timestamp. "Payments on this card in the last hour" means the hour before *this payment*,
  so replaying last month's events produces exactly the features they had live.
- **Redis sorted sets** hold the velocity state: members are event or card ids, scores are
  timestamps, and a window is a `ZCOUNT` between two scores. Two pipelined round trips per
  event; 2.5 ms p50 / 4.9 ms p95 over 35,421 events (0.7 ms p50 inside the Docker network).
- **Velocity counts include the event itself**; **baselines exclude it**. A spend spike is
  compared with the user's history, not with a history that already contains the spike.
- **Missing means unknown, not zero.** A user with fewer than five payments has no
  z-score; a card never seen being added has no age. Rules never fire on a missing value,
  and the model receives it as missing (XGBoost learns what "unknown" means on its own).
- **Idempotent.** A redelivered event (the worker crashed before acknowledging it) returns
  the vector computed the first time and is not counted twice.

## Event context

| Signal | What it measures | Why it matters |
|---|---|---|
| `is_payment` | Payment (true) or card added / updated / removed (false) | Card events and payments carry different risks; one model scores both |
| `amount` | Payment amount | Cash-out transactions are large; probes are tiny |
| `payment_approved` | Issuer approved the attempt | "Approved after many declines" is the signature of a guessed CVV (R003) |
| `channel_card_present` | Card physically swiped or tapped | A cloned card is used at a terminal; stolen card details are used online |
| `high_risk_category` | Gift cards, crypto, gambling, money transfer | Value that can be resold or moved instantly. Genuine users buy these too (2.9% of normal payments), so on its own it proves nothing |

## Velocity

| Signal | What it measures | Why it matters |
|---|---|---|
| `cards_added_1h` / `_3h` / `_24h` | Cards added to this account in the window | Card testing: a throwaway account adds a batch of stolen cards to find the live ones (R001 at three in 3 h) |
| `card_txn_count_5m` / `_1h` | Payments on this card | Bursts of attempts on one card |
| `device_txn_count_5m` / `_1h` | Payments from this device | A fraud rig works fast; people don't buy ten things in five minutes |
| `device_distinct_cards_24h` | Different cards used on this device | One device cycling through cards is a carding rig (R005, on a device under 72 h old) |
| `ip_distinct_cards_24h` | Different cards used from this IP | The same, seen from the network side |
| `card_failed_attempts_1h` | Declined payments on this card | CVV guessing produces a run of `incorrect_cvv` declines (R003, R008) |
| `user_total_amount_1h` | Total attempted amount by the user | Draining an account fast |

Edge cases: a card re-added later keeps one entry with its latest time; an older event
arriving late can't move a card's "last seen" time backwards (`ZADD GT`).

## Behaviour against the user's own baseline

| Signal | What it measures | Why it matters |
|---|---|---|
| `user_payments_90d` | Approved payments in the 90-day baseline | How much the other behavioural signals can be trusted |
| `amount_zscore` | (log amount − mean) / std over the user's 90 days of approved payments; needs at least 5 | "Unusual for *this* user": ₹20,000 is routine for some, alarming for others |
| `amount_vs_typical` | Amount ÷ the user's geometric-mean payment | The same idea in units an analyst reads at a glance ("40× typical") |
| `hour_share` | Share of the user's past payments within an hour of this time of day | Account takeovers often happen while the owner is asleep |
| `unusual_hour` | Under 5% of the user's payments at this hour (needs 10 payments) | A readable version of the above |
| `category_first_time` | First payment by this user in this merchant category | A first-ever crypto purchase after months of groceries |

Why log scale: spending is heavy-tailed. On raw amounts one electronics purchase inflates
the standard deviation so much that real spikes stop standing out; on log amounts a
z-score of 3 is already a large jump. The standard deviation has a floor (0.3), so a user
who always spends the same amount doesn't get an enormous z-score for a small change.
Declined payments never enter the baseline.

## Account and device

| Signal | What it measures | Why it matters |
|---|---|---|
| `account_age_hours` | Time since the account was first seen | Throwaway accounts used for card testing are hours old |
| `device_age_hours` | Time since this device was first seen on this account | The single strongest signal in this dataset, which is why the generator makes genuine users change phones too |
| `new_device` | Device first seen on this account under 24 h ago | Most takeovers come from the attacker's device |

## Geo and network

| Signal | What it measures | Why it matters |
|---|---|---|
| `distance_from_last_km` | Great-circle distance from this card's previous payment | Context for the next two |
| `travel_speed_kmh` | Distance ÷ time since that payment | Cloned cards are used far from the genuine card |
| `impossible_travel` | Over 100 km away at more than 900 km/h | Faster than an airliner. Below 100 km, GPS and IP geolocation noise dominates |
| `new_country` | Country first seen on this account under 24 h ago | Cards abroad for the first time |
| `geo_bin_country_mismatch` | Event country differs from the card's issuing country | Indian cards used in Lagos; US cards used in Jakarta |
| `datacenter_ip` | IP inside a cloud or hosting range | Real customers rarely pay from AWS; fraud tooling does. VPN users are the false positives |

Edge cases: an event that arrives out of order is judged on the absolute time gap; the time
gap has a one-minute floor so near-simultaneous events don't divide by zero; an older event
never overwrites the card's latest known location.

Impossible travel cuts both ways. When a cloned card is swiped abroad and the genuine
cardholder pays at home minutes later, the *genuine* purchase is the one that looks
impossible, relative to the fraud. That's why the rules split it: travel to a new device or
country is denied (R002), travel back to a familiar device and country is reviewed (R007).

## Card (instrument)

| Signal | What it measures | Why it matters |
|---|---|---|
| `card_age_hours` | Time since the card was added to the account | Stolen cards are used within minutes of being added |
| `bin_risk` | Risk of the card's issuer range, 0–1 | Prepaid cards are cheap and anonymous; some BIN ranges have high chargeback rates |
| `card_name_mismatch` | Cardholder name differed from the account holder when added | Someone else's card on your account. Family cards are the false positives |
| `new_card_high_amount` | Card under 24 h old and at least 5× typical spend (or ₹10,000 with no history) | The cash-out after adding a stolen card |

The BIN table and datacenter ranges are plain files in
[`features/reference/`](../src/fraud_engine/features/reference/), illustrative here; in
production they come from a BIN intelligence feed (plus your own chargeback rate per BIN)
and an IP-to-ASN database refreshed daily.

## The rules built on these signals

Measured on all 35,421 labelled events (1,775 fraudulent): 90.9% of fraud flagged at a
0.17% false-positive rate, 0.01% of genuine events denied.

| Rule | Condition | Action | Hits | Why |
|---|---|---|---|---|
| R001 Card testing | `cards_added_3h >= 3` | DENY | 535 | Nobody adds three cards in three hours |
| R002 Impossible travel to a new device or country | `impossible_travel and (new_device or new_country)` | DENY | 66 | The cloned card, abroad |
| R003 CVV brute force | `card_failed_attempts_1h >= 5 and payment_approved` | DENY | 53 | The guess that worked |
| R004 Spend spike on a new card | `new_card_high_amount or (amount_zscore > 3 and card_age_hours < 24)` | REVIEW | 153 | Cash-out on a freshly added card |
| R005 Many cards on a fresh device | `device_distinct_cards_24h >= 4 and device_age_hours < 72` | REVIEW | 339 | A carding rig, not someone who owns four cards |
| R006 Hosting IP on a new device | `datacenter_ip and new_device` | REVIEW | 1,085 | Fraud tooling runs in the cloud |
| R007 Impossible travel back to familiar ground | `impossible_travel and not new_device and not new_country` | REVIEW | 52 | Probably the genuine cardholder, on a compromised card |
| R008 Repeated declines from a new device | `card_failed_attempts_1h >= 3 and new_device` | REVIEW | 394 | CVV guessing before it succeeds |
| R009 New country, new device, foreign card | `new_country and new_device and geo_bin_country_mismatch` | REVIEW | 1,289 | The combination genuine travellers almost never show |
| R010 Name mismatch on a new device | `card_name_mismatch and new_device` | REVIEW | 971 | Someone else's card, from someone else's phone |

R001–R006 are the starter rules from the plan (74.3% recall on this data); R007–R010 and
the R002 / R005 refinements came from reading the misses and false alarms.

## What the model relies on

Top features by XGBoost gain: `new_device`, `card_name_mismatch`, `channel_card_present`,
`device_age_hours`, `geo_bin_country_mismatch`, `device_txn_count_1h`, `account_age_hours`,
`amount`, `datacenter_ip`, `user_total_amount_1h`. Per-event explanations (SHAP) are on every
decision and in the dashboard.

## Signals not built yet

- **Graph links**: devices, IPs and cards shared between accounts ("connected to a banned
  account"), the main tool against fraud rings.
- **Device fingerprinting and emulator detection**, beyond a device id the client supplies.
- **Merchant risk**: chargeback rates per merchant and merchant category code.
- **Real BIN and IP intelligence feeds** in place of the illustrative reference files.
