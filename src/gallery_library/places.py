"""Place names for coordinates, worked out offline.

The nearest town from GeoNames' list of towns with 5,000 or more people
(geodata/cities5000.tsv.gz, CC BY 4.0). Nothing is sent anywhere: naming a trip
must not tell a web service where the user has been.
"""

from __future__ import annotations

import gzip
import math
import threading
from pathlib import Path

DATA = Path(__file__).with_name("geodata") / "cities5000.tsv.gz"
NEARBY_KM = 12.0        # a bigger town this close wins over the nearest village
_FAR_KM = 120.0         # further than this from any town (open sea): no name

_lock = threading.Lock()
_towns: dict | None = None   # (int lat, int lng) -> [(lat, lng, name, country, population)]


def _load() -> dict:
    global _towns
    with _lock:
        if _towns is None:
            grid: dict = {}
            with gzip.open(DATA, "rt", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("#"):
                        continue
                    country, name, lat, lng, population = line.rstrip("\n").split("\t")
                    lat, lng = float(lat), float(lng)
                    grid.setdefault((math.floor(lat), math.floor(lng)), []).append(
                        (lat, lng, name, country, int(population)))
            _towns = grid
        return _towns


def _km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def nearest_town(lat: float, lng: float) -> dict | None:
    """{"name", "country" (ISO code), "km"} of the town a spot belongs to, or None at sea.

    The biggest town within NEARBY_KM, so a photo in a suburb is named after
    the city rather than the suburb; otherwise simply the nearest town.
    """
    grid = _load()
    cell_lat, cell_lng = math.floor(lat), math.floor(lng)
    candidates = []
    for d_lat in (-1, 0, 1):
        for d_lng in (-1, 0, 1):
            wrapped = (cell_lng + d_lng + 180) % 360 - 180
            for town in grid.get((cell_lat + d_lat, wrapped), ()):
                candidates.append((_km(lat, lng, town[0], town[1]), town))
    if not candidates:
        return None
    nearby = [c for c in candidates if c[0] <= NEARBY_KM]
    km, town = max(nearby, key=lambda c: c[1][4]) if nearby else min(candidates, key=lambda c: c[0])
    if km > _FAR_KM:
        return None
    return {"name": town[2], "country": town[3], "km": round(km, 1)}
