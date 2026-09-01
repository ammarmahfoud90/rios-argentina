#!/usr/bin/env python3
"""
probe_a5_coverage.py — Coverage probe for the INA alerta5 (a5) API.

Answers three questions before you build anything on top of it:

  1. How many hydrometric series exist, and where are they?
  2. How many of them actually have data in the last N hours?
  3. How many have precomputed daily percentiles (the "flow vs normal" colour)?

Outputs:
  a5_series_catalogue.csv   every hydro series with metadata + recency flag
  a5_live_stations.geojson  only the live ones, ready for deck.gl / MapLibre
  a5_coverage_report.txt    the printed summary, saved

Usage:
  pip install requests
  python probe_a5_coverage.py
  python probe_a5_coverage.py --hours 24 --percentile-sample 80
  A5_TOKEN=xxxx python probe_a5_coverage.py     # if read access needs a token

Endpoints used (all documented at https://alerta.ina.gob.ar/a5/apiUI):
  GET /a5/obs/puntual/series?format=geojson
  GET /a5/obs/puntual/series/{id}/estadisticosDiarios?format=json
  GET /a5/getPercentilesDiarios?series_id={id}
  GET /a5/getObservaciones?tipo=puntual&series_id={id}&timestart=&timeend=
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://alerta.ina.gob.ar/a5"
TIMEOUT = 45
UA = {"User-Agent": "coverage-probe/1.0 (hydrology portfolio research)"}

# a5 variable ids seen in the wild. Verify against /a5/metadatos?element=var
# before trusting these — print_variables() below dumps whatever the catalogue
# actually returns so you can correct them.
LIKELY_HYDRO_VARS = {1, 2, 4, 31, 33, 39, 40}  # nivel / caudal family


def session_with_token():
    s = requests.Session()
    s.headers.update(UA)
    token = os.environ.get("A5_TOKEN")
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def get_json(sess, path, params=None, retries=3):
    url = path if path.startswith("http") else f"{BASE}/{path.lstrip('/')}"
    for attempt in range(retries):
        try:
            r = sess.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 401:
                raise SystemExit(
                    "401 Unauthorized — read access needs a token.\n"
                    "Request one from jbianchi@ina.gob.ar, then rerun with A5_TOKEN=..."
                )
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == retries - 1:
                return {"__error__": str(e)}
            time.sleep(1.5 * (attempt + 1))
    return None


def flatten(obj, prefix=""):
    """a5 nests metadata inconsistently across endpoints. Flatten defensively."""
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


def pick(d, *candidates, default=""):
    """Grab the first key that exists, matching on suffix."""
    for c in candidates:
        for k, v in d.items():
            if k == c or k.endswith("." + c):
                if v not in (None, ""):
                    return v
    return default


def fetch_catalogue(sess, timestart=None, timeend=None):
    params = {"format": "geojson"}
    if timestart:
        params["timestart"] = timestart.isoformat()
        params["timeend"] = timeend.isoformat()
    data = get_json(sess, "obs/puntual/series", params=params)
    if not data or "__error__" in (data or {}):
        print(f"  ! catalogue fetch failed: {(data or {}).get('__error__')}")
        return []
    feats = data.get("features") if isinstance(data, dict) else data
    return feats or []


def parse_feature(f):
    props = flatten(f.get("properties", f))
    geom = f.get("geometry") or {}
    coords = geom.get("coordinates") or [None, None]
    return {
        "series_id": pick(props, "id", "series_id"),
        "station_id": pick(props, "estacion_id", "unid"),
        "station": pick(props, "nombre", "estacion", "nombre_estacion"),
        "river": pick(props, "rio", "curso", "nombre_rio"),
        "variable": pick(props, "var_nombre", "nombre_variable", "variable"),
        "var_id": pick(props, "var_id"),
        "unit": pick(props, "unidades", "unit_nombre", "abrev"),
        "network": pick(props, "red", "red_nombre", "fuente"),
        "province": pick(props, "provincia"),
        "procedure": pick(props, "proc_nombre", "procedimiento"),
        "lon": coords[0] if coords else None,
        "lat": coords[1] if len(coords) > 1 else None,
    }


def has_percentiles(sess, series_id):
    d = get_json(sess, "getPercentilesDiarios", params={"series_id": series_id}, retries=2)
    if not d or "__error__" in (d if isinstance(d, dict) else {}):
        return False
    if isinstance(d, list):
        return len(d) > 0
    if isinstance(d, dict):
        for key in ("percentiles", "data", "rows", "result"):
            if isinstance(d.get(key), list) and d[key]:
                return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=72,
                    help="recency window for a station to count as live")
    ap.add_argument("--percentile-sample", type=int, default=60,
                    help="how many live series to test for precomputed percentiles")
    ap.add_argument("--workers", type=int, default=6,
                    help="keep this low — it's a public agency server, be polite")
    ap.add_argument("--all-vars", action="store_true",
                    help="don't filter to hydro variables")
    args = ap.parse_args()

    sess = session_with_token()
    now = datetime.now(timezone.utc)
    lines = []

    def say(msg=""):
        print(msg)
        lines.append(msg)

    say(f"INA a5 coverage probe — {now:%Y-%m-%d %H:%M UTC}")
    say("=" * 64)

    say("\n[1/4] Pulling full series catalogue...")
    all_feats = fetch_catalogue(sess)
    if not all_feats:
        say("No catalogue returned. Check the base URL or whether a token is required.")
        sys.exit(1)
    rows = [parse_feature(f) for f in all_feats]
    say(f"      {len(rows)} point series returned")

    var_counts = Counter(f"{r['var_id']}: {r['variable']}" for r in rows)
    say("\n      variables present (verify the hydro ids against this list):")
    for v, n in var_counts.most_common(25):
        say(f"        {n:>6}  {v}")

    if not args.all_vars:
        before = len(rows)
        rows = [r for r in rows
                if str(r["var_id"]).isdigit() and int(r["var_id"]) in LIKELY_HYDRO_VARS]
        say(f"\n      filtered to hydro variables: {len(rows)} of {before}"
            f"  (rerun with --all-vars to keep everything)")

    say(f"\n[2/4] Pulling catalogue filtered to the last {args.hours}h...")
    live_feats = fetch_catalogue(sess, now - timedelta(hours=args.hours), now)
    live_ids = {str(parse_feature(f)["series_id"]) for f in live_feats}
    say(f"      {len(live_ids)} series report data in the window")

    if not live_ids:
        say("      (server may ignore the time filter on this endpoint — "
            "falling back to per-series probing would be the next step)")

    for r in rows:
        r["live"] = str(r["series_id"]) in live_ids

    live_rows = [r for r in rows if r["live"]]
    say(f"      live hydro series: {len(live_rows)} of {len(rows)} "
        f"({100 * len(live_rows) / max(len(rows), 1):.0f}%)")

    say("\n[3/4] Sampling for precomputed daily percentiles...")
    sample = random.sample(live_rows, min(args.percentile_sample, len(live_rows))) \
        if live_rows else []
    ok = 0
    if sample:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(has_percentiles, sess, r["series_id"]): r for r in sample}
            for fut in as_completed(futs):
                r = futs[fut]
                r["percentiles"] = fut.result()
                ok += bool(r["percentiles"])
        say(f"      {ok} of {len(sample)} sampled series have percentiles "
            f"({100 * ok / len(sample):.0f}%)")
        say("      -> this is your 'flow vs normal' colour. If it's low, you compute "
            "them yourself from getObservaciones history.")

    say("\n[4/4] Breakdowns (live hydro series only)")
    for label, key in (("network", "network"), ("province", "province"),
                       ("river", "river")):
        c = Counter(r[key] or "(unlabelled)" for r in live_rows)
        say(f"\n      by {label}:")
        for name, n in c.most_common(15):
            say(f"        {n:>5}  {name}")

    with_coords = [r for r in live_rows if r["lat"] and r["lon"]]
    say(f"\n      georeferenced: {len(with_coords)} of {len(live_rows)}")
    if with_coords:
        lats = [r["lat"] for r in with_coords]
        lons = [r["lon"] for r in with_coords]
        say(f"      bbox: lon {min(lons):.2f}..{max(lons):.2f}  "
            f"lat {min(lats):.2f}..{max(lats):.2f}")

    fields = ["series_id", "station_id", "station", "river", "variable", "var_id",
              "unit", "network", "province", "procedure", "lat", "lon", "live",
              "percentiles"]
    with open("a5_series_catalogue.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r.setdefault("percentiles", "")
            w.writerow(r)

    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
         "properties": {k: v for k, v in r.items() if k not in ("lat", "lon")}}
        for r in with_coords]}
    with open("a5_live_stations.geojson", "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False)

    with open("a5_coverage_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print("\nWrote a5_series_catalogue.csv, a5_live_stations.geojson, "
          "a5_coverage_report.txt")


if __name__ == "__main__":
    main()
