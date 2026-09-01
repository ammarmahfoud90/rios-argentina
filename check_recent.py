#!/usr/bin/env python3
"""
check_recent.py — is the rise real, or does it live in the last data point?

State of play: reference period ruled out (four baselines within 7 points).
Season ruled out (mean gap +1.6 over 24 years). Yet 2025 scores 26 and today
scores 67 at the same stations. So whatever is happening happened recently.

Two possibilities, and they look completely different day by day:

  REAL RISE      the percentile climbs over weeks or months. There is a
                 ramp. Every day near the end is high, not just the last.
                 External support exists: INA reported the Parana rising
                 through August on Yacyreta releases, and San Nicolas hit
                 its 2026 high on 8 August.

  LAST-POINT     the series sits near 26 until the final day or two, then
  ARTEFACT       jumps. That would mean the number the map publishes is not
                 drawn the same way as the history it is compared against —
                 a partial day, a raw sub-daily reading against a daily-mean
                 climatology, or a different aggregation on the final row.

Three views, all against the same full-record pool:

  1  median percentile per month, 2024 to now — is there a ramp?
  2  median percentile per day for the last 90 days — where is the step?
  3  today's percentile minus the percentile 7 and 30 days ago, per station

If (3) is centred near zero, the last point is fine and the rise is real.
If it is systematically positive, the final row is the problem.

Reads only ./cache and stations_final.geojson. No network.

    python3 check_recent.py
    python3 check_recent.py --stations 60

Writes recent_report.txt
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


def value_near(series_map, target, tol=3):
    """Value on target date, or the closest within tol days."""
    for off in range(0, tol + 1):
        for s in ((1, -1) if off else (1,)):
            d = target + timedelta(days=off * s)
            if d in series_map:
                return d, series_map[d]
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stations", type=int, default=0)
    ap.add_argument("--days", type=int, default=90)
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

    say(f"Recent-rise check — {datetime.now():%Y-%m-%d %H:%M}")
    say("=" * 68)

    with open(src, encoding="utf-8") as fh:
        feats = json.load(fh)["features"]
    props = [f["properties"] for f in feats]
    todo = props[:args.stations] if args.stations else props
    say(f"  {len(todo)} stations, reference date {today}")

    by_month = defaultdict(list)
    by_day = defaultdict(list)
    deltas7, deltas30 = [], []
    movers = []
    used = 0

    for i, p in enumerate(todo, 1):
        sid = str(p.get("series_id"))
        name = str(p.get("station") or sid)
        series = load_cached(sid)
        if len(series) < 365 * 8:
            continue
        if (today - series[-1][0]).days > 30:
            continue
        pool = build_pool(series)
        if not pool:
            continue
        used += 1
        smap = dict(series)

        # 1 monthly, 2024 onward
        m_by = defaultdict(list)
        for d, v in series:
            if d.year < 2024:
                continue
            r = rank(pool, d, v)
            if r is not None:
                m_by[(d.year, d.month)].append(r)
        for k, rs in m_by.items():
            if len(rs) >= 10:
                by_month[k].append(statistics.median(rs))

        # 2 daily, last N days
        for k in range(args.days):
            d = today - timedelta(days=k)
            if d in smap:
                r = rank(pool, d, smap[d])
                if r is not None:
                    by_day[k].append(r)

        # 3 today vs 7 and 30 days ago
        ld, lv = series[-1]
        rn = rank(pool, ld, lv)
        if rn is None:
            continue
        for gap, store in ((7, deltas7), (30, deltas30)):
            d0, v0 = value_near(smap, ld - timedelta(days=gap))
            if v0 is None:
                continue
            r0 = rank(pool, d0, v0)
            if r0 is not None:
                store.append(rn - r0)
                if gap == 7:
                    movers.append((rn - r0, name, r0, rn))

        if i % 25 == 0:
            print(f"      {i}/{len(todo)}...")

    say(f"  scored {used} stations")

    say("\n[1] Median percentile by month (full-record baseline)")
    say(f"    {'month':<10}{'stations':>10}{'median pct':>13}")
    for (y, mo) in sorted(by_month):
        v = by_month[(y, mo)]
        if len(v) < 5:
            continue
        med = statistics.median(v)
        say(f"    {y}-{mo:02d}   {len(v):>10}{med:>13.1f}   {'#' * int(round(med / 2.5))}")

    say(f"\n[2] Median percentile by day, last {args.days} days")
    say(f"    {'days ago':<10}{'stations':>10}{'median pct':>13}")
    for k in sorted(by_day):
        v = by_day[k]
        if len(v) < 5 or k % 5:
            continue
        med = statistics.median(v)
        say(f"    {k:<10}{len(v):>10}{med:>13.1f}   {'#' * int(round(med / 2.5))}")
    say("")
    say("    A ramp means a real rise. Flat then a jump at day 0 or 1 means")
    say("    the published number is not drawn like the history behind it.")

    say("\n[3] Today's percentile minus the same station a week / month ago")
    for gap, store in ((7, deltas7), (30, deltas30)):
        if len(store) < 10:
            continue
        pos = 100 * sum(1 for x in store if x > 0) // len(store)
        say(f"    {gap:>3} days: n={len(store):<4} median {statistics.median(store):+6.1f}"
            f"   mean {statistics.fmean(store):+6.1f}   {pos}% positive")
    say("")
    say("    Centred near zero -> the last point is consistent with the days")
    say("    before it. Strongly positive -> the final row is the problem.")

    if movers:
        movers.sort(reverse=True)
        say("\n[4] Biggest one-week jumps")
        say(f"    {'station':<32}{'wk ago':>9}{'today':>8}{'jump':>8}")
        for dlt, name, r0, rn in movers[:12]:
            say(f"    {name[:30]:<32}{r0:>9.1f}{rn:>8.1f}{dlt:>+8.1f}")

    with open("recent_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\nWrote recent_report.txt")


if __name__ == "__main__":
    main()
