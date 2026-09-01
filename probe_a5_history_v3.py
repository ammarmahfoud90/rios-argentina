#!/usr/bin/env python3
"""
probe_a5_history_v3.py — how much history do the live stations have?

Three questions, one run:

  A. Is 5000 a cap? v2 guessed the parameter name wrong (limit=20000 returned
     nothing, meaning rejected, not honoured). This tries several spellings with
     a SMALL value first, which is the correct way to detect whether a parameter
     is respected at all.

  B. How far back does each live series go? Determines whether you can compute
     day-of-year percentiles from a station's own record. Uses year-offset
     sampling — about 8 small requests per series instead of downloading
     decades of data.

  C. If the live series are too short, how far is each one from a historical
     BDHI station? That is the "borrow the distribution from a neighbour"
     fallback, and its viability is a distance question.

Usage:
  python3 probe_a5_history_v3.py                # 120 live series sampled
  python3 probe_a5_history_v3.py --sample 300
  python3 probe_a5_history_v3.py --skip-pagination

Runtime: ~8 requests per sampled series. 120 series ~ 3 minutes at 6 workers.

Outputs:
  a5_history_v3.csv          per-series record start, span, nearest BDHI neighbour
  a5_history_report_v3.txt   the summary
"""

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://alerta.ina.gob.ar/a5"
TIMEOUT = 45
UA = {"User-Agent": "coverage-probe/3.0 (hydrology portfolio research)"}

HYDRO_VARS = {2: "Altura hidrométrica", 39: "Altura h. media diaria",
              101: "Altura h. 4-horaria", 4: "Caudal", 40: "Caudal medio diario"}
BBOX = (-74.0, -56.0, -52.0, -20.0)

# Years back to test for presence of data. Each test is a 45-day window.
YEAR_PROBES = [1, 2, 3, 5, 8, 12, 20, 30, 45]

# How many years of record you want before per-station percentiles are honest.
MIN_YEARS_FOR_PERCENTILES = 10


def session_with_token():
    s = requests.Session()
    s.headers.update(UA)
    if os.environ.get("A5_TOKEN"):
        s.headers["Authorization"] = f"Bearer {os.environ['A5_TOKEN']}"
    return s


def get_json(sess, path, params=None, retries=2):
    url = path if path.startswith("http") else f"{BASE}/{path.lstrip('/')}"
    for attempt in range(retries):
        try:
            r = sess.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 401:
                raise SystemExit("401 — needs a token. Set A5_TOKEN.")
            if r.status_code >= 400:
                return None
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                return None
            time.sleep(1.5)
    return None


def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}{k}"
            if isinstance(v, (dict, list)):
                out.update(flatten(v, prefix=f"{key}."))
            else:
                out[key] = v
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            out.update(flatten(v, prefix=f"{prefix}{i}."))
    return out


def pick(d, *cands, default=""):
    for c in cands:
        for k, v in d.items():
            if (k == c or k.endswith("." + c)) and v not in (None, ""):
                return v
    return default


def parse_feature(f):
    props = flatten(f.get("properties", f))
    coords = (f.get("geometry") or {}).get("coordinates") or [None, None]
    try:
        lon, lat = float(coords[0]), float(coords[1])
    except (TypeError, ValueError, IndexError):
        lon = lat = None
    return {
        "series_id": pick(props, "id", "series_id"),
        "station": pick(props, "nombre", "estacion"),
        "river": pick(props, "rio", "curso"),
        "var_id": pick(props, "var_id"),
        "network": pick(props, "red", "red_nombre", "fuente"),
        "lon": lon, "lat": lat,
        # if the catalogue already carries a date range, we get B for free
        "cat_start": pick(props, "date_range.timestart", "timestart",
                          "fecha_inicio", "start_date"),
        "cat_end": pick(props, "date_range.timeend", "timeend",
                        "fecha_fin", "end_date"),
    }


def coords_ok(r):
    if r["lon"] is None or r["lat"] is None:
        return False
    if abs(r["lon"]) < 0.01 and abs(r["lat"]) < 0.01:
        return False
    return BBOX[0] <= r["lon"] <= BBOX[2] and BBOX[1] <= r["lat"] <= BBOX[3]


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


