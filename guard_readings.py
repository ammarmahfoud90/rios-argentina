#!/usr/bin/env python3
"""
guard_readings.py — sanity-check TODAY'S reading before it goes on the map.

Everything built so far checks each station's HISTORY. Nothing checks the
one number the map actually shows. Corrientes proved why that matters: a
gauge reporting 7.01 m and scoring above every value in its own record,
during a month INA classifies as "aguas bajas". The history was fine. The
current reading was not.

Five guards, in order of how confidently they reject:

  A  stale      last reading older than --max-age days. No current claim.
  B  flatline   last N days identical. Sensor stuck, not river steady.
  C  spike      one-day change larger than anything in the station's own
                record. Not a flood — floods rise fast but within precedent.
  D  step       recent months sit almost entirely outside the historical
                envelope. THIS is the Corrientes case.
  E  range      value beyond the record's own min/max by a wide margin.

Guard D is the subtle one, and it is why this script exists separately from
the datum repair. A datum change and a genuine flood look identical on any
single day. They differ in DURATION: a flood peaks and recedes, a re-zeroed
gauge stays shifted forever. So D asks how much of the last --step-window
days sits beyond the historical p95/p5. A flood touches that ceiling
briefly. A step never comes back down.

That test is deliberately conservative, because a long real event exists
too — the 2019-21 bajante held low for two years. So D never deletes: it
demotes to "reading shown, no colour claimed", the same honesty rule used
for tier 3.

Reads only ./cache and the repair outputs. No network, no downloads.

    python3 guard_readings.py
    python3 guard_readings.py --step-window 120 --strict

Writes stations_published.geojson, guard_report.txt
"""

import argparse
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, date, timedelta

CACHE_DIR = "cache"
STATIONS_IN = "stations_final.geojson"
CLIM_IN = "climatology_final.json"


# ------------------------------------------------------------------ io

def load_cached(sid):
    """Daily series from cache as [(date, value)]. Same format build wrote."""
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
    """Day of year with 29 Feb folded onto 28 Feb, matching the builder."""
    n = d.timetuple().tm_yday
    if d.month > 2 and not (d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)):
        return n
    if d.month == 2 and d.day == 29:
        return 59
    if d.month > 2 and (d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)):
        return n - 1
    return n


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


# -------------------------------------------------------------- guards

def guard_stale(series, today, max_age):
    if not series:
        return "stale", "no cached data at all"
    last = series[-1][0]
    age = (today - last).days
    if age > max_age:
        return "stale", f"last reading {last} is {age} days old"
    return None, None


def guard_flatline(series, n_days):
    """A stuck sensor repeats one value exactly. A steady river does not."""
    if len(series) < n_days:
        return None, None
    tail = [v for _, v in series[-n_days:]]
    if len(set(round(v, 4) for v in tail)) == 1:
        return "flatline", f"last {n_days} days all exactly {tail[-1]:.3f}"
    return None, None


def guard_spike(series, factor):
    """
    One-day change bigger than the record has ever produced.

    Real floods rise fast, but they rise within the station's own precedent,
    because the precedent contains real floods. A sensor glitch does not.
    """
    if len(series) < 400:
        return None, None
    diffs = []
    for i in range(1, len(series) - 1):          # exclude the final transition:
        gap = (series[i][0] - series[i - 1][0]).days   # today's jump must not
        if gap == 1:                                   # define its own baseline
            diffs.append(abs(series[i][1] - series[i - 1][1]))
    if len(diffs) < 200:
        return None, None
    worst = max(diffs)
    gap = (series[-1][0] - series[-2][0]).days
    if gap != 1:
        return None, None
    now = abs(series[-1][1] - series[-2][1])
    if worst > 0 and now > worst * factor:
        return "spike", (f"jumped {now:.2f} in one day; largest ever seen "
                         f"in {len(diffs)} days of record is {worst:.2f}")
    return None, None


def guard_range(series, margin):
    """Value far outside the whole record's range."""
    if len(series) < 400:
        return None, None
    vals = sorted(v for _, v in series[:-1])
    lo, hi = vals[0], vals[-1]
    q1, q3 = pct(vals, 25), pct(vals, 75)
    iqr = max(q3 - q1, 1e-6)
    v = series[-1][1]
    if v > hi + margin * iqr:
        return "range", (f"{v:.2f} exceeds record max {hi:.2f} by "
                         f"{(v - hi) / iqr:.1f}x the spread")
    if v < lo - margin * iqr:
        return "range", (f"{v:.2f} below record min {lo:.2f} by "
                         f"{(lo - v) / iqr:.1f}x the spread")
    return None, None


