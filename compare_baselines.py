#!/usr/bin/env python3
"""
compare_baselines.py — why does the same data say 20 and 69 at once?

Test B scored every day since 2002 against a pre-2002 climatology and put
2026 at percentile 20. The map scores today against the FULL record and puts
the median station at 69. Same stations, same cache, opposite conclusions.

Exactly two things differ between those calculations, and this measures both
separately instead of guessing which one wins.

  1  THE BASELINE POOL
     The map's pool contains the recent low years; test B's does not. Adding
     a run of dry years to the reference lowers the reference, so today
     ranks higher against it. This is not a bug — it is the reference-period
     choice every hydrological service has to make. USGS percentiles rest on
     a stated period. Here we just make the choice visible: today's reading
     scored against four different pools, side by side.

  2  THE TIME OF YEAR
     Test B averages all 365 days. The map looks only at late August. A
     basin can sit below normal on the annual mean and above normal at the
     end of winter, because those are different questions. So we also score
     the late-August window year by year, against a fixed pool, and compare
     that curve to the all-days curve from test B.

If (1) dominates, the fix is to declare a reference period.
If (2) dominates, the map is right and the annual figure is a different fact.

Reads only ./cache and stations_final.geojson. No network.

    python3 compare_baselines.py
    python3 compare_baselines.py --window 10 --stations 60

Writes baseline_report.txt
"""

import argparse
import bisect
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, date, timedelta

CACHE_DIR = "cache"
CANDIDATES = ["stations_final.geojson", "stations_current.geojson"]

# name -> (first year inclusive, last year inclusive or None for open)
BASELINES = [
    ("full record",   None, None),
    ("pre-2002",      None, 2001),
    ("1991-2020",     1991, 2020),
    ("last 30 yr",    1996, None),
]


def load_cached(sid):
    path = os.path.join(CACHE_DIR, f"{sid}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (ValueError, OSError):
        return []
    out = []
    for d, v in raw:
        try:
            out.append((datetime.strptime(d, "%Y-%m-%d").date(), float(v)))
        except (ValueError, TypeError):
            continue
    return sorted(out)


def doy(d):
    n = d.timetuple().tm_yday
    leap = d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)
    if d.month == 2 and d.day == 29:
        return 59
    if d.month > 2 and leap:
        return n - 1
    return n


def build_pool(pairs, window=7, min_years=8):
    bucket = defaultdict(list)
    years = defaultdict(set)
    for d, v in pairs:
        n = doy(d)
        for off in range(-window, window + 1):
            k = (n + off - 1) % 365 + 1
            bucket[k].append(v)
            years[k].add(d.year)
    return {k: sorted(vs) for k, vs in bucket.items()
            if len(years[k]) >= min_years}


def rank(pool, d, v):
    s = pool.get(doy(d))
    if not s or len(s) < 20:
        return None
    lo = bisect.bisect_left(s, v)
    hi = bisect.bisect_right(s, v)
    return round(100.0 * (lo + hi) / 2.0 / len(s), 1)


