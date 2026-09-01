#!/usr/bin/env python3
"""
propagate.py — decide which reaches a gauge is allowed to speak for.

A gauge measures one point. The map colours a network. Something has to
decide how far each reading travels, and that decision is the difference
between a map that means something and a pretty picture.

THE RULE: a gauge speaks until the river stops being the same river.

Walking downstream from a gauged reach, we keep colouring until one of:

  * another gauge is reached          — it knows better from there on
  * catchment area grows past --grow  — a major tributary joined, so the
                                        flow is no longer what we measured
  * --max-km of river is covered      — a hard leash

Walking upstream, the same, mirrored: stop when catchment falls below
1/--grow of the gauge's own. That ratio test does something neat for free:
at a confluence the tributary's area drops off a cliff while the main stem's
barely changes, so the colour follows the main stem upstream on its own,
without needing river names we do not have.

Reaches nobody can speak for stay uncoloured. On a country with 128 usable
gauges that will be most of them, and saying so is the point — the US map
this is modelled on has 4,693 gauges for a similar area.

Conflicts go to the nearer gauge, measured along the river, not in a
straight line.

    python3 propagate.py
    python3 propagate.py --grow 2.0 --max-km 400

Writes reach_colours.json, propagate_report.txt
"""

import argparse
import json
import os
from collections import defaultdict, Counter, deque

NETWORK = "network.json"
SNAPPED = "stations_snapped.json"

ORDER = ["much_below", "below", "normal", "above", "much_above"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grow", type=float, default=1.5,
                    help="stop when catchment grows past this multiple of "
                         "the gauge's own (downstream) or falls below its "
                         "reciprocal (upstream)")
    ap.add_argument("--max-km", type=float, default=300.0,
                    help="hard limit on how far one gauge may speak")
    ap.add_argument("--up-fraction", type=float, default=0.5,
                    help="upstream leash as a fraction of --max-km")
    args = ap.parse_args()

    for f in (NETWORK, SNAPPED):
        if not os.path.exists(f):
            raise SystemExit(f"{f} not found")

    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say(f"Propagation — grow {args.grow}x, leash {args.max_km} km")
    say("=" * 68)

    net = json.load(open(NETWORK, encoding="utf-8"))
    reaches = {r["id"]: r for r in net["reaches"]}
    say(f"  {len(reaches):,} reaches, min order {net.get('min_order')}")

    upstream = defaultdict(list)
    for r in reaches.values():
        if r["next"] and r["next"] in reaches:
            upstream[r["next"]].append(r["id"])

    # reach length: HydroRIVERS LENGTH_KM was not carried into network.json,
    # so approximate from vertex spacing where needed
    def reach_km(r):
        pts = r["pts"]
        tot = 0.0
        for i in range(1, len(pts)):
            dx = (pts[i][0] - pts[i - 1][0]) * 96.0
            dy = (pts[i][1] - pts[i - 1][1]) * 110.57
            tot += (dx * dx + dy * dy) ** 0.5
        return max(tot, 0.1)

    stations = json.load(open(SNAPPED, encoding="utf-8"))
    gauges = [s for s in stations
              if s.get("routed") and s.get("reach") in reaches
              and s.get("class") and s.get("publish") == "colour"]
    say(f"  {len(gauges)} gauges usable for colour "
        f"(routed, published, classified)")

    gauged_reach = {g["reach"]: g for g in gauges}

    # best[reach] = (distance_km, gauge)
    best = {}

    for g in gauges:
        start = g["reach"]
        start_up = max(reaches[start].get("up_skm") or 1.0, 1.0)

        # downstream
        q = deque([(start, 0.0)])
        seen = {start}
        while q:
            rid, dist = q.popleft()
            r = reaches[rid]
            cur = best.get(rid)
            if cur is None or dist < cur[0]:
                best[rid] = (dist, g)
            nxt = r["next"]
            if not nxt or nxt not in reaches or nxt in seen:
                continue
            nd = dist + reach_km(r)
            if nd > args.max_km:
                continue
            if nxt in gauged_reach:
                continue
            if (reaches[nxt].get("up_skm") or 0) > start_up * args.grow:
                continue
            seen.add(nxt)
            q.append((nxt, nd))

        # upstream, shorter leash, follows the main stem via the area test
        q = deque([(start, 0.0)])
        seen = {start}
        limit = args.max_km * args.up_fraction
        while q:
            rid, dist = q.popleft()
            r = reaches[rid]
            for up in upstream.get(rid, ()):
                if up in seen or up in gauged_reach:
                    continue
                nd = dist + reach_km(reaches[up])
                if nd > limit:
                    continue
                if (reaches[up].get("up_skm") or 0) < start_up / args.grow:
                    continue
                cur = best.get(up)
                if cur is None or nd < cur[0]:
                    best[up] = (nd, g)
                seen.add(up)
                q.append((up, nd))

    out = {}
    for rid, (dist, g) in best.items():
        out[str(rid)] = {
            "class": g["class"],
            "pct": g.get("percentile"),
            "src": g.get("station"),
            "sid": g.get("series_id"),
            "km": round(dist, 1),
        }

    say(f"\n[1] Coverage")
    say(f"    {len(out):,} of {len(reaches):,} reaches coloured "
        f"({100 * len(out) // len(reaches)}%)")
    km_col = sum(reach_km(reaches[int(k)]) for k in out)
    km_all = sum(reach_km(r) for r in reaches.values())
    say(f"    {km_col:,.0f} km of river coloured out of {km_all:,.0f} km "
        f"({100 * km_col / km_all:.0f}%)")

    by_ord = Counter(reaches[int(k)]["ord"] for k in out)
    tot_ord = Counter(r["ord"] for r in reaches.values())
    say("\n    coverage by Strahler order (big rivers should score high)")
    for o in sorted(tot_ord, reverse=True):
        c = by_ord.get(o, 0)
        say(f"      order {o}: {c:>6,} / {tot_ord[o]:>6,}  "
            f"({100 * c // tot_ord[o]:>3}%)")

    say("\n[2] Classes on the network")
    cls = Counter(v["class"] for v in out.values())
    tot = sum(cls.values()) or 1
    for k in ORDER:
        say(f"    {k:<12}{cls.get(k, 0):>7,}  ({100 * cls.get(k, 0) // tot}%)")

    say("\n[3] Reach of each gauge")
    per = Counter(v["src"] for v in out.values())
    say(f"    median reaches per gauge: "
        f"{sorted(per.values())[len(per) // 2] if per else 0}")
    say("    widest:")
    for name, c in per.most_common(10):
        say(f"      {str(name)[:34]:<36}{c:>6,} reaches")
    lonely = [g["station"] for g in gauges
              if per.get(g["station"], 0) <= 1]
    say(f"    gauges colouring only their own reach: {len(lonely)}")

    ds = sorted(v["km"] for v in out.values())
    if ds:
        say(f"\n    distance from gauge: median {ds[len(ds) // 2]:.0f} km, "
            f"p90 {ds[int(len(ds) * 0.9)]:.0f} km, max {ds[-1]:.0f} km")

    with open("reach_colours.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    with open("propagate_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    say(f"\nWrote reach_colours.json "
        f"({os.path.getsize('reach_colours.json') / 1e6:.1f} MB), "
        f"propagate_report.txt")


if __name__ == "__main__":
    main()
