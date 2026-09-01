#!/usr/bin/env python3
"""
prep_network.py — turn HydroRIVERS into the map's skeleton.

Three jobs:

  1  CLIP AND THIN
     1.6M South American reaches down to something a browser can hold.
     Filtering by Strahler order is safe: order never decreases downstream,
     so keeping order >= k keeps every reach below a kept reach. The network
     stays connected downstream, which is what the animation walks.

  2  TOPOLOGY
     NEXT_DOWN gives each reach the one below it. Built into a dict, checked
     for cycles and for links that point at reaches we dropped (those become
     terminals). This is the chain the colour travels along.

  3  SNAP THE GAUGES
     Nearest reach to each station — but nearest is not always right. A
     gauge on the Parana sitting 200 m from a tiny creek must not snap to
     the creek. So among candidates within --snap-km we prefer the biggest
     river (largest upstream area), not the closest line, unless the closest
     is dramatically nearer.

     Then it CHECKS itself: a station whose name says Parana should land on
     a reach draining hundreds of thousands of km2. Mismatches get printed
     rather than silently accepted.

    pip install pyshp
    python3 prep_network.py
    python3 prep_network.py --min-order 3 --snap-km 5

Writes network.json, stations_snapped.json, network_report.txt
"""

import argparse
import glob
import json
import math
import os
import statistics
from collections import defaultdict, Counter

try:
    import shapefile
except ImportError:
    raise SystemExit("need pyshp:  pip install pyshp")

AR_BBOX = (-74.0, -56.0, -52.0, -20.0)
STATIONS = ["stations_published.geojson", "stations_final.geojson"]

# station/river name -> smallest upstream area we would believe, km2
EXPECT_UPLAND = {
    "parana": 100000, "uruguay": 50000, "paraguay": 100000,
    "iguazu": 20000, "bermejo": 20000, "pilcomayo": 40000,
    "salado": 10000, "negro": 20000, "chubut": 10000,
    "colorado": 10000, "santa cruz": 10000, "dulce": 10000,
}


# ------------------------------------------------------------ geometry

