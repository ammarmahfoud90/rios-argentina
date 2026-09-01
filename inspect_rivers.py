#!/usr/bin/env python3
"""
inspect_rivers.py — look at the shapefile before writing anything that
depends on it. Field names get guessed wrong; that lesson already cost us
once in this project. So: print what is actually there.

    pip install pyshp
    python3 inspect_rivers.py
"""

import glob
import os
from collections import Counter

try:
    import shapefile  # pyshp
except ImportError:
    raise SystemExit("need pyshp:  pip install pyshp")

# Argentina plus a margin, so upstream reaches in Brazil/Paraguay/Bolivia
# that feed our gauges are not cut off
AR_BBOX = (-74.0, -56.0, -52.0, -20.0)   # lon_min, lat_min, lon_max, lat_max


def find_shp():
    hits = glob.glob("**/HydroRIVERS_v10_sa.shp", recursive=True)
    return hits[0] if hits else None


def main():
    path = find_shp()
    if not path:
        raise SystemExit("HydroRIVERS_v10_sa.shp not found — unzip it here")
    print(f"file: {path}  ({os.path.getsize(path) / 1e6:.0f} MB)\n")

    r = shapefile.Reader(path)
    print(f"reaches in South America: {len(r):,}")
    print(f"layer bbox: {[round(x, 2) for x in r.bbox]}\n")

    fields = [f for f in r.fields if f[0] != "DeletionFlag"]
    print("FIELDS")
    for name, typ, size, dec in fields:
        print(f"   {name:<14} {typ}{size}" + (f".{dec}" if dec else ""))
    names = [f[0] for f in fields]

    print("\nFIRST 2 RECORDS")
    for i, rec in enumerate(r.iterRecords()):
        print("   " + ", ".join(f"{n}={v}" for n, v in zip(names, rec))[:300])
        if i >= 1:
            break

    # what we need for the map, checked rather than assumed
    print("\nWHAT THE MAP NEEDS")
    for want, why in [("HYRIV_ID", "reach id"),
                      ("NEXT_DOWN", "downstream link — the animation"),
                      ("MAIN_RIV", "groups reaches into one river"),
                      ("ORD_STRA", "Strahler order — how we thin the network"),
                      ("DIS_AV_CMS", "long-term average flow"),
                      ("LENGTH_KM", "reach length")]:
        print(f"   {want:<12} {'FOUND' if want in names else 'MISSING':<8} {why}")

    lon0, lat0, lon1, lat1 = AR_BBOX
    n_box = 0
    orders = Counter()
    ord_field = "ORD_STRA" if "ORD_STRA" in names else None
    oi = names.index(ord_field) if ord_field else None

    for sr in r.iterShapeRecords():
        b = sr.shape.bbox
        if b[2] < lon0 or b[0] > lon1 or b[3] < lat0 or b[1] > lat1:
            continue
        n_box += 1
        if oi is not None:
            orders[sr.record[oi]] += 1

    print(f"\nreaches inside {AR_BBOX}: {n_box:,}")
    if orders:
        print("\nby Strahler order (cumulative if we keep >= each)")
        tot = sum(orders.values())
        run = 0
        for o in sorted(orders, reverse=True):
            run += orders[o]
            print(f"   order {o:<3} {orders[o]:>9,}   keep >={o}: {run:>9,} "
                  f"({100 * run // tot}%)")


if __name__ == "__main__":
    main()
