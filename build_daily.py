#!/usr/bin/env python3
"""
build_daily.py — refresh the map. Safe to run unattended, every day.

WHAT IT DOES NOT DO, ON PURPOSE

  The datum repair, the sibling collapse and the snapping were judgement
  calls. They looked at overlaps, demoted what could not be verified, and
  decided which channel a gauge belongs to. None of that should rerun
  unsupervised at 6am — a bad automatic re-splice would silently change what
  the map claims. Those results are frozen in climatology_final.json and
  stations_snapped.json, and this script treats them as given.

  Rerun repair_climatology.py and snap_v2.py by hand when the roster changes,
  and read the reports before committing the result.

WHAT IT DOES

  1  Top up the cache. Only from the last cached day, not the whole record.
     125 years of Parana stage does not need redownloading daily.
  2  Rank today's reading against that station's own day-of-year pool.
  3  Run the five guards from guard_readings.py — stale, flatline, spike,
     out of range, and the sustained step that caught Corrientes.
  4  Propagate along the network with the catchment-growth rule.
  5  Write stations_snapped.json and reach_colours.json, which are what the
     page reads. network.json never changes and stays committed.

    python3 build_daily.py
    python3 build_daily.py --dry-run      # no writes, just the report

Environment: A5_TOKEN is used if set. It has never been required.

Writes stations_snapped.json, reach_colours.json, build_daily_report.txt
"""

import argparse
import bisect
import json
import math
import os
import sys
import time
from collections import defaultdict, Counter, deque
from datetime import datetime, date, timedelta

try:
    import requests
except ImportError:
    raise SystemExit("need requests:  pip install requests")

BASE = "https://alerta.ina.gob.ar/a5"
UA = {"User-Agent": "rios-argentina-daily/1.0 (hydrology portfolio)"}
TIMEOUT = 60
CACHE_DIR = "cache"

CLIM = "climatology_final.json"
SNAP = "stations_snapped.json"
NET = "network.json"

WINDOW = 7
BACKFILL_DAYS = 10          # re-ask for this much overlap, in case of late data


# ----------------------------------------------------------------- api

def session():
    s = requests.Session()
    s.headers.update(UA)
    if os.environ.get("A5_TOKEN"):
        s.headers["Authorization"] = f"Bearer {os.environ['A5_TOKEN']}"
    return s


def get_json(sess, path, params=None, retries=3):
    url = f"{BASE}/{path.lstrip('/')}"
    for a in range(retries):
        try:
            r = sess.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 401:
                raise SystemExit("401 — set A5_TOKEN.")
            if r.status_code >= 400:
                return None
            return r.json()
        except (requests.RequestException, ValueError):
            if a == retries - 1:
                return None
            time.sleep(2)
    return None


def observations(sess, sid, t0, t1):
    d = get_json(sess, "getObservaciones",
                 params={"tipo": "puntual", "series_id": sid,
                         "timestart": t0.strftime("%Y-%m-%dT00:00:00Z"),
                         "timeend": t1.strftime("%Y-%m-%dT23:59:59Z")})
    rows = d if isinstance(d, list) else (d or {}).get("data") or []
    daily = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = row.get("timestart") or row.get("timeend") or row.get("fecha")
        val = row.get("valor")
        if ts is None or val is None:
            continue
        try:
            day = datetime.strptime(str(ts)[:10], "%Y-%m-%d").date()
            daily[day].append(float(val))
        except (ValueError, TypeError):
            continue
    return [(d, sum(v) / len(v)) for d, v in sorted(daily.items())]


# --------------------------------------------------------------- cache

def cache_path(sid):
    return os.path.join(CACHE_DIR, f"{sid}.json")


def read_cache(sid):
    p = cache_path(sid)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as fh:
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


def write_cache(sid, pairs):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path(sid), "w", encoding="utf-8") as fh:
        json.dump([[d.isoformat(), v] for d, v in pairs], fh,
                  separators=(",", ":"))


# ------------------------------------------------------- climatology

def doy(d):
    n = d.timetuple().tm_yday
    leap = d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)
    if d.month == 2 and d.day == 29:
        return 59
    if d.month > 2 and leap:
        return n - 1
    return n


def build_pool(pairs, window=WINDOW, min_years=8):
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
    if not s or len(s) < 10:
        return None
    lo = bisect.bisect_left(s, v)
    hi = bisect.bisect_right(s, v)
    return round(100.0 * (lo + hi) / 2.0 / len(s), 1)


def rank_from_curve(curve, value):
    """
    Rank a reading against the stored 5/10/25/50/75/90/95 breakpoints.

    This matters more than it looks. The cache file for a series holds only
    that series' own data, but the climatology was built from the SPLICED
    record — donor century plus modern telemetry. Ranking against the cache
    would silently throw the donor away and turn Corrientes from 126 years
    into 26. So the daily job ranks against the curves the repair produced,
    which is the whole point of having frozen them.
    """
    pts = []
    for p in (5, 10, 25, 50, 75, 90, 95):
        v = curve.get(str(p))
        if v is None:
            continue
        pts.append((float(v), float(p)))
    if len(pts) < 3:
        return None
    pts.sort()
    if value < pts[0][0]:
        return round(pts[0][1] / 2.0, 1)          # somewhere below p5
    if value > pts[-1][0]:
        return round((pts[-1][1] + 100.0) / 2.0, 1)   # somewhere above p95
    for i in range(1, len(pts)):
        v0, p0 = pts[i - 1]
        v1, p1 = pts[i]
        if value <= v1:
            if v1 - v0 < 1e-12:
                return round(p1, 1)
            return round(p0 + (p1 - p0) * (value - v0) / (v1 - v0), 1)
    return None


