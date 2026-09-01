#!/usr/bin/env python3
"""
diagnose_bias.py — why is the median station at percentile 69 instead of 50?

The skew is uniform across Patagonia, NOA, Litoral and Pampa, across level
and flow, across tier 1 and tier 2, and across every record length. Weather
cannot do that. Something in the method is putting modern readings above a
pool built mostly from older ones.

Three tests, each of which would look different if the cause were weather:

  A  OFFSET SIGN
     The repair measured a real datum offset on the joins that had genuine
     overlap. If donor and telemetry were merely on arbitrary gauge zeros,
     those offsets scatter around zero. If the old readings are a
     systematically lower statistic than the new ones, they lean positive.

  B  PERCENTILE BY YEAR   <- the decisive one
     For every station, score EVERY day of the last 25 years against a
     climatology built only from data older than that window. Then take the
     median per calendar year across all stations.

       weather        wobbles year to year, no persistence
       real trend     drifts gradually
       method change  flat, then a step in one year, then flat and high

  C  SPREAD RATIO
     A daily mean of 24 hourly readings varies less than a single reading
     taken once a day. If the modern era is daily means and the old era is
     single manual readings, the modern era should be measurably SMOOTHER,
     not just higher. If it is both, that is the signature.

Reads only ./cache and the repair outputs. No network.

    python3 diagnose_bias.py
    python3 diagnose_bias.py --years 30 --stations 60

Writes bias_report.txt
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
    """Sorted value pool per day-of-year, +/- window days. Built once."""
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


def roughness(pairs):
    """
    Median day-to-day change. This measures MEASUREMENT noise, not the
    season: total spread is dominated by the annual cycle, so averaging 24
    readings into one barely dents it. Consecutive days, though, differ
    mostly by noise — and a daily mean is visibly smoother than a single
    reading taken once a day.
    """
    diffs = []
    for i in range(1, len(pairs)):
        if (pairs[i][0] - pairs[i - 1][0]).days == 1:
            diffs.append(abs(pairs[i][1] - pairs[i - 1][1]))
    if len(diffs) < 200:
        return None
    return statistics.median(diffs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=25,
                    help="how many recent calendar years to score")
    ap.add_argument("--stations", type=int, default=0,
                    help="cap for a quick run; 0 = all")
    ap.add_argument("--min-pool-years", type=int, default=8)
    args = ap.parse_args()

    src = next((c for c in CANDIDATES if os.path.exists(c)), None)
    if not src:
        print("no stations_final.geojson or stations_current.geojson here")
        return

    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say(f"Bias diagnosis — {datetime.now():%Y-%m-%d %H:%M}")
    say("=" * 66)
    say(f"  source: {src}")

    with open(src, encoding="utf-8") as fh:
        feats = json.load(fh)["features"]
    props = [f["properties"] for f in feats]
    say(f"  {len(props)} stations")

    # ---------------------------------------------------------- test A
    say("\n[A] Sign of the measured datum offsets")
    offs = [p["datum_offset"] for p in props
            if isinstance(p.get("datum_offset"), (int, float))
            and abs(p["datum_offset"]) > 1e-9]
    if not offs:
        say("    no measured offsets in this file (run on stations_final.geojson)")
    else:
        pos = sum(1 for o in offs if o > 0)
        neg = len(offs) - pos
        say(f"    {len(offs)} joins with a measured offset")
        say(f"    positive {pos}   negative {neg}   "
            f"({100 * pos // len(offs)}% positive, 50% expected if arbitrary)")
        say(f"    median offset {statistics.median(offs):+.3f} m, "
            f"mean {statistics.fmean(offs):+.3f} m")
        if pos and neg:
            say(f"    median |positive| {statistics.median([o for o in offs if o > 0]):.3f}, "
                f"median |negative| {statistics.median([-o for o in offs if o < 0]):.3f}")

    # ------------------------------------------------------ tests B, C
    this_year = date.today().year
    first_year = this_year - args.years + 1
    cutoff = date(first_year, 1, 1)

    per_year = defaultdict(list)      # year -> median percentile per station
    spread_rows = []
    used = 0
    skipped = 0

    todo = props[:args.stations] if args.stations else props
    say(f"\n[B] Scoring every day since {first_year} for {len(todo)} stations "
        f"(this is the slow part)")

    for i, p in enumerate(todo, 1):
        sid = str(p.get("series_id"))
        series = load_cached(sid)
        old = [(d, v) for d, v in series if d < cutoff]
        new = [(d, v) for d, v in series if d >= cutoff]
        if len(old) < 365 * 8 or len(new) < 365:
            skipped += 1
            continue

        pool = build_pool(old, min_years=args.min_pool_years)
        if not pool:
            skipped += 1
            continue
        used += 1

        byyear = defaultdict(list)
        for d, v in new:
            r = rank(pool, d, v)
            if r is not None:
                byyear[d.year].append(r)
        for y, rs in byyear.items():
            if len(rs) >= 120:
                per_year[y].append(statistics.median(rs))

        o_r = roughness(old)
        n_r = roughness(new)
        if o_r and n_r and o_r > 1e-6:
            spread_rows.append(n_r / o_r)

        if i % 25 == 0:
            print(f"      {i}/{len(todo)}...")

    say(f"    scored {used} stations, skipped {skipped} for too little data")

    if per_year:
        say("")
        say(f"    {'year':<8}{'stations':>10}{'median pct':>13}   {'':<24}")
        for y in sorted(per_year):
            v = per_year[y]
            if len(v) < 5:
                continue
            med = statistics.median(v)
            bar = "#" * int(round(med / 2.5))
            say(f"    {y:<8}{len(v):>10}{med:>13.1f}   {bar}")
        say("")
        say("    50 = the modern era matches the old climatology exactly.")
        say("    A one-year jump that never comes back is a method change.")
        say("    A gradual climb is a real trend. Wobble is weather.")

    # ---------------------------------------------------------- test C
    say("\n[C] Is the modern era smoother than the old one?")
    if len(spread_rows) >= 20:
        med = statistics.median(spread_rows)
        below = 100 * sum(1 for r in spread_rows if r < 1.0) // len(spread_rows)
        say(f"    {len(spread_rows)} stations compared")
        say(f"    median modern roughness / old roughness = {med:.2f}")
        say(f"    {below}% of stations are smoother now than before")
        if med < 0.9:
            say("    -> modern era is measurably smoother. Consistent with "
                "daily means replacing single daily readings.")
        elif med > 1.1:
            say("    -> modern era is rougher, which argues against the "
                "daily-mean explanation.")
        else:
            say("    -> spread is about the same, so smoothing is probably "
                "not the mechanism.")
    else:
        say("    not enough stations with both eras to judge")

    with open("bias_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\nWrote bias_report.txt")


if __name__ == "__main__":
    main()