def guard_step(series, window, share, today):
    """
    THE CORRIENTES TEST.

    Build the station's day-of-year envelope from everything older than the
    window. Then ask what share of the last `window` days sits outside it.

    A flood pushes above p95 for days or weeks, then recedes. A gauge that
    was re-zeroed sits outside the envelope essentially every single day and
    never returns. So a very high share is evidence of a datum step, not
    weather.

    Returns (share_high, share_low, detail) — the caller decides.
    """
    if len(series) < 400:
        return None, None, "record too short to judge"

    cutoff = today - timedelta(days=window)
    recent = [(d, v) for d, v in series if d > cutoff]
    older = [(d, v) for d, v in series if d <= cutoff]
    if len(recent) < max(20, window // 6) or len(older) < 365 * 3:
        return None, None, "not enough data either side of the window"

    pool = defaultdict(list)
    for d, v in older:
        n = doy(d)
        for off in range(-7, 8):
            pool[(n + off - 1) % 365 + 1].append(v)

    env = {}
    for n, vs in pool.items():
        if len(vs) >= 20:
            s = sorted(vs)
            env[n] = (pct(s, 5), pct(s, 95))

    hi = lo = tot = 0
    for d, v in recent:
        e = env.get(doy(d))
        if not e:
            continue
        tot += 1
        if v > e[1]:
            hi += 1
        elif v < e[0]:
            lo += 1
    if tot < 20:
        return None, None, "too few comparable days"

    sh_hi = round(100.0 * hi / tot, 1)
    sh_lo = round(100.0 * lo / tot, 1)
    detail = (f"{sh_hi}% of last {tot} days above the historical p95, "
              f"{sh_lo}% below p5")
    return sh_hi, sh_lo, detail


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age", type=int, default=10,
                    help="a reading older than this makes no claim about today")
    ap.add_argument("--flat-days", type=int, default=14)
    ap.add_argument("--spike-factor", type=float, default=1.5,
                    help="reject a one-day change this many times the "
                         "largest the record has ever contained")
    ap.add_argument("--range-margin", type=float, default=2.0)
    ap.add_argument("--step-window", type=int, default=90,
                    help="how many recent days to test against the envelope")
    ap.add_argument("--step-share", type=float, default=80.0,
                    help="percent of that window outside the envelope before "
                         "it is called a step rather than weather")
    ap.add_argument("--strict", action="store_true",
                    help="drop stepped stations instead of demoting them")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD, for testing")
    args = ap.parse_args()

    today = (datetime.strptime(args.today, "%Y-%m-%d").date()
             if args.today else date.today())

    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say(f"Reading guard — {datetime.utcnow():%Y-%m-%d %H:%M} UTC")
    say("=" * 66)

    if not os.path.exists(STATIONS_IN):
        say(f"  {STATIONS_IN} not found — run repair_climatology.py first")
        return

    with open(STATIONS_IN, encoding="utf-8") as fh:
        fc = json.load(fh)
    feats = fc.get("features", [])
    say(f"  {len(feats)} sites to check")

    verdicts = defaultdict(list)
    out_feats = []

    for f in feats:
        p = dict(f.get("properties", {}))
        sid = str(p.get("series_id"))
        name = str(p.get("station") or sid)
        series = load_cached(sid)

        reason = None
        code = None

        for fn, a in ((guard_stale, (series, today, args.max_age)),
                      (guard_flatline, (series, args.flat_days)),
                      (guard_spike, (series, args.spike_factor)),
                      (guard_range, (series, args.range_margin))):
            code, reason = fn(*a)
            if code:
                break

        step_note = ""
        if not code:
            hi, lo, detail = guard_step(series, args.step_window,
                                        args.step_share, today)
            step_note = detail
            if hi is not None and max(hi, lo) >= args.step_share:
                code, reason = "step", detail

        if code:
            verdicts[code].append((name, sid, reason))
            p["publish"] = "reading_only"
            p["guard"] = code
            p["guard_reason"] = reason
            # the colour claim is withdrawn; the number is still shown
            p["class"] = None
            p["percentile"] = None
        else:
            verdicts["ok"].append((name, sid, step_note))
            p["publish"] = "colour"
            p["guard"] = None
            p["guard_reason"] = None

        if code == "step" and args.strict:
            continue
        if code == "stale":
            p["last_value"] = p.get("last_value")
        out_feats.append({"type": "Feature", "geometry": f.get("geometry"),
                          "properties": p})

    say("\n[1] Guard results")
    order = ["ok", "stale", "flatline", "spike", "range", "step"]
    for k in order:
        if verdicts[k]:
            say(f"    {k:<10} {len(verdicts[k]):>4}")

    for k in ["step", "spike", "range", "flatline", "stale"]:
        if not verdicts[k]:
            continue
        say(f"\n    {k} ({len(verdicts[k])}):")
        for name, sid, why in verdicts[k][:15]:
            say(f"      {name[:34]:<34} {sid:>7}  {why}")
        if len(verdicts[k]) > 15:
            say(f"      ... and {len(verdicts[k]) - 15} more, see guard_report.txt")

    coloured = [f for f in out_feats if f["properties"]["publish"] == "colour"]
    say(f"\n[2] Final map layer")
    say(f"    {len(coloured)} sites coloured, "
        f"{len(out_feats) - len(coloured)} shown as reading only")

    cls = defaultdict(int)
    for f in coloured:
        cls[f["properties"].get("class")] += 1
    tot = sum(cls.values()) or 1
    for k in ["much_below", "below", "normal", "above", "much_above"]:
        say(f"    {k:<12} {cls[k]:>4}  ({100*cls[k]//tot}%)")
    ab = 100 * (cls["above"] + cls["much_above"]) // tot
    be = 100 * (cls["below"] + cls["much_below"]) // tot
    say(f"    above-ish {ab}%, below-ish {be}%   (25/25 expected by chance)")

    with open("stations_published.geojson", "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": out_feats},
                  fh, ensure_ascii=False)
    full = []
    for k in order:
        for name, sid, why in verdicts[k]:
            full.append(f"{k:<10} {name[:36]:<36} {sid:>7}  {why}")
    with open("guard_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n\nALL STATIONS\n" + "\n".join(full) + "\n")

    print("\nWrote stations_published.geojson, guard_report.txt")


if __name__ == "__main__":
    main()
