"""Real-time feature computation.

All state lives in Redis and every window is measured in *event time* (the event's own
timestamp, never the wall clock), so replaying last month's events produces the same vectors
they got live. Wall-clock TTLs are used only to stop abandoned keys from filling memory.

Each event costs two pipelined round trips:

1. Record the event in the velocity sets and count them, and read the prior state the other
   features need. Velocity members are event ids or card ids, so recording twice is harmless.
2. Fold the event into that state: spending baselines, first-seen times, the card's last
   location. Baselines are read in step 1, *before* this update, so an amount is compared
   with the user's history rather than with a history that already contains it.

Step 2 is skipped when the event was already processed (the worker retried after a crash);
the vector computed the first time is returned instead, so nothing is counted twice.

Known limitation: two workers handling events for the same user at the same instant can race
between steps 1 and 2. Run one worker per stream, or shard the stream by user, to avoid it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from redis.asyncio import Redis

from ..schemas import HIGH_RISK_CATEGORIES
from .geo import travel
from .reference import BinRiskTable, DatacenterRanges
from .spec import FEATURE_NAMES

HOUR = 3600.0
DAY = 86_400.0
BASELINE_DAYS = 90
MIN_BASELINE_PAYMENTS = 5
MIN_HOUR_HISTORY = 10
UNUSUAL_HOUR_SHARE = 0.05
# Users whose spend barely varies would otherwise get enormous z-scores for small changes.
LOG_STD_FLOOR = 0.3
NEW_WINDOW_HOURS = 24.0
HIGH_AMOUNT_VS_TYPICAL = 5.0
# Fallback when the user has no baseline yet; in the event's currency (INR in the demo data).
HIGH_AMOUNT_NO_HISTORY = 10_000.0
# Keep a little more than the longest window when pruning, to tolerate out-of-order events.
PRUNE_SLACK = HOUR
VELOCITY_TTL = int(2 * DAY)
PROFILE_TTL = int(120 * DAY)
SEEN_TTL = int(2 * DAY)

_HIGH_RISK = {c.value for c in HIGH_RISK_CATEGORIES}

Op = tuple[str | None, str, tuple, dict]


def _str(v: Any) -> Any:
    return v.decode() if isinstance(v, bytes) else v


def _float(v: Any) -> float | None:
    v = _str(v)
    return None if v is None or v == "" else float(v)


def _hours(seconds: float) -> float:
    return round(max(seconds, 0.0) / HOUR, 3)


@dataclass(slots=True)
class _Event:
    event_id: str
    event_type: str
    user_id: str
    card_id: str
    device_id: str
    ip: str
    ts: float
    lat: float | None
    lon: float | None
    country: str | None
    amount: float | None = None
    category: str | None = None
    channel: str | None = None
    approved: bool | None = None
    action: str | None = None
    bin: str | None = None
    card_type: str | None = None
    issuing_country: str | None = None
    name_matches: bool | None = None

    @classmethod
    def parse(cls, e: dict) -> _Event:
        geo = e.get("geo") or {}
        ev = cls(
            event_id=str(e["event_id"]),
            event_type=e["event_type"],
            user_id=e["user_id"],
            card_id=e["card_id"],
            device_id=e["device_id"],
            ip=str(e["ip_address"]),
            ts=datetime.fromisoformat(e["timestamp"]).timestamp(),
            lat=geo.get("lat"),
            lon=geo.get("lon"),
            country=geo.get("country"),
        )
        if ev.event_type == "payment":
            ev.amount = float(e["amount"])
            ev.category = e["merchant_category"]
            ev.channel = e.get("channel", "online")
            ev.approved = e.get("status", "approved") == "approved"
        else:
            ev.action = e["action"]
            ev.bin = e["bin"]
            ev.card_type = e["card_type"]
            ev.issuing_country = e["issuing_country"]
            ev.name_matches = e.get("name_matches_account")
        return ev

    @property
    def is_payment(self) -> bool:
        return self.event_type == "payment"


class FeatureEngine:
    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str = "fe",
        datacenter: DatacenterRanges | None = None,
        bins: BinRiskTable | None = None,
    ) -> None:
        self.redis = redis
        self.prefix = prefix
        self.datacenter = datacenter or DatacenterRanges.default()
        self.bins = bins or BinRiskTable.default()

    def _k(self, *parts: str) -> str:
        return ":".join((self.prefix, *parts))

    async def _run(self, ops: list[Op]) -> dict[str, Any]:
        async with self.redis.pipeline(transaction=False) as pipe:
            for _, method, args, kwargs in ops:
                getattr(pipe, method)(*args, **kwargs)
            results = await pipe.execute()
        return {name: r for (name, *_), r in zip(ops, results, strict=True) if name}

    async def reset(self) -> int:
        """Delete all feature state under this prefix (used before a full replay)."""
        deleted = 0
        batch: list = []
        async for key in self.redis.scan_iter(match=f"{self.prefix}:*", count=1000):
            batch.append(key)
            if len(batch) >= 1000:
                deleted += await self.redis.delete(*batch)
                batch.clear()
        if batch:
            deleted += await self.redis.delete(*batch)
        return deleted

    async def compute(self, event: dict) -> dict[str, Any]:
        """Compute the feature vector for one event and fold the event into the state."""
        ev = _Event.parse(event)
        t = ev.ts
        k_seen = self._k("seen", ev.event_id)
        k_user_cards = self._k("u", ev.user_id, "cards")
        k_user_amt = self._k("u", ev.user_id, "amt")
        k_user_meta = self._k("u", ev.user_id, "meta")
        k_user_devices = self._k("u", ev.user_id, "devices")
        k_user_countries = self._k("u", ev.user_id, "countries")
        k_user_days = self._k("u", ev.user_id, "days")
        k_user_hours = self._k("u", ev.user_id, "hours")
        k_user_cats = self._k("u", ev.user_id, "cats")
        k_card_tx = self._k("c", ev.card_id, "tx")
        k_card_fail = self._k("c", ev.card_id, "fail")
        k_card_meta = self._k("c", ev.card_id, "meta")
        k_dev_tx = self._k("d", ev.device_id, "tx")
        k_dev_cards = self._k("d", ev.device_id, "cards")
        k_ip_cards = self._k("ip", ev.ip, "cards")

        # ---- Round trip 1: record velocity, count windows, read prior state ----------------
        ops: list[Op] = [("seen", "get", (k_seen,), {})]
        if ev.action == "added":
            # gt=True: an out-of-order older event must not move a card's time backwards.
            ops.append((None, "zadd", (k_user_cards, {ev.card_id: t}), {"gt": True}))
        if ev.is_payment:
            ops += [
                (None, "zadd", (k_card_tx, {ev.event_id: t}), {}),
                (None, "zadd", (k_dev_tx, {ev.event_id: t}), {}),
                (None, "zadd", (k_user_amt, {f"{ev.event_id}|{ev.amount}": t}), {}),
            ]
            if not ev.approved:
                ops.append((None, "zadd", (k_card_fail, {ev.event_id: t}), {}))
        ops += [
            (None, "zadd", (k_dev_cards, {ev.card_id: t}), {"gt": True}),
            (None, "zadd", (k_ip_cards, {ev.card_id: t}), {"gt": True}),
            ("cards_added_1h", "zcount", (k_user_cards, t - HOUR, t), {}),
            ("cards_added_3h", "zcount", (k_user_cards, t - 3 * HOUR, t), {}),
            ("cards_added_24h", "zcount", (k_user_cards, t - DAY, t), {}),
            ("card_txn_count_5m", "zcount", (k_card_tx, t - 300, t), {}),
            ("card_txn_count_1h", "zcount", (k_card_tx, t - HOUR, t), {}),
            ("device_txn_count_5m", "zcount", (k_dev_tx, t - 300, t), {}),
            ("device_txn_count_1h", "zcount", (k_dev_tx, t - HOUR, t), {}),
            ("device_distinct_cards_24h", "zcount", (k_dev_cards, t - DAY, t), {}),
            ("ip_distinct_cards_24h", "zcount", (k_ip_cards, t - DAY, t), {}),
            ("card_failed_attempts_1h", "zcount", (k_card_fail, t - HOUR, t), {}),
            ("amounts_1h", "zrangebyscore", (k_user_amt, t - HOUR, t), {}),
            ("user_first_seen", "hget", (k_user_meta, "first_seen"), {}),
            ("device_first_seen", "hget", (k_user_devices, ev.device_id), {}),
            ("card_meta", "hgetall", (k_card_meta,), {}),
        ]
        if ev.country:
            ops.append(("country_first_seen", "hget", (k_user_countries, ev.country), {}))
        if ev.is_payment:
            ops += [
                ("days", "hgetall", (k_user_days,), {}),
                ("hours", "hgetall", (k_user_hours,), {}),
                ("category_seen", "sismember", (k_user_cats, ev.category), {}),
            ]
        r = await self._run(ops)

        if r["seen"] is not None:
            return json.loads(_str(r["seen"]))

        meta = {_str(k): _str(v) for k, v in (r["card_meta"] or {}).items()}
        f: dict[str, Any] = dict.fromkeys(FEATURE_NAMES)

        # ---- Context ----------------------------------------------------------------------
        f["event_type"] = ev.event_type
        f["is_payment"] = ev.is_payment
        if ev.is_payment:
            f["merchant_category"] = ev.category
            f["amount"] = ev.amount
            f["payment_approved"] = ev.approved
            f["channel_card_present"] = ev.channel == "card_present"
            f["high_risk_category"] = ev.category in _HIGH_RISK

        # ---- Velocity ---------------------------------------------------------------------
        for name in (
            "cards_added_1h",
            "cards_added_3h",
            "cards_added_24h",
            "card_txn_count_5m",
            "card_txn_count_1h",
            "device_txn_count_5m",
            "device_txn_count_1h",
            "device_distinct_cards_24h",
            "ip_distinct_cards_24h",
            "card_failed_attempts_1h",
        ):
            f[name] = int(r[name])
        f["user_total_amount_1h"] = round(
            sum(float(_str(m).rsplit("|", 1)[1]) for m in r["amounts_1h"]), 2
        )

        # ---- Behaviour --------------------------------------------------------------------
        log_amount = 0.0
        days: dict[str, float] = {}
        if ev.is_payment:
            log_amount = math.log(ev.amount)
            today = int(t // DAY)
            days = {_str(k): float(_str(v)) for k, v in (r["days"] or {}).items()}
            n = s = q = 0.0
            for key, value in days.items():
                day, stat = key.split(":")
                if today - BASELINE_DAYS < int(day) <= today:
                    if stat == "n":
                        n += value
                    elif stat == "s":
                        s += value
                    else:
                        q += value
            f["user_payments_90d"] = int(n)
            if n >= MIN_BASELINE_PAYMENTS:
                mean = s / n
                var = max(q / n - mean * mean, 0.0) * n / (n - 1)
                std = max(math.sqrt(var), LOG_STD_FLOOR)
                f["amount_zscore"] = round((log_amount - mean) / std, 3)
                f["amount_vs_typical"] = round(ev.amount / math.exp(mean), 3)

            hours = {int(_str(k)): int(_str(v)) for k, v in (r["hours"] or {}).items()}
            total = sum(hours.values())
            hour = int((t % DAY) // HOUR)
            if total >= MIN_HOUR_HISTORY:
                near = sum(hours.get((hour + d) % 24, 0) for d in (-1, 0, 1))
                f["hour_share"] = round(near / total, 4)
                f["unusual_hour"] = f["hour_share"] < UNUSUAL_HOUR_SHARE
            else:
                f["unusual_hour"] = False
            f["category_first_time"] = not r["category_seen"]

        # ---- Account and device -----------------------------------------------------------
        user_first = _float(r["user_first_seen"])
        device_first = _float(r["device_first_seen"])
        f["account_age_hours"] = _hours(t - user_first) if user_first is not None else 0.0
        f["device_age_hours"] = _hours(t - device_first) if device_first is not None else 0.0
        f["new_device"] = f["device_age_hours"] < NEW_WINDOW_HOURS

        # ---- Geo and network --------------------------------------------------------------
        f["datacenter_ip"] = self.datacenter.contains(ev.ip)
        f["impossible_travel"] = False
        country_first = None
        if ev.country:
            country_first = _float(r.get("country_first_seen"))
            f["new_country"] = (
                country_first is None or (t - country_first) / HOUR < NEW_WINDOW_HOURS
            )
        last_ts = _float(meta.get("last_ts"))
        if ev.is_payment and ev.lat is not None and last_ts is not None:
            distance, speed, impossible = travel(
                float(meta["last_lat"]), float(meta["last_lon"]), last_ts, ev.lat, ev.lon, t
            )
            f["distance_from_last_km"] = round(distance, 1)
            f["travel_speed_kmh"] = round(speed, 1)
            f["impossible_travel"] = impossible

        # ---- Instrument -------------------------------------------------------------------
        added_at = _float(meta.get("added_at"))
        if ev.is_payment:
            card_type, bin_, issuer = meta.get("card_type"), meta.get("bin"), meta.get("issuer")
            name_match = meta.get("name_match")
            name_mismatch = None if not name_match else name_match == "0"
        else:
            card_type, bin_, issuer = ev.card_type, ev.bin, ev.issuing_country
            name_mismatch = None if ev.name_matches is None else not ev.name_matches
            if ev.action == "added" and (added_at is None or t < added_at):
                added_at = t
        if added_at is not None:
            f["card_age_hours"] = _hours(t - added_at)
        if bin_ or card_type:
            f["bin_risk"] = self.bins.risk(bin_, card_type)
        f["card_name_mismatch"] = name_mismatch
        if ev.country and issuer:
            f["geo_bin_country_mismatch"] = ev.country != issuer
        f["new_card_high_amount"] = False
        if ev.is_payment and f["card_age_hours"] is not None:
            if f["amount_vs_typical"] is not None:
                high = f["amount_vs_typical"] >= HIGH_AMOUNT_VS_TYPICAL
            else:
                high = ev.amount >= HIGH_AMOUNT_NO_HISTORY
            f["new_card_high_amount"] = f["card_age_hours"] < NEW_WINDOW_HOURS and high

        # ---- Round trip 2: fold this event into the state ---------------------------------
        w: list[Op] = []
        # Keep the earliest first-seen time, so an out-of-order event can't move it forward.
        if user_first is None or t < user_first:
            w.append((None, "hset", (k_user_meta, "first_seen", t), {}))
        if device_first is None or t < device_first:
            w.append((None, "hset", (k_user_devices, ev.device_id, t), {}))
        if ev.country and (country_first is None or t < country_first):
            w.append((None, "hset", (k_user_countries, ev.country, t), {}))
        if ev.is_payment and ev.approved:
            today = int(t // DAY)
            w += [
                (None, "hincrby", (k_user_days, f"{today}:n", 1), {}),
                (None, "hincrbyfloat", (k_user_days, f"{today}:s", log_amount), {}),
                (None, "hincrbyfloat", (k_user_days, f"{today}:q", log_amount**2), {}),
                (None, "hincrby", (k_user_hours, str(int((t % DAY) // HOUR)), 1), {}),
                (None, "sadd", (k_user_cats, ev.category), {}),
            ]
            stale = [k for k in days if int(k.split(":")[0]) <= today - BASELINE_DAYS]
            if stale:
                w.append((None, "hdel", (k_user_days, *stale), {}))
        if ev.is_payment and ev.lat is not None and (last_ts is None or t >= last_ts):
            mapping = {"last_lat": ev.lat, "last_lon": ev.lon, "last_ts": t}
            w.append((None, "hset", (k_card_meta,), {"mapping": mapping}))
        if not ev.is_payment:
            mapping = {"bin": ev.bin, "card_type": ev.card_type, "issuer": ev.issuing_country}
            if ev.name_matches is not None:
                mapping["name_match"] = "1" if ev.name_matches else "0"
            if added_at is not None:
                mapping["added_at"] = added_at
            w.append((None, "hset", (k_card_meta,), {"mapping": mapping}))

        for key, keep in (
            (k_card_tx, HOUR),
            (k_dev_tx, HOUR),
            (k_card_fail, HOUR),
            (k_user_amt, HOUR),
            (k_user_cards, DAY),
            (k_dev_cards, DAY),
            (k_ip_cards, DAY),
        ):
            w += [
                (None, "zremrangebyscore", (key, "-inf", t - keep - PRUNE_SLACK), {}),
                (None, "expire", (key, VELOCITY_TTL), {}),
            ]
        for key in (
            k_user_meta,
            k_user_devices,
            k_user_countries,
            k_user_days,
            k_user_hours,
            k_user_cats,
            k_card_meta,
        ):
            w.append((None, "expire", (key, PROFILE_TTL), {}))
        w.append((None, "set", (k_seen, json.dumps(f)), {"ex": SEEN_TTL}))
        await self._run(w)
        return f
