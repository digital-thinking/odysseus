"""Estimated locations: a photo without one borrows the spot of a photo taken at the same time.

A phone records where a photo was taken; an older camera, a second phone or a
scanner does not. A photo without a location takes the spot of a photo that has
one when

  - that photo was taken at most NEAR before or after it, or
  - the photos just before and just after it, each at most BRACKET away in
    time, were taken within AGREE_KM of each other (you stayed in one place).

The nearer one in time gives the spot. Only locations read from the files are
borrowed, never other estimates, so an estimate cannot drift along a chain.
"""

from __future__ import annotations

import bisect
from datetime import timedelta

from src.gallery_library.trips import km_between

NEAR = timedelta(minutes=15)
BRACKET = timedelta(minutes=60)
AGREE_KM = 1.0
SOURCE = "time"     # GalleryImage.location_source of an estimated location


def estimate(located, missing) -> dict:
    """{photo id: (lat, lng)} for the photos of `missing` that can be placed.

    located: (taken_at, lat, lng) of photos with a location from their file
             (lat/lng as stored, text); missing: (id, taken_at) of photos without.
    """
    spots = sorted((when, lat, lng) for when, lat, lng in located if when is not None)
    times = [s[0] for s in spots]
    placed = {}
    for image_id, when in missing:
        if when is None or not spots:
            continue
        i = bisect.bisect_left(times, when)
        before = spots[i - 1] if i > 0 else None
        after = spots[i] if i < len(spots) else None
        gaps = [(when - s[0], s) for s in (before,) if s] + [(s[0] - when, s) for s in (after,) if s]
        gap, nearest = min(gaps, key=lambda g: g[0])
        ok = gap <= NEAR
        if not ok and before and after and when - before[0] <= BRACKET and after[0] - when <= BRACKET:
            try:
                ok = km_between(float(before[1]), float(before[2]), float(after[1]), float(after[2])) <= AGREE_KM
            except ValueError:
                ok = False
        if ok:
            placed[image_id] = (nearest[1], nearest[2])
    return placed