def subset(pairs, y0, y1):
    return [(d, v) for d, v in pairs
            if (y0 is None or d.year >= y0) and (y1 is None or d.year <= y1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=7,
                    help="+/- days pooled around each day of year")
    ap.add_argument("--season-window", type=int, default=15,
                    help="+/- days around today's date for the seasonal test")
    ap.add_argument("--stations", type=int, default=0)
    ap.add_argument("--today", default=None)
    args = ap.parse_args()

    today = (datetime.strptime(args.today, "%Y-%m-%d").date()
             if args.today else date.today())

    src = next((c for c in CANDIDATES if os.path.exists(c)), None)
    if not src:
        print("no stations_final.geojson here")
        return

    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say(f"Baseline comparison — {datetime.now():%Y-%m-%d %H:%M}")
    say("=" * 68)
    say(f"  source: {src}   reference date: {today}")

    with open(src, encoding="utf-8") as fh:
        feats = json.load(fh)["features"]
    props = [f["properties"] for f in feats]
    todo = props[:args.stations] if args.stations else props
    say(f"  {len(todo)} stations")

    # ---------------------------------------------------- test 1
    by_baseline = defaultdict(list)
    season_by_year = defaultdict(list)
    annual_by_year = defaultdict(list)
    per_station = []
    skipped = 0

    target_doy = doy(today)

    def near_season(d):
        gap = abs(doy(d) - target_doy)
        return min(gap, 365 - gap) <= args.season_window

    say(f"\n[1] Today's reading against four baselines, and the same "
        f"stations' late-{today:%B} history")

    for i, p in enumerate(todo, 1):
        sid = str(p.get("series_id"))
        series = load_cached(sid)
        if len(series) < 365 * 8:
            skipped += 1
            continue
        last_d, last_v = series[-1]
        if (today - last_d).days > 30:
            skipped += 1
            continue

        row = {"name": str(p.get("station") or sid), "sid": sid}
        ok = False
        for name, y0, y1 in BASELINES:
            sub = subset(series, y0, y1)
            if len(sub) < 365 * 8:
                row[name] = None
                continue
            r = rank(build_pool(sub, args.window), last_d, last_v)
            row[name] = r
            if r is not None:
                by_baseline[name].append(r)
                ok = True
        if ok:
            per_station.append(row)

        # seasonal vs annual, both against the SAME fixed pre-2002 pool
        old = subset(series, None, 2001)
        if len(old) >= 365 * 8:
            pool = build_pool(old, args.window)
            s_by, a_by = defaultdict(list), defaultdict(list)
            for d, v in series:
                if d.year < 2002:
                    continue
                r = rank(pool, d, v)
                if r is None:
                    continue
                a_by[d.year].append(r)
                if near_season(d):
                    s_by[d.year].append(r)
            for y, rs in a_by.items():
                if len(rs) >= 120:
                    annual_by_year[y].append(statistics.median(rs))
            for y, rs in s_by.items():
                if len(rs) >= 15:
                    season_by_year[y].append(statistics.median(rs))

        if i % 25 == 0:
            print(f"      {i}/{len(todo)}...")

    say(f"    used {len(per_station)} stations, skipped {skipped}")

    say("")
    say(f"    {'baseline':<16}{'n':>6}{'median pct':>13}{'above-ish':>12}")
    for name, _, _ in BASELINES:
        v = by_baseline.get(name)
        if not v:
            continue
        ab = 100 * sum(1 for x in v if x >= 75) // len(v)
        say(f"    {name:<16}{len(v):>6}{statistics.median(v):>13.1f}{str(ab) + '%':>12}")
    say("")
    say("    If these four differ a lot, the reference period is doing the")
    say("    work and it has to be declared on the map.")

    # ---------------------------------------------------- test 2
    say(f"\n[2] Same fixed pre-2002 baseline: all days vs the "
        f"{today:%B} window only")
    if season_by_year:
        say("")
        say(f"    {'year':<8}{'all days':>11}{'this season':>14}{'gap':>8}")
        for y in sorted(season_by_year):
            s = season_by_year[y]
            a = annual_by_year.get(y)
            if len(s) < 5 or not a or len(a) < 5:
                continue
            ms, ma = statistics.median(s), statistics.median(a)
            say(f"    {y:<8}{ma:>11.1f}{ms:>14.1f}{ms - ma:>+8.1f}")
        say("")
        say("    A consistently positive gap means late winter runs higher")
        say("    than the annual average at these gauges — so 'below normal")
        say("    for the year' and 'above normal today' can both be true.")
    else:
        say("    not enough stations with a pre-2002 record for this test")

    # ------------------------------------------------- worst movers
    movers = [r for r in per_station
              if r.get("full record") is not None and r.get("pre-2002") is not None]
    movers.sort(key=lambda r: abs(r["full record"] - r["pre-2002"]), reverse=True)
    if movers:
        say("\n[3] Stations the baseline choice moves the most")
        say(f"    {'station':<32}{'full':>8}{'pre-02':>9}{'1991-2020':>12}")
        for r in movers[:12]:
            f = r["full record"]
            p2 = r["pre-2002"]
            n = r.get("1991-2020")
            say(f"    {r['name'][:30]:<32}{f:>8.1f}{p2:>9.1f}"
                f"{('-' if n is None else f'{n:.1f}'):>12}")

    with open("baseline_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\nWrote baseline_report.txt")


if __name__ == "__main__":
    main()
