"""Distance and travel-speed checks."""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088
# Faster than a commercial jet's ground speed: nobody legitimately covers ground this quickly.
IMPOSSIBLE_SPEED_KMH = 900.0
# Below this, GPS / IP geolocation error dominates and "travel" is mostly noise.
MIN_TRAVEL_DISTANCE_KM = 100.0
# Floor on the time gap so near-simultaneous events (or skewed clocks) don't divide by ~zero.
MIN_GAP_SECONDS = 60.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def travel(
    prev_lat: float, prev_lon: float, prev_ts: float, lat: float, lon: float, ts: float
) -> tuple[float, float, bool]:
    """Return (distance_km, speed_kmh, impossible) for moving between two events.

    The gap uses the absolute difference, so an event that arrives out of order (its timestamp
    is earlier than the previous one) is judged on how far apart the two really were.
    """
    distance = haversine_km(prev_lat, prev_lon, lat, lon)
    gap_hours = max(abs(ts - prev_ts), MIN_GAP_SECONDS) / 3600
    speed = distance / gap_hours
    impossible = distance > MIN_TRAVEL_DISTANCE_KM and speed > IMPOSSIBLE_SPEED_KMH
    return distance, speed, impossible
