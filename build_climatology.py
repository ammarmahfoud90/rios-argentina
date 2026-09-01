#!/usr/bin/env python3
"""
build_climatology.py — turn the tier 1 / tier 2 roster into "is today normal?"

This is the first script that is not a probe. It produces the numbers the map
is actually coloured with.

What it does, in order:

  1. RE-CHECKS THE PAIRINGS. v4 joined a modern series to a historical one by
     distance alone. It never checked that the two measure the SAME THING. A
     level gauge joined to a flow gauge is a silent disaster, so every pair is
     re-validated against the catalogue: same variable family (level with level,
     flow with flow) or the pair is dropped.

  2. DOWNLOADS THE HISTORY. Tries one big request per series first; if the
     response looks truncated it falls back to chunked requests. Everything is
     cached to ./cache so a rerun costs nothing. This is the same lesson as the
     5000 cap: assume a limit exists until you have shown it does not.

  3. CHECKS THE DATUM BEFORE CONCATENATING. Two series at one site can be
     measured against different gauge zeros. If they overlap in time, the median
     difference over the overlap is measured and applied. If they do not overlap,
     the pair is flagged, because splicing across an unknown datum shift invents
     a jump that never happened.

  4. COMPUTES DAY-OF-YEAR PERCENTILES. For each calendar day, pools observations
     from a +/- WINDOW day window across all years and takes the 5/10/25/50/75/
     90/95th percentiles. A day backed by too few distinct years is marked
     unreliable rather than published.

  5. CLASSIFIES TODAY. Current reading against that day's distribution gives the
     colour class, plus how many years actually support the judgement.

Usage:
  python3 build_climatology.py                 # 40 stations, a quick trial
  python3 build_climatology.py --all           # the whole roster
  python3 build_climatology.py --min-years 15  # stricter
  python3 build_climatology.py --no-cache

Inputs:  a5_joins_v4.csv  (from probe_a5_join_v4.py)
Outputs:
  climatology.json         per station: the day-of-year percentile curves
  stations_current.geojson today's reading + colour class, for deck.gl
  build_report.txt         what was dropped and why — read this
"""

import argparse
import csv
import json
import os
import statistics
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://alerta.ina.gob.ar/a5"
TIMEOUT = 60
UA = {"User-Agent": "climatology-build/1.0 (hydrology portfolio)"}
CACHE_DIR = "cache"

LEVEL_VARS = {2, 39, 101}
FLOW_VARS = {4, 40}
PCTLS = [5, 10, 25, 50, 75, 90, 95]

WINDOW = 7           # +/- days pooled into each day-of-year distribution
MIN_YEARS_PER_DOY = 8
CHUNK_YEARS = 5
SUSPECT_ROW_COUNTS = {1000, 5000, 10000, 20000}   # round = probably truncated


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


def flatten(o, p=""):
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            key = f"{p}{k}"
            if isinstance(v, (dict, list)):
                out.update(flatten(v, f"{key}."))
            else:
                out[key] = v
    elif isinstance(o, list):
        for i, v in enumerate(o[:3]):
            out.update(flatten(v, f"{p}{i}."))
    return out


def pick(d, *cands, default=""):
    for c in cands:
        for k, v in d.items():
            if (k == c or k.endswith("." + c)) and v not in (None, ""):
                return v
    return default


def extract_observations(p):
    if p is None:
        return []
    if isinstance(p, list):
        if p and isinstance(p[0], dict) and any(
                k in p[0] for k in ("timestart", "valor", "fecha", "timeend")):
            return p
        for it in p:
            g = extract_observations(it)
            if g:
                return g
        return []
    if isinstance(p, dict):
        for k in ("observaciones", "data", "rows", "result", "series", "values"):
            if k in p:
                g = extract_observations(p[k])
                if g:
                    return g
    return []


def obs_to_pairs(obs):
    """[(date, value)] — daily resolution, values coerced to float."""
    out = []
    for o in obs:
        ts = None
        for k in ("timestart", "fecha", "time", "date", "timeend"):
            v = o.get(k)
            if isinstance(v, str) and len(v) >= 10:
                ts = v[:10]
                break
        if not ts:
            continue
        val = None
        for k in ("valor", "value", "val"):
            if o.get(k) is not None:
                try:
                    val = float(o[k])
                except (TypeError, ValueError):
                    val = None
                break
        if val is None:
            continue
        try:
            d = datetime.strptime(ts, "%Y-%m-%d").date()
        except ValueError:
            continue
        out.append((d, val))
    return out


