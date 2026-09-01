#!/usr/bin/env python3
"""
region_check.py — did the Patagonia/NOA skew survive the cleanup?

The earlier breakdown (Patagonia 75, NOA 75) came from the 399-station file,
before the bad splices and duplicate series were removed. Misiones rain does
not reach Patagonia, so if those regions are still high, something regional
is still wrong. If they fell back toward 50, they were broken series.

    python3 region_check.py
"""

import json
import os
import statistics
from collections import defaultdict

SRC = next((c for c in ["stations_published.geojson", "stations_final.geojson"]
            if os.path.exists(c)), None)


def region(lon, lat):
    if lat < -40:
        return "Patagonia"
    if lon > -60 and lat > -34:
        return "Litoral / NEA"
    if lon < -64 and lat > -31:
        return "NOA"
    if lon < -64:
        return "Cuyo"
    return "Pampa / centro"


def main():
    if not SRC:
        print("no stations file here")
        return
    with open(SRC, encoding="utf-8") as fh:
        feats = json.load(fh)["features"]

    print(f"source: {SRC}")
    groups = defaultdict(list)
    for f in feats:
        p = f["properties"]
        if p.get("publish") == "reading_only":
            continue
        if not isinstance(p.get("percentile"), (int, float)):
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        groups[region(lon, lat)].append((p["percentile"], p.get("station")))

    print(f"\n{'region':<18}{'n':>5}{'median':>9}{'above-ish':>12}   was (399-file)")
    prior = {"Litoral / NEA": "72 / 44%", "Pampa / centro": "64 / 21%",
             "NOA": "75 / 50%", "Patagonia": "76 / 54%"}
    for k in sorted(groups, key=lambda k: -len(groups[k])):
        v = [x[0] for x in groups[k]]
        ab = 100 * sum(1 for x in v if x >= 75) // len(v)
        print(f"{k:<18}{len(v):>5}{statistics.median(v):>9.1f}{str(ab) + '%':>12}   "
              f"{prior.get(k, '-')}")

    for k in ("Patagonia", "NOA"):
        if groups.get(k):
            print(f"\n{k}:")
            for pct, name in sorted(groups[k], reverse=True):
                print(f"   {str(name)[:34]:<34}{pct:>7.1f}")


if __name__ == "__main__":
    main()