def obs_between(sess, series_id, t0, t1):
    d = get_json(sess, "getObservaciones",
                 params={"tipo": "puntual", "series_id": series_id,
                         "timestart": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "timeend": t1.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return extract_observations(d)


# ---------- A. pagination ----------

def test_pagination(sess, say):
    say("\n[A] Is 5000 a cap?")
    baseline = get_json(sess, "obs/puntual/series", params={"format": "geojson"})
    n_base = len((baseline or {}).get("features", []) or [])
    say(f"    unparameterised: {n_base} features")

    honoured = None
    for name in ("limit", "per_page", "count", "size", "max", "n"):
        d = get_json(sess, "obs/puntual/series",
                     params={"format": "geojson", name: 10})
        n = len((d or {}).get("features", []) or [])
        say(f"    {name}=10 -> {n} features")
        if n == 10:
            honoured = name
            break

    if not honoured:
        say("    no limit parameter honoured. 5000 is either the true total or a "
            "hard server cap you cannot page past from this endpoint.")
        say("    next move: filter the catalogue by network or variable instead, "
            "e.g. ?var_id=2, and sum the parts. If the parts exceed 5000, it was "
            "a cap.")
        for vid in (2, 4, 39):
            d = get_json(sess, "obs/puntual/series",
                         params={"format": "geojson", "var_id": vid})
            n = len((d or {}).get("features", []) or [])
            say(f"      var_id={vid} -> {n} features")
        return None

    for off in ("offset", "skip", "start", "page"):
        d = get_json(sess, "obs/puntual/series",
                     params={"format": "geojson", honoured: 10, off: 10})
        feats = (d or {}).get("features", []) or []
        say(f"    {honoured}=10&{off}=10 -> {len(feats)} features")
        if len(feats) == 10:
            say(f"    -> pageable with {honoured}/{off}. Page the full catalogue "
                f"to confirm the true total.")
            return (honoured, off)
    say(f"    {honoured} works but no offset parameter found.")
    return (honoured, None)


# ---------- B. record length ----------

def record_start(sess, r, now):
    """Find the oldest year offset that still returns data. ~9 small requests."""
    if r.get("cat_start"):
        r["start_source"] = "catalogue"
        r["record_start"] = str(r["cat_start"])[:10]
        try:
            y = int(r["record_start"][:4])
            r["years"] = round(now.year - y + (now.month / 12), 1)
        except ValueError:
            r["years"] = ""
        return r

    r["start_source"] = "probed"
    oldest_hit = 0
    for yb in YEAR_PROBES:
        t1 = now - timedelta(days=365 * yb)
        t0 = t1 - timedelta(days=45)
        if obs_between(sess, r["series_id"], t0, t1):
            oldest_hit = yb
        else:
            # one miss can be a gap; stop after a miss beyond the last hit + 1 step
            if oldest_hit and yb > oldest_hit:
                break
    r["years"] = oldest_hit
    r["record_start"] = (now - timedelta(days=365 * oldest_hit)).strftime("%Y-%m-%d") \
        if oldest_hit else ""
    return r


# ---------- C. nearest historical neighbour ----------

def haversine(a, b):
    lon1, lat1 = a
    lon2, lat2 = b
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=120)
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--skip-pagination", action="store_true")
    args = ap.parse_args()

    sess = session_with_token()
    now = datetime.now(timezone.utc)
    lines = []

    def say(m=""):
        print(m)
        lines.append(m)

    say(f"INA a5 history probe v3 — {now:%Y-%m-%d %H:%M UTC}")
    say("=" * 66)

    if not args.skip_pagination:
        test_pagination(sess, say)

    say("\n[B] Fetching catalogue and finding live series...")
    d = get_json(sess, "obs/puntual/series", params={"format": "geojson"})
    rows = [parse_feature(f) for f in (d or {}).get("features", []) or []]
    hydro = [r for r in rows if str(r["var_id"]).isdigit()
             and int(r["var_id"]) in HYDRO_VARS and coords_ok(r)]
    say(f"    {len(hydro)} georeferenced level/flow series")

    have_dates = sum(1 for r in hydro if r.get("cat_start"))
    say(f"    catalogue already carries a start date for {have_dates} of them"
        + ("  (free answer, no probing needed)" if have_dates > len(hydro) * 0.8
           else ""))

    hist = [r for r in hydro if r["network"] == "alturas_bdhi"]
    say(f"    historical BDHI series available as donors: {len(hist)}")

    since = now - timedelta(hours=args.hours)
    cand = [r for r in hydro if r["network"] != "alturas_bdhi"]
    sample = random.sample(cand, min(args.sample * 2, len(cand)))
    say(f"\n    checking which of {len(sample)} are live, then dating them...")

    live = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(obs_between, sess, r["series_id"], since, now): r
                for r in sample}
        for f in as_completed(futs):
            if f.result():
                live.append(futs[f])
    say(f"    {len(live)} live")

    target = live[:args.sample]
    say(f"\n    dating {len(target)} live series (this is the slow part)...")
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(record_start, sess, r, now) for r in target]
        for f in as_completed(futs):
            f.result()
            done += 1
            if done % 25 == 0:
                print(f"      {done}/{len(target)}...")

    say("\n    record length distribution (live series):")
    buckets = [(0, 1, "<1 yr"), (1, 3, "1-3 yr"), (3, 5, "3-5 yr"),
               (5, 10, "5-10 yr"), (10, 20, "10-20 yr"), (20, 999, "20+ yr")]
    for lo, hi, label in buckets:
        n = sum(1 for r in target
                if isinstance(r.get("years"), (int, float)) and lo <= r["years"] < hi)
        bar = "#" * int(40 * n / max(len(target), 1))
        say(f"      {label:<9} {n:>4}  {bar}")

    enough = [r for r in target
              if isinstance(r.get("years"), (int, float))
              and r["years"] >= MIN_YEARS_FOR_PERCENTILES]
    say(f"\n    >= {MIN_YEARS_FOR_PERCENTILES} yr of record: {len(enough)} of "
        f"{len(target)} ({100*len(enough)/max(len(target),1):.0f}%)")
    say("    -> if this is high, compute percentiles per station from its own record.")
    say("    -> if low, you need the BDHI donor approach below.")

    say("\n    by network:")
    bynet = defaultdict(list)
    for r in target:
        if isinstance(r.get("years"), (int, float)):
            bynet[r["network"] or "(none)"].append(r["years"])
    for net, ys in sorted(bynet.items(), key=lambda x: -len(x[1])):
        say(f"      {net:<26} n={len(ys):<4} median={sorted(ys)[len(ys)//2]:>5} yr")

    say("\n[C] Distance from short-record live stations to a BDHI donor...")
    short = [r for r in target
             if isinstance(r.get("years"), (int, float))
             and r["years"] < MIN_YEARS_FOR_PERCENTILES]
    donors = [h for h in hist if coords_ok(h)]
    if short and donors:
        for r in short:
            best, bd = None, 1e9
            for h in donors:
                dkm = haversine((r["lon"], r["lat"]), (h["lon"], h["lat"]))
                if dkm < bd:
                    best, bd = h, dkm
            r["donor"] = best["station"]
            r["donor_km"] = round(bd, 1)
        ds = sorted(r["donor_km"] for r in short)
        say(f"    {len(short)} short-record stations")
        say(f"    nearest donor: median {ds[len(ds)//2]:.0f} km, "
            f"p25 {ds[len(ds)//4]:.0f} km, p75 {ds[3*len(ds)//4]:.0f} km")
        say(f"    within 50 km: {sum(1 for x in ds if x <= 50)}  "
            f"| within 150 km: {sum(1 for x in ds if x <= 150)}")
        say("    NOTE: straight-line distance, not along-river. A donor 20 km away "
            "on a different basin is useless. Check the river column before "
            "trusting any pairing.")
    else:
        say("    nothing to pair (either no short records or no donors).")

    fields = ["series_id", "station", "river", "var_id", "network", "lat", "lon",
              "years", "record_start", "start_source", "donor", "donor_km"]
    with open("a5_history_v3.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in target:
            for k in fields:
                r.setdefault(k, "")
            w.writerow(r)
    with open("a5_history_report_v3.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("\nWrote a5_history_v3.csv, a5_history_report_v3.txt")


if __name__ == "__main__":
    main()