# ------------------------------------------------------- history download

def fetch_range(sess, sid, t0, t1):
    d = get_json(sess, "getObservaciones",
                 params={"tipo": "puntual", "series_id": sid,
                         "timestart": t0.strftime("%Y-%m-%dT00:00:00Z"),
                         "timeend": t1.strftime("%Y-%m-%dT23:59:59Z")})
    return extract_observations(d)


def fetch_history(sess, sid, start, end, use_cache=True, log=None):
    """One big request, verified. Falls back to chunks if it looks truncated."""
    path = os.path.join(CACHE_DIR, f"{sid}.json")
    if use_cache and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            return [(datetime.strptime(d, "%Y-%m-%d").date(), v) for d, v in raw]
        except (ValueError, OSError):
            pass

    obs = fetch_range(sess, sid, start, end)
    pairs = obs_to_pairs(obs)
    truncated = False
    if pairs:
        got_first = min(p[0] for p in pairs)
        # round row count, or the earliest row is far later than we asked for
        if len(obs) in SUSPECT_ROW_COUNTS:
            truncated = True
        if (got_first - start.date()).days > 400:
            truncated = True
    elif (end - start).days > 400:
        truncated = True

    if truncated:
        if log:
            log(f"      series {sid}: single request looked truncated "
                f"({len(obs)} rows) — chunking")
        pairs = []
        cur = start
        while cur < end:
            nxt = min(cur + timedelta(days=365 * CHUNK_YEARS), end)
            pairs.extend(obs_to_pairs(fetch_range(sess, sid, cur, nxt)))
            cur = nxt + timedelta(days=1)

    # daily mean, deduplicated
    byday = defaultdict(list)
    for d, v in pairs:
        byday[d].append(v)
    daily = sorted((d, statistics.fmean(vs)) for d, vs in byday.items())

    if use_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([[d.isoformat(), v] for d, v in daily], fh)
        except OSError:
            pass
    return daily


# --------------------------------------------------------- splice + datum

def datum_offset(modern, donor):
    """Median (modern - donor) over overlapping days. None if no overlap."""
    md = dict(modern)
    common = [(md[d] - v) for d, v in donor if d in md]
    if len(common) < 30:
        return None, len(common)
    return statistics.median(common), len(common)


def splice(modern, donor, offset):
    """Donor first, corrected onto the modern datum; modern always wins."""
    md = dict(modern)
    out = dict((d, v + offset) for d, v in donor)
    out.update(md)
    return sorted(out.items())


# ------------------------------------------------------------ climatology

def doy(d):
    """Day of year with 29 Feb folded onto 28 Feb, so every year has 365."""
    n = d.timetuple().tm_yday
    if d.month > 2 and (d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)):
        n -= 1
    return min(max(n, 1), 365)


def climatology(daily, window=WINDOW, min_years=MIN_YEARS_PER_DOY):
    """For each of 365 days: percentiles pooled over +/- window across years."""
    buckets = defaultdict(list)
    for d, v in daily:
        buckets[doy(d)].append((d.year, v))

    curves, coverage = {}, {}
    for n in range(1, 366):
        pool, years = [], set()
        for off in range(-window, window + 1):
            k = ((n - 1 + off) % 365) + 1
            for y, v in buckets.get(k, []):
                pool.append(v)
                years.add(y)
        if len(pool) < 10 or len(years) < min_years:
            coverage[n] = len(years)
            continue
        pool.sort()
        curves[n] = {str(p): round(quantile(pool, p / 100), 4) for p in PCTLS}
        coverage[n] = len(years)
    return curves, coverage


def quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def percentile_rank(sorted_vals, x):
    if not sorted_vals:
        return None
    below = sum(1 for v in sorted_vals if v < x)
    equal = sum(1 for v in sorted_vals if v == x)
    return round(100 * (below + 0.5 * equal) / len(sorted_vals), 1)


def classify(rank):
    if rank is None:
        return "unknown"
    if rank < 5:
        return "much_below"
    if rank < 25:
        return "below"
    if rank <= 75:
        return "normal"
    if rank <= 95:
        return "above"
    return "much_above"


