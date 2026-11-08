"""Trips: runs of days on which photos were taken far from home.

Works on the photos that have a location. "Home" is worked out per day as the
spot (a 0.1° cell, about 10 km) where photos were taken on the most days in the
half year before and after, so a move to another town is followed. A day with a
photo more than AWAY_KM from home is an away day; away days with at most
MAX_GAP_DAYS days without located photos between them make one trip, and any
day with photos only near home ends it.

The trip covers the time from the last photo at home before leaving to the
first photo back home (whole days if there is none), so photos without a
location taken during the trip (another camera, an old phone) belong to it too.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

AWAY_KM = 50.0
MAX_GAP_DAYS = 2
HOME_WINDOW_DAYS = 183
_CELL_DEG = 0.1


@dataclass
class Trip:
    start: datetime                 # inclusive
    end: datetime                   # exclusive
    home: tuple                     # (lat, lng) of home while away
    points: list = field(default_factory=list)   # located photos in [start, end): (id, when, lat, lng)
    far_km: float = 0.0             # furthest from home

    @property
    def days(self) -> int:
        last = (self.end - timedelta(microseconds=1)).date()
        return (last - self.start.date()).days + 1


def km_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def _cell(lat: float, lng: float) -> tuple:
    return math.floor(lat / _CELL_DEG), math.floor(lng / _CELL_DEG)


def _cell_centre(cell: tuple) -> tuple:
    return (cell[0] + 0.5) * _CELL_DEG, (cell[1] + 0.5) * _CELL_DEG


def homes_by_day(by_day: dict) -> dict:
    """date -> home cell: the most frequent 'main spot of the day' within ±HOME_WINDOW_DAYS."""
    days = sorted(by_day)
    main = {d: Counter(_cell(p[2], p[3]) for p in by_day[d]).most_common(1)[0][0] for d in days}
    window = timedelta(days=HOME_WINDOW_DAYS)
    counts: Counter = Counter()
    homes = {}
    lo = hi = 0
    for d in days:
        while hi < len(days) and days[hi] <= d + window:
            counts[main[days[hi]]] += 1
            hi += 1
        while days[lo] < d - window:
            counts[main[days[lo]]] -= 1
            lo += 1
        homes[d] = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return homes


def find_trips(points) -> list[Trip]:
    """Trips found in located photos: an iterable of (id, taken_at, lat, lng)."""
    pts = sorted((p for p in points if p[1] is not None), key=lambda p: (p[1], p[0]))
    if not pts:
        return []
    by_day: dict = defaultdict(list)
    for p in pts:
        by_day[p[1].date()].append(p)
    homes = homes_by_day(by_day)

    def far(p, home):
        return km_between(p[2], p[3], home[0], home[1])

    trips: list[Trip] = []
    run: list[date] = []

    def close_run():
        if not run:
            return
        first_day, last_day = by_day[run[0]], by_day[run[-1]]
        home = _cell_centre(homes[run[0]])
        first_away = next(i for i, p in enumerate(first_day) if far(p, home) >= AWAY_KM)
        last_away = max(i for i, p in enumerate(last_day) if far(p, _cell_centre(homes[run[-1]])) >= AWAY_KM)
        start = (first_day[first_away - 1][1] + timedelta(microseconds=1) if first_away
                 else datetime.combine(run[0], time.min))
        end = (last_day[last_away + 1][1] if last_away + 1 < len(last_day)
               else datetime.combine(run[-1] + timedelta(days=1), time.min))
        trip = Trip(start=start, end=end, home=home)
        for d in run:
            trip.points.extend(p for p in by_day[d] if start <= p[1] < end)
        trip.far_km = max(far(p, _cell_centre(homes[p[1].date()])) for p in trip.points)
        trips.append(trip)
        run.clear()

    for d in sorted(by_day):
        home = _cell_centre(homes[d])
        away = any(far(p, home) >= AWAY_KM for p in by_day[d])
        if not away:
            close_run()
            continue
        if run and (d - run[-1]).days > MAX_GAP_DAYS + 1:
            close_run()
        run.append(d)
    close_run()
    return trips
