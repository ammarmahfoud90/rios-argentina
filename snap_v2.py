#!/usr/bin/env python3
"""
snap_v2.py — attach gauges to reaches properly, and admit where we can't.

Reads network.json (already built) rather than rescanning the shapefile, so
this runs in seconds and can be re-tuned freely.

WHAT WAS WRONG IN V1

  Nearest-line-wins put gauges on side creeks. The v1 fallback preferred the
  bigger river but only inside a window pinned to the nearest candidate, so
  a creek at 0.5 km still hid a main stem at 2.5 km.

  Now: score = log10(upstream area) - distance / --half-km. Size wins, but
  each --half-km of extra distance costs a factor of ten in catchment. On
  the real network this moved Esquina from a 27,000 km2 tributary to the
  Parana at 2.2M, and Puerto Iguazu from 68,000 to 834,000, while leaving
  genuine small-stream gauges alone.

WHAT CANNOT BE FIXED BY TUNING

  Below Diamante the Parana is a braided delta. HydroRIVERS carries one
  order-9 centerline through one channel; the port gauges are on others.
  Measured: San Nicolas 10 km from the nearest order-9 reach, Ramallo 21,
  San Pedro 29, Villa Paranacito 21. Any radius wide enough to reach it is
  wide enough to grab the wrong river.

  The Rio de la Plata is not in HydroRIVERS at all — it is a water body, not
  a river. Buenos Aires, La Plata, Olivos and Martin Garcia have nothing
  near them but order-4 creeks 9-15 km away.

  So those gauges are marked routed=false. They keep their reading and their
  colour as a point on the map, but no reach is coloured from them and the
  animation does not run through them. Same rule as tier 3: show the number,
  withhold the claim we cannot support.

    python3 snap_v2.py
    python3 snap_v2.py --half-km 3 --radius 10

Writes stations_snapped.json (overwrites v1), snap_report.txt
"""

import argparse
import json
import math
import os
import statistics
from collections import Counter, defaultdict

NETWORK = "network.json"
STATIONS = ["stations_published.geojson", "stations_final.geojson"]

# Rio de la Plata estuary: no river centerline exists here at all
PLATA_BOX = (-59.0, -35.6, -55.0, -33.9)   # lon0, lat0, lon1, lat1


def in_box(lon, lat, box):
    return box[0] <= lon <= box[2] and box[1] <= lat <= box[3]


def point_seg_km(plon, plat, a, b):
    kx = 111.32 * math.cos(math.radians(plat))
    ky = 110.57
    px, py = plon * kx, plat * ky
    ax, ay = a[0] * kx, a[1] * ky
    bx, by = b[0] * kx, b[1] * ky
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    if d2 < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / d2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--radius", type=float, default=8.0,
                    help="search radius in km")
    ap.add_argument("--half-km", type=float, default=2.0,
                    help="every this many km of extra distance costs a "
                         "factor of ten in catchment area")
    ap.add_argument("--route-max-km", type=float, default=5.0,
                    help="beyond this the gauge is shown but not routed")
    args = ap.parse_args()

    if not os.path.exists(NETWORK):
        raise SystemExit("network.json not found — run prep_network.py first")
    src = next((c for c in STATIONS if os.path.exists(c)), None)
    if not src:
        raise SystemExit("no stations geojson found")

    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say(f"Snap v2 — radius {args.radius} km, half {args.half_km} km")
    say("=" * 68)

    net = json.load(open(NETWORK, encoding="utf-8"))
    reaches = net["reaches"]
    say(f"  {len(reaches):,} reaches, min order {net.get('min_order')}")

    CELL = 0.25
    grid = defaultdict(list)
    for i, r in enumerate(reaches):
        for x, y in r["pts"]:
            grid[(int(math.floor(y / CELL)), int(math.floor(x / CELL)))].append(i)
    for k in grid:
        grid[k] = list(set(grid[k]))

    feats = json.load(open(src, encoding="utf-8"))["features"]
    say(f"  {len(feats)} gauges from {src}\n")

    span = int(math.ceil(args.radius / (CELL * 100))) + 2
    out = []
    routed = 0
    dists = []
    unrouted = []

    for f in feats:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        gy, gx = int(math.floor(lat / CELL)), int(math.floor(lon / CELL))
        cand = set()
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                cand.update(grid.get((gy + dy, gx + dx), ()))

        best = None
        for i in cand:
            r = reaches[i]
            pts = r["pts"]
            d = min(point_seg_km(lon, lat, pts[k - 1], pts[k])
                    for k in range(1, len(pts)))
            if d > args.radius:
                continue
            score = math.log10(max(r["up_skm"], 1.0)) - d / args.half_km
            if best is None or score > best[0]:
                best = (score, d, r)

        rec = {
            "series_id": p.get("series_id"), "station": p.get("station"),
            "lon": lon, "lat": lat,
            "percentile": p.get("percentile"), "class": p.get("class"),
            "publish": p.get("publish"),
            "years_of_record": p.get("years_of_record"),
        }

        if best is None:
            rec.update(routed=False, reason="no reach within radius",
                       reach=None, snap_km=None)
            unrouted.append((p.get("station"), lon, lat, "nothing in radius"))
        else:
            _, d, r = best
            estuary = in_box(lon, lat, PLATA_BOX)
            far = d > args.route_max_km
            if estuary or far:
                why = "Plata estuary — not a river in HydroRIVERS" if estuary \
                      else f"nearest reach {d:.1f} km away (delta channel)"
                rec.update(routed=False, reason=why, reach=r["id"],
                           snap_km=round(d, 3), up_skm=r["up_skm"],
                           ord=r["ord"])
                unrouted.append((p.get("station"), lon, lat, why))
            else:
                rec.update(routed=True, reason=None, reach=r["id"],
                           snap_km=round(d, 3), up_skm=r["up_skm"],
                           ord=r["ord"], dis_av=r["dis"])
                routed += 1
                dists.append(d)
        out.append(rec)

    say(f"[1] Routed {routed} of {len(feats)}")
    if dists:
        ds = sorted(dists)
        say(f"    distance km: median {statistics.median(ds):.2f}, "
            f"p90 {ds[int(len(ds) * 0.9)]:.2f}, max {ds[-1]:.2f}")
        say(f"    within 500 m: {sum(1 for d in ds if d <= 0.5)}, "
            f"within 1 km: {sum(1 for d in ds if d <= 1.0)}")
        orders = Counter(r["ord"] for r in out if r.get("routed"))
        say("    by Strahler order of the reach they landed on:")
        for o in sorted(orders, reverse=True):
            say(f"      order {o}: {orders[o]}")
        dup = Counter(r["reach"] for r in out if r.get("routed"))
        say(f"    reaches with more than one gauge: "
            f"{sum(1 for c in dup.values() if c > 1)}")

    say(f"\n[2] Shown but not routed ({len(unrouted)})")
    for st, lon, lat, why in unrouted:
        say(f"    {str(st)[:30]:<32} {why}")

    say("\n    These keep their reading and their colour as a point. No reach")
    say("    is coloured from them and the animation does not pass through")
    say("    them, because we cannot say which channel they belong to.")

    with open("stations_snapped.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    with open("snap_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\nWrote stations_snapped.json, snap_report.txt")


if __name__ == "__main__":
    main()