def today_pool(daily, ref, window=WINDOW):
    n = doy(ref)
    pool = []
    for d, v in daily:
        k = doy(d)
        diff = min(abs(k - n), 365 - abs(k - n))
        if diff <= window and d.year != ref.year:
            pool.append(v)
    return sorted(pool)


# ------------------------------------------------------------------- main

def var_family(vid):
    try:
        vid = int(vid)
    except (TypeError, ValueError):
        return None
    if vid in LEVEL_VARS:
        return "level"
    if vid in FLOW_VARS:
        return "flow"
    return None


def load_catalogue_vars(sess, say):
    """series_id -> (var_id, start, end). Paged, same as v4."""
    say("  loading catalogue to re-check pair compatibility...")
    out, seen, offset = {}, set(), 0
    while offset < 60000:
        d = get_json(sess, "obs/puntual/series",
                     params={"format": "geojson", "limit": 1000, "offset": offset})
        feats = (d or {}).get("features", []) or []
        if not feats:
            break
        fresh = 0
        for f in feats:
            props = flatten(f.get("properties", f))
            sid = str(pick(props, "id", "series_id"))
            if sid and sid not in seen:
                seen.add(sid)
                fresh += 1
                out[sid] = {
                    "var_id": pick(props, "var_id"),
                    "start": str(pick(props, "date_range.timestart", "timestart"))[:10],
                    "end": str(pick(props, "date_range.timeend", "timeend"))[:10],
                }
        if fresh == 0 or len(feats) < 1000:
            break
        offset += 1000
    say(f"  {len(out)} series in catalogue")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--joins", default="a5_joins_v4.csv")
    ap.add_argument("--limit-stations", type=int, default=40)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--min-years", type=int, default=MIN_YEARS_PER_DOY)
    ap.add_argument("--window", type=int, default=WINDOW)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    sess = session()
    now = datetime.now(timezone.utc)
    lines = []

    def say(m=""):
        print(m)
        lines.append(m)

    say(f"Climatology build — {now:%Y-%m-%d %H:%M UTC}")
    say("=" * 66)

    with open(args.joins, encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("tier") in ("1", "2")]
    say(f"\n[1] {len(rows)} tier 1/2 stations in {args.joins}")

    cat = load_catalogue_vars(sess, say)

    # --- pair validation
    say("\n[2] Re-checking pairings (v4 matched on distance only)")
    dropped = Counter()
    keep = []
    for r in rows:
        mine = var_family(cat.get(str(r["series_id"]), {}).get("var_id"))
        if mine is None:
            dropped["modern series not in catalogue or odd variable"] += 1
            continue
        r["_family"] = mine
        if r["tier"] == "2" and r.get("donor_series_id"):
            theirs = var_family(cat.get(str(r["donor_series_id"]), {}).get("var_id"))
            if theirs is None:
                dropped["donor variable unknown"] += 1
                r["donor_series_id"] = ""
                r["tier"] = "3"
            elif theirs != mine:
                dropped[f"variable mismatch ({mine} joined to {theirs})"] += 1
                r["donor_series_id"] = ""
                r["tier"] = "3"
            elif r.get("river_agrees") == "NO":
                dropped["river names disagree"] += 1
                r["donor_series_id"] = ""
                r["tier"] = "3"
        keep.append(r)
    for reason, n in dropped.most_common():
        say(f"    dropped {n:>4}  {reason}")
    keep = [r for r in keep if r["tier"] in ("1", "2")]
    say(f"    {len(keep)} stations survive pair validation")

    if not args.all:
        keep = keep[:args.limit_stations]
        say(f"    trial run: using {len(keep)} (pass --all for the rest)")

    # --- download + build
    say(f"\n[3] Downloading history and computing percentiles "
        f"(window +/-{args.window}d, min {args.min_years} yr per day)")

    def build_one(r):
        sid = str(r["series_id"])
        meta = cat.get(sid, {})
        start = meta.get("start") or "1970-01-01"
        try:
            t0 = datetime.strptime(start, "%Y-%m-%d")
        except ValueError:
            t0 = datetime(1970, 1, 1)
        daily = fetch_history(sess, sid, t0, now.replace(tzinfo=None),
                              use_cache=not args.no_cache)
        r["_note"] = ""
        if r["tier"] == "2" and r.get("donor_series_id"):
            dsid = str(r["donor_series_id"])
            dmeta = cat.get(dsid, {})
            try:
                d0 = datetime.strptime(dmeta.get("start") or "1970-01-01", "%Y-%m-%d")
            except ValueError:
                d0 = datetime(1970, 1, 1)
            try:
                d1 = datetime.strptime(dmeta.get("end") or "2000-01-01", "%Y-%m-%d")
            except ValueError:
                d1 = datetime(2000, 1, 1)
            donor = fetch_history(sess, dsid, d0, d1, use_cache=not args.no_cache)
            if donor and daily:
                off, n_overlap = datum_offset(daily, donor)
                if off is None:
                    r["_note"] = f"no datum overlap ({n_overlap}d) — donor not spliced"
                else:
                    r["_datum_offset"] = round(off, 3)
                    r["_overlap_days"] = n_overlap
                    daily = splice(daily, donor, off)
                    if abs(off) > 1.0:
                        r["_note"] = f"large datum shift {off:+.2f} — verify by hand"
            elif donor and not daily:
                daily = donor
        r["_daily"] = daily
        return r

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for f in as_completed([ex.submit(build_one, r) for r in keep]):
            f.result()
            done += 1
            if done % 10 == 0:
                print(f"    {done}/{len(keep)}...")

    out_clim, features, notes = {}, [], []
    stats = Counter()
    for r in keep:
        daily = r.get("_daily") or []
        if len(daily) < 365:
            stats["too little data after download"] += 1
            continue
        years = len({d.year for d, _ in daily})
        curves, coverage = climatology(daily, args.window, args.min_years)
        if not curves:
            stats["no day had enough distinct years"] += 1
            continue

        last_d, last_v = daily[-1]
        pool = today_pool(daily, last_d, args.window)
        rank = percentile_rank(pool, last_v)
        cls = classify(rank)
        stats[cls] += 1

        sid = str(r["series_id"])
        out_clim[sid] = {
            "station": r.get("station"), "river": r.get("river"),
            "variable_family": r.get("_family"),
            "years": years,
            "first": daily[0][0].isoformat(), "last": last_d.isoformat(),
            "days_covered": len(curves),
            "percentiles": curves,
        }
        if r.get("_note"):
            notes.append(f"    {sid} {r.get('station')}: {r['_note']}")

        try:
            lon, lat = float(r["lon"]), float(r["lat"])
        except (TypeError, ValueError, KeyError):
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "series_id": sid, "station": r.get("station"),
                "river": r.get("river"), "tier": r.get("tier"),
                "variable": r.get("_family"),
                "last_date": last_d.isoformat(), "last_value": round(last_v, 3),
                "percentile": rank, "class": cls,
                "years_of_record": years,
                "days_with_climatology": len(curves),
                "donor_series_id": r.get("donor_series_id", ""),
                "datum_offset": r.get("_datum_offset", ""),
            },
        })

    say(f"\n[4] Built climatology for {len(out_clim)} stations")
    say("\n    today's state:")
    for k in ("much_below", "below", "normal", "above", "much_above"):
        if stats[k]:
            say(f"      {k:<12} {stats[k]:>4}")
    for k, n in stats.items():
        if k not in ("much_below", "below", "normal", "above", "much_above"):
            say(f"      skipped: {k} — {n}")

    if out_clim:
        ys = sorted(v["years"] for v in out_clim.values())
        dc = sorted(v["days_covered"] for v in out_clim.values())
        say(f"\n    years of record: median {ys[len(ys)//2]}, "
            f"min {ys[0]}, max {ys[-1]}")
        say(f"    days of the year with a usable distribution: "
            f"median {dc[len(dc)//2]} of 365")
        thin = [k for k, v in out_clim.items() if v["days_covered"] < 300]
        if thin:
            say(f"    {len(thin)} stations cover under 300 days — these have "
                f"seasonal holes; consider excluding them from the map")

    if notes:
        say("\n    datum warnings (read these):")
        for n in notes[:25]:
            say(n)
        if len(notes) > 25:
            say(f"    ... and {len(notes)-25} more in build_report.txt")

    with open("climatology.json", "w", encoding="utf-8") as fh:
        json.dump(out_clim, fh, ensure_ascii=False)
    with open("stations_current.geojson", "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": features},
                  fh, ensure_ascii=False)
    with open("build_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n" + "\n".join(notes))
    print("\nWrote climatology.json, stations_current.geojson, build_report.txt")
    print(f"(history cached in ./{CACHE_DIR} — reruns are free)")


if __name__ == "__main__":
    main()