def haversine_km(lon1, lat1, lon2, lat2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def point_seg_km(plon, plat, alon, alat, blon, blat):
    """Distance from point to segment, in local flat approximation."""
    kx = 111.32 * math.cos(math.radians(plat))
    ky = 110.57
    px, py = plon * kx, plat * ky
    ax, ay = alon * kx, alat * ky
    bx, by = blon * kx, blat * ky
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    if d2 < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / d2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def line_dist_km(plon, plat, pts):
    best = float("inf")
    for i in range(1, len(pts)):
        d = point_seg_km(plon, plat, pts[i - 1][0], pts[i - 1][1],
                         pts[i][0], pts[i][1])
        if d < best:
            best = d
    return best


def simplify(pts, tol_deg):
    """Douglas-Peucker, iterative, to keep the payload small."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = pts[i]
        bx, by = pts[j]
        worst, wi = -1.0, -1
        for k in range(i + 1, j):
            px, py = pts[k]
            dx, dy = bx - ax, by - ay
            d2 = dx * dx + dy * dy
            if d2 < 1e-18:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / d2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > worst:
                worst, wi = d, k
        if worst > tol_deg:
            keep[wi] = True
            stack.append((i, wi))
            stack.append((wi, j))
    return [p for p, k in zip(pts, keep) if k]


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-order", type=int, default=4,
                    help="keep Strahler order >= this (4 ~ 31k reaches)")
    ap.add_argument("--snap-km", type=float, default=3.0,
                    help="search radius for attaching a gauge to a reach")
    ap.add_argument("--simplify-deg", type=float, default=0.002,
                    help="~200 m; set 0 to keep full geometry")
    args = ap.parse_args()

    hits = glob.glob("**/HydroRIVERS_v10_sa.shp", recursive=True)
    if not hits:
        raise SystemExit("HydroRIVERS_v10_sa.shp not found")
    path = hits[0]

    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say(f"Network prep — min order {args.min_order}, snap {args.snap_km} km")
    say("=" * 68)

    r = shapefile.Reader(path)
    names = [f[0] for f in r.fields if f[0] != "DeletionFlag"]
    ix = {n: i for i, n in enumerate(names)}
    lon0, lat0, lon1, lat1 = AR_BBOX

    reaches = {}
    n_seen = 0
    for sr in r.iterShapeRecords():
        n_seen += 1
        rec = sr.record
        if rec[ix["ORD_STRA"]] < args.min_order:
            continue
        b = sr.shape.bbox
        if b[2] < lon0 or b[0] > lon1 or b[3] < lat0 or b[1] > lat1:
            continue
        pts = [(round(x, 5), round(y, 5)) for x, y in sr.shape.points]
        if len(pts) < 2:
            continue
        if args.simplify_deg > 0:
            pts = simplify(pts, args.simplify_deg)
        reaches[rec[ix["HYRIV_ID"]]] = {
            "id": rec[ix["HYRIV_ID"]],
            "next": rec[ix["NEXT_DOWN"]],
            "main": rec[ix["MAIN_RIV"]],
            "ord": rec[ix["ORD_STRA"]],
            "len": rec[ix["LENGTH_KM"]],
            "up_skm": rec[ix["UPLAND_SKM"]],
            "dis": rec[ix["DIS_AV_CMS"]],
            "pts": pts,
        }
        if n_seen % 400000 == 0:
            print(f"      scanned {n_seen:,}...")

    say(f"\n[1] Clip and thin")
    say(f"    scanned {n_seen:,} reaches, kept {len(reaches):,}")
    pts_tot = sum(len(v['pts']) for v in reaches.values())
    say(f"    {pts_tot:,} vertices after simplification")

    # ------------------------------------------------------ topology
    say(f"\n[2] Topology")
    outside = 0
    for v in reaches.values():
        if v["next"] and v["next"] not in reaches:
            v["next"] = 0
            outside += 1
    terminals = sum(1 for v in reaches.values() if not v["next"])
    say(f"    {terminals:,} terminal reaches "
        f"({outside:,} of them because the next reach fell outside the clip)")

    upstream = defaultdict(list)
    for v in reaches.values():
        if v["next"]:
            upstream[v["next"]].append(v["id"])
    say(f"    {len(upstream):,} reaches have something draining into them")

    # cycle / depth check
    depth = {}
    bad = 0
    longest = 0
    for rid in reaches:
        chain, cur = [], rid
        while cur and cur in reaches and cur not in depth:
            if cur in chain:
                bad += 1
                break
            chain.append(cur)
            cur = reaches[cur]["next"]
        base = depth.get(cur, 0)
        for k, c in enumerate(reversed(chain)):
            depth[c] = base + k + 1
        if chain:
            longest = max(longest, base + len(chain))
    say(f"    longest downstream chain: {longest:,} reaches")
    say(f"    cycles detected: {bad}   (must be 0)")

    # ---------------------------------------------------- snapping
    src = next((c for c in STATIONS if os.path.exists(c)), None)
    if not src:
        say("\n[3] no stations file found — skipping snap")
        stations = []
    else:
        with open(src, encoding="utf-8") as fh:
            stations = json.load(fh)["features"]
        say(f"\n[3] Snapping {len(stations)} gauges from {src}")

    # coarse spatial index
    CELL = 0.25
    grid = defaultdict(list)
    for v in reaches.values():
        cells = set()
        for x, y in v["pts"]:
            cells.add((int(math.floor(y / CELL)), int(math.floor(x / CELL))))
        for c in cells:
            grid[c].append(v["id"])

    snapped = []
    unsnapped = []
    dists = []
    warn = []

    span = int(math.ceil(args.snap_km / 25.0)) + 1
    for f in stations:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        gy, gx = int(math.floor(lat / CELL)), int(math.floor(lon / CELL))
        cand = set()
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                cand.update(grid.get((gy + dy, gx + dx), ()))

        best = []
        for rid in cand:
            v = reaches[rid]
            d = line_dist_km(lon, lat, v["pts"])
            if d <= args.snap_km:
                best.append((d, v))
        if not best:
            unsnapped.append((p.get("station"), lon, lat))
            continue

        # prefer the bigger river unless a much closer one exists
        best.sort(key=lambda t: t[0])
        nearest_d = best[0][0]
        pool = [(d, v) for d, v in best if d <= max(nearest_d * 3.0, nearest_d + 0.5)]
        pool.sort(key=lambda t: (-t[1]["up_skm"], t[0]))
        d, v = pool[0]
        dists.append(d)

        name = f"{p.get('station') or ''} {p.get('river') or ''}".lower()
        for key, floor in EXPECT_UPLAND.items():
            if key in name and v["up_skm"] < floor:
                warn.append((p.get("station"), key, v["up_skm"], floor, d))
                break

        snapped.append({
            "series_id": p.get("series_id"), "station": p.get("station"),
            "reach": v["id"], "snap_km": round(d, 3),
            "ord": v["ord"], "up_skm": v["up_skm"], "dis_av": v["dis"],
            "percentile": p.get("percentile"), "class": p.get("class"),
            "publish": p.get("publish"), "lon": lon, "lat": lat,
        })

    if dists:
        ds = sorted(dists)
        say(f"    snapped {len(snapped)}, failed {len(unsnapped)}")
        say(f"    distance km: median {statistics.median(ds):.2f}, "
            f"p90 {ds[int(len(ds) * 0.9)]:.2f}, max {ds[-1]:.2f}")
        say(f"    within 500 m: {sum(1 for d in ds if d <= 0.5)}, "
            f"within 1 km: {sum(1 for d in ds if d <= 1.0)}")
        per = Counter(s["reach"] for s in snapped)
        dupes = [k for k, c in per.items() if c > 1]
        say(f"    reaches carrying more than one gauge: {len(dupes)}")

    if warn:
        say(f"\n    NAME vs CATCHMENT MISMATCH ({len(warn)}) — likely snapped "
            f"to the wrong watercourse:")
        for st, key, got, floor, d in warn[:15]:
            say(f"      {str(st)[:32]:<32} '{key}' expects >{floor:,} km2, "
                f"got {got:,.0f} at {d:.2f} km")

    if unsnapped:
        say(f"\n    NO REACH WITHIN {args.snap_km} km ({len(unsnapped)}):")
        for st, lon, lat in unsnapped[:15]:
            say(f"      {str(st)[:32]:<32} {lat:.3f}, {lon:.3f}")

    # ------------------------------------------------------- write
    out = {
        "min_order": args.min_order,
        "reaches": [
            {"id": v["id"], "next": v["next"], "ord": v["ord"],
             "up_skm": v["up_skm"], "dis": v["dis"], "pts": v["pts"]}
            for v in reaches.values()
        ],
    }
    with open("network.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    with open("stations_snapped.json", "w", encoding="utf-8") as fh:
        json.dump(snapped, fh, ensure_ascii=False, separators=(",", ":"))
    with open("network_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    mb = os.path.getsize("network.json") / 1e6
    say(f"\nWrote network.json ({mb:.1f} MB), stations_snapped.json, "
        f"network_report.txt")
    if mb > 60:
        say("    that is large for a browser — rerun with --min-order 5 "
            "or a coarser --simplify-deg")


if __name__ == "__main__":
    main()