def classify(r):
    if r is None:
        return None
    return ("much_below" if r < 5 else "below" if r < 25 else
            "normal" if r <= 75 else "above" if r <= 95 else "much_above")


# ------------------------------------------------------------ guards
# Same five tests as guard_readings.py. Kept here so the daily job is one
# file with no import path assumptions.

def guard(series, today, max_age=10, flat_days=14, spike_factor=1.5,
          range_margin=2.0, step_window=90, step_share=80.0):
    if not series:
        return "stale", "no cached data"
    last = series[-1][0]
    if (today - last).days > max_age:
        return "stale", f"last reading {last} is {(today - last).days} days old"

    if len(series) >= flat_days:
        tail = [v for _, v in series[-flat_days:]]
        if len(set(round(v, 4) for v in tail)) == 1:
            return "flatline", f"last {flat_days} days all exactly {tail[-1]:.3f}"

    if len(series) >= 400:
        diffs = [abs(series[i][1] - series[i - 1][1])
                 for i in range(1, len(series) - 1)
                 if (series[i][0] - series[i - 1][0]).days == 1]
        if len(diffs) >= 200 and (series[-1][0] - series[-2][0]).days == 1:
            worst = max(diffs)
            now = abs(series[-1][1] - series[-2][1])
            if worst > 0 and now > worst * spike_factor:
                return "spike", (f"jumped {now:.2f} in a day; record max is "
                                 f"{worst:.2f}")

        vals = sorted(v for _, v in series[:-1])
        q1, q3 = vals[len(vals) // 4], vals[3 * len(vals) // 4]
        iqr = max(q3 - q1, 1e-6)
        v = series[-1][1]
        if v > vals[-1] + range_margin * iqr:
            return "range", f"{v:.2f} far above record max {vals[-1]:.2f}"
        if v < vals[0] - range_margin * iqr:
            return "range", f"{v:.2f} far below record min {vals[0]:.2f}"

        cutoff = today - timedelta(days=step_window)
        recent = [(d, v) for d, v in series if d > cutoff]
        older = [(d, v) for d, v in series if d <= cutoff]
        if len(recent) >= max(20, step_window // 6) and len(older) >= 365 * 3:
            pool = defaultdict(list)
            for d, v in older:
                n = doy(d)
                for off in range(-WINDOW, WINDOW + 1):
                    pool[(n + off - 1) % 365 + 1].append(v)
            env = {}
            for n, vs in pool.items():
                if len(vs) >= 20:
                    s = sorted(vs)
                    env[n] = (s[int(len(s) * 0.05)], s[int(len(s) * 0.95)])
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
            if tot >= 20:
                sh_hi, sh_lo = 100.0 * hi / tot, 100.0 * lo / tot
                if max(sh_hi, sh_lo) >= step_share:
                    return "step", (f"{sh_hi:.0f}% of the last {tot} days above "
                                    f"p95, {sh_lo:.0f}% below p5")
    return None, None


# ------------------------------------------------------- propagation

def propagate(reaches, gauges, grow=1.5, max_km=300.0, up_fraction=0.5):
    byid = {r["id"]: r for r in reaches}
    upstream = defaultdict(list)
    for r in reaches:
        if r["next"] and r["next"] in byid:
            upstream[r["next"]].append(r["id"])

    def reach_km(r):
        pts = r["pts"]
        t = 0.0
        for i in range(1, len(pts)):
            dx = (pts[i][0] - pts[i - 1][0]) * 96.0
            dy = (pts[i][1] - pts[i - 1][1]) * 110.57
            t += math.hypot(dx, dy)
        return max(t, 0.1)

    gauged = {g["reach"]: g for g in gauges}
    best = {}
    for g in gauges:
        start = g["reach"]
        if start not in byid:
            continue
        start_up = max(byid[start].get("up_skm") or 1.0, 1.0)

        q = deque([(start, 0.0)])
        seen = {start}
        while q:
            rid, dist = q.popleft()
            cur = best.get(rid)
            if cur is None or dist < cur[0]:
                best[rid] = (dist, g)
            nxt = byid[rid]["next"]
            if not nxt or nxt not in byid or nxt in seen or nxt in gauged:
                continue
            nd = dist + reach_km(byid[rid])
            if nd > max_km or (byid[nxt].get("up_skm") or 0) > start_up * grow:
                continue
            seen.add(nxt)
            q.append((nxt, nd))

        q = deque([(start, 0.0)])
        seen = {start}
        limit = max_km * up_fraction
        while q:
            rid, dist = q.popleft()
            for up in upstream.get(rid, ()):
                if up in seen or up in gauged:
                    continue
                nd = dist + reach_km(byid[up])
                if nd > limit or (byid[up].get("up_skm") or 0) < start_up / grow:
                    continue
                cur = best.get(up)
                if cur is None or nd < cur[0]:
                    best[up] = (nd, g)
                seen.add(up)
                q.append((up, nd))

    return {str(rid): {"class": g["class"], "pct": g.get("percentile"),
                       "src": g.get("station"), "sid": g.get("series_id"),
                       "km": round(dist, 1)}
            for rid, (dist, g) in best.items()}


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--grow", type=float, default=1.5)
    ap.add_argument("--max-km", type=float, default=300.0)
    ap.add_argument("--today", default=None)
    args = ap.parse_args()

    today = (datetime.strptime(args.today, "%Y-%m-%d").date()
             if args.today else date.today())

    for f in (CLIM, SNAP, NET):
        if not os.path.exists(f):
            raise SystemExit(f"{f} missing — run the one-off scripts first")

    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say(f"Daily build — {datetime.now():%Y-%m-%d %H:%M} for {today}")
    say("=" * 66)

    clim = json.load(open(CLIM, encoding="utf-8"))
    snapped = json.load(open(SNAP, encoding="utf-8"))
    net = json.load(open(NET, encoding="utf-8"))
    say(f"  {len(clim)} stations with a climatology, "
        f"{len(snapped)} on the map, {len(net['reaches']):,} reaches")

    sess = session()
    fetched = new_rows = failed = 0

    say("\n[1] Topping up the cache")
    for i, s in enumerate(snapped, 1):
        sid = str(s.get("series_id"))
        series = read_cache(sid)
        start = (series[-1][0] - timedelta(days=BACKFILL_DAYS)) if series \
            else today - timedelta(days=365)
        if start > today:
            continue
        obs = observations(sess, sid, start, today)
        fetched += 1
        if obs:
            merged = dict(series)
            before = len(merged)
            merged.update(dict(obs))
            series = sorted(merged.items())
            new_rows += len(merged) - before
            if not args.dry_run:
                write_cache(sid, series)
        else:
            failed += 1
        s["_series"] = series
        if i % 25 == 0:
            print(f"      {i}/{len(snapped)}...")

    say(f"    asked {fetched} series, {new_rows} new daily values, "
        f"{failed} returned nothing")

    say("\n[2] Ranking and guarding today's readings")
    verdicts = Counter()
    for s in snapped:
        series = s.pop("_series", [])
        s["percentile"] = None
        s["class"] = None
        s["publish"] = "reading_only"
        s["guard"] = None
        if not series:
            verdicts["no data"] += 1
            continue
        code, why = guard(series, today)
        s["last_date"] = series[-1][0].isoformat()
        s["last_value"] = round(series[-1][1], 3)
        if code:
            s["guard"] = code
            s["guard_reason"] = why
            verdicts[code] += 1
            continue
        entry = clim.get(str(s.get("series_id"))) or {}
        curve = (entry.get("percentiles") or {}).get(str(doy(series[-1][0])))
        if curve:
            r = rank_from_curve(curve, series[-1][1])
            s["years_of_record"] = entry.get("years", s.get("years_of_record"))
        else:
            # no stored curve for this day of year — fall back to the cache,
            # which is honest but shorter, and say so
            r = rank(build_pool(series), series[-1][0], series[-1][1])
            if r is not None:
                s["ranked_from"] = "cache only"
        c = classify(r)
        if c is None:
            verdicts["no climatology"] += 1
            continue
        s["percentile"] = r
        s["class"] = c
        s["publish"] = "colour"
        verdicts["ok"] += 1

    for k, n in verdicts.most_common():
        say(f"    {k:<16}{n:>5}")

    cls = Counter(s["class"] for s in snapped if s.get("class"))
    say("")
    for k in ["much_below", "below", "normal", "above", "much_above"]:
        say(f"    {k:<12}{cls.get(k, 0):>5}")

    say("\n[3] Propagating along the network")
    gauges = [s for s in snapped
              if s.get("routed") and s.get("class") and s.get("publish") == "colour"]
    colours = propagate(net["reaches"], gauges, args.grow, args.max_km)
    say(f"    {len(gauges)} gauges coloured {len(colours):,} reaches")

    if args.dry_run:
        say("\n[dry run] nothing written")
    else:
        with open(SNAP, "w", encoding="utf-8") as fh:
            json.dump(snapped, fh, ensure_ascii=False, separators=(",", ":"))
        with open("reach_colours.json", "w", encoding="utf-8") as fh:
            json.dump(colours, fh, ensure_ascii=False, separators=(",", ":"))
        say(f"\nWrote {SNAP}, reach_colours.json")

    with open("build_daily_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    # a build that colours almost nothing is a failure, not a quiet success
    if verdicts["ok"] < 40:
        say(f"\nWARNING: only {verdicts['ok']} usable readings. "
            f"Not publishing this would be wiser than publishing it.")
        sys.exit(1)


if __name__ == "__main__":
    main()
