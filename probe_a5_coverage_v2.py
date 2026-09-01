#!/usr/bin/env python3
"""
probe_a5_coverage_v2.py — corrected coverage probe for the INA alerta5 (a5) API.

Fixes from v1, based on the real catalogue output:
  * variable filter corrected. v1 wrongly counted precipitation (ids 1, 31) as
    hydrology and missed id 101 (4-hourly level). Now only level/flow series
    at usable time resolution.
  * liveness is measured, not asked for. v1 trusted a timestart/timeend filter
    on the catalogue endpoint that the server silently ignores. v2 calls
    getObservaciones per series and reads the actual last timestamp.
  * catalogue pagination probed. v1 took 5000 at face value; that is suspiciously
    round and is probably a cap.
  * junk coordinates flagged instead of averaged into a bogus bounding box.
  * percentile test re-run against the clean pool only.

Usage:
  python3 probe_a5_coverage_v2.py                 # samples 400 series
  python3 probe_a5_coverage_v2.py --all           # probes every hydro series
  python3 probe_a5_coverage_v2.py --hours 168     # wider recency window

Runtime: roughly one second per 6 series probed. 400 series ~ 1 minute.

Outputs:
  a5_hydro_catalogue_v2.csv    every level/flow series + measured last timestamp
  a5_live_stations_v2.geojson  only series with recent data and valid coords
  a5_coverage_report_v2.txt    the printed summary
"""

import argparse
import csv
import json
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
UA = {"User-Agent": "coverage-probe/2.0 (hydrology portfolio research)"}

# Confirmed from the live catalogue. Level and flow only, at resolutions that
# can drive a daily map. Monthly aggregates (33, 48, 49, 50, 51, 52, 67) are
# deliberately excluded — they cannot colour a "today vs normal" map.
HYDRO_VARS = {
    2: "Altura hidrométrica",
    39: "Altura hidrométrica media diaria",
    101: "Altura hidrométrica 4-horaria",
    4: "Caudal",
    40: "Caudal medio diario",
}
# 19 is "par Altura/Caudal" — include with --include-pairs if you want it.
PAIR_VAR = 19

# Argentina + shared basins, generous. Anything outside is a bad coordinate.
BBOX = (-74.0, -56.0, -52.0, -20.0)  # lon_min, lat_min, lon_max, lat_max


def session_with_token():
    s = requests.Session()
    s.headers.update(UA)
    if os.environ.get("A5_TOKEN"):
        s.headers["Authorization"] = f"Bearer {os.environ['A5_TOKEN']}"
    return s


def get_json(sess, path, params=None, retries=3):
    url = path if path.startswith("http") else f"{BASE}/{path.lstrip('/')}"
    for attempt in range(retries):
        try:
            r = sess.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 401:
                raise SystemExit("401 Unauthorized — needs a token. "
                                 "Contact jbianchi@ina.gob.ar, then set A5_TOKEN.")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == retries - 1:
                return {"__error__": str(e)[:120]}
            time.sleep(1.5 * (attempt + 1))
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


def pick(d, *candidates, default=""):
    for c in candidates:
        for k, v in d.items():
            if (k == c or k.endswith("." + c)) and v not in (None, ""):
                return v
    return default


def parse_feature(f):
    props = flatten(f.get("properties", f))
    coords = (f.get("geometry") or {}).get("coordinates") or [None, None]
    lon = coords[0] if coords else None
    lat = coords[1] if len(coords) > 1 else None
    try:
        lon, lat = float(lon), float(lat)
    except (TypeError, ValueError):
        lon = lat = None
    return {
        "series_id": pick(props, "id", "series_id"),
        "station_id": pick(props, "estacion_id", "unid"),
        "station": pick(props, "nombre", "estacion", "nombre_estacion"),
        "river": pick(props, "rio", "curso", "nombre_rio"),
        "variable": pick(props, "var_nombre", "nombre_variable", "variable"),
        "var_id": pick(props, "var_id"),
        "unit": pick(props, "unidades", "unit_nombre", "abrev"),
        "network": pick(props, "red", "red_nombre", "fuente"),
        "procedure": pick(props, "proc_nombre", "procedimiento"),
        "lon": lon, "lat": lat,
    }


def coords_ok(r):
    if r["lon"] is None or r["lat"] is None:
        return False
    lo, la = r["lon"], r["lat"]
    if abs(lo) < 0.01 and abs(la) < 0.01:
        return False
    return BBOX[0] <= lo <= BBOX[2] and BBOX[1] <= la <= BBOX[3]


def fetch_catalogue(sess, limit=None, offset=None):
    params = {"format": "geojson"}
    if limit is not None:
        params["limit"] = limit
    if offset:
        params["offset"] = offset
    d = get_json(sess, "obs/puntual/series", params=params)
    if not d or (isinstance(d, dict) and "__error__" in d):
        return []
    feats = d.get("features") if isinstance(d, dict) else d
    return feats or []


def extract_observations(payload):
    """getObservaciones nests the rows differently depending on format. Dig."""
    if payload is None or (isinstance(payload, dict) and "__error__" in payload):
        return []
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and \
                any(k in payload[0] for k in ("timestart", "timeend", "valor", "fecha")):
            return payload
        for item in payload:
            got = extract_observations(item)
            if got:
                return got
        return []
    if isinstance(payload, dict):
        for key in ("observaciones", "data", "rows", "result", "series", "values"):
            if key in payload:
                got = extract_observations(payload[key])
                if got:
                    return got
    return []


def last_timestamp(obs):
    stamps = []
    for o in obs:
        for k in ("timestart", "fecha", "timeend", "time", "date"):
            v = o.get(k)
            if isinstance(v, str) and len(v) >= 10:
                stamps.append(v)
                break
    return max(stamps) if stamps else None


def probe_series(sess, r, since, until):
    d = get_json(sess, "getObservaciones",
                 params={"tipo": "puntual", "series_id": r["series_id"],
                         "timestart": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "timeend": until.strftime("%Y-%m-%dT%H:%M:%SZ")},
                 retries=2)
    obs = extract_observations(d)
    r["n_obs"] = len(obs)
    r["last_seen"] = last_timestamp(obs) or ""
    r["live"] = len(obs) > 0
    return r


def has_percentiles(sess, series_id):
    d = get_json(sess, "getPercentilesDiarios",
                 params={"series_id": series_id}, retries=2)
    if isinstance(d, list):
        return len(d) > 0
    if isinstance(d, dict) and "__error__" not in d:
        return any(isinstance(d.get(k), list) and d[k]
                   for k in ("percentiles", "data", "rows", "result"))
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--sample", type=int, default=400,
                    help="how many hydro series to probe for liveness")
    ap.add_argument("--all", action="store_true", help="probe every hydro series")
    ap.add_argument("--percentile-sample", type=int, default=60)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--include-pairs", action="store_true",
                    help="also include var 19 (par Altura/Caudal)")
    args = ap.parse_args()

    sess = session_with_token()
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)
    lines = []

    def say(m=""):
        print(m)
        lines.append(m)

    say(f"INA a5 coverage probe v2 — {now:%Y-%m-%d %H:%M UTC}")
    say("=" * 66)

    say("\n[1/5] Catalogue, and testing whether 5000 was a cap...")
    base = fetch_catalogue(sess)
    say(f"      default call: {len(base)} features")
    wide = fetch_catalogue(sess, limit=20000)
    say(f"      with limit=20000: {len(wide)} features")
    feats = wide if len(wide) > len(base) else base
    if len(wide) == len(base) == 5000:
        say("      -> still exactly 5000. Trying offset paging.")
        page, off = list(base), 5000
        while True:
            nxt = fetch_catalogue(sess, limit=5000, offset=off)
            if not nxt:
                break
            say(f"         offset {off}: +{len(nxt)}")
            page.extend(nxt)
            if len(nxt) < 5000 or off > 40000:
                break
            off += 5000
        if len(page) > len(feats):
            feats = page
    say(f"      catalogue size used: {len(feats)}")

    rows = [parse_feature(f) for f in feats]
    wanted = set(HYDRO_VARS)
    if args.include_pairs:
        wanted.add(PAIR_VAR)
    hydro = [r for r in rows
             if str(r["var_id"]).isdigit() and int(r["var_id"]) in wanted]

    say(f"\n[2/5] Level and flow series: {len(hydro)}")
    vc = Counter(int(r["var_id"]) for r in hydro)
    for vid, n in sorted(vc.items(), key=lambda x: -x[1]):
        say(f"        {n:>5}  {vid}: {HYDRO_VARS.get(vid, 'par Altura/Caudal')}")

    bad = [r for r in hydro if not coords_ok(r)]
    say(f"\n[3/5] Coordinates: {len(hydro) - len(bad)} valid, {len(bad)} junk or "
        f"outside {BBOX}")
    good = [r for r in hydro if coords_ok(r)]
    if good:
        los = [r["lon"] for r in good]
        las = [r["lat"] for r in good]
        say(f"      real bbox: lon {min(los):.2f}..{max(los):.2f}  "
            f"lat {min(las):.2f}..{max(las):.2f}")

    pool = good if good else hydro
    target = pool if args.all else random.sample(pool, min(args.sample, len(pool)))
    say(f"\n[4/5] Probing {len(target)} series for data in the last "
        f"{args.hours}h (this is the real test)...")
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(probe_series, sess, r, since, now) for r in target]
        for f in as_completed(futs):
            f.result()
            done += 1
            if done % 50 == 0:
                print(f"      {done}/{len(target)}...")
    live = [r for r in target if r.get("live")]
    pct = 100 * len(live) / max(len(target), 1)
    say(f"      LIVE: {len(live)} of {len(target)} probed  ({pct:.0f}%)")
    if not args.all:
        say(f"      extrapolated to the {len(pool)} georeferenced hydro series: "
            f"~{int(len(pool) * pct / 100)} live")

    say("\n      liveness by network:")
    bynet = defaultdict(lambda: [0, 0])
    for r in target:
        b = bynet[r["network"] or "(unlabelled)"]
        b[1] += 1
        b[0] += bool(r.get("live"))
    for net, (l, t) in sorted(bynet.items(), key=lambda x: -x[1][1]):
        say(f"        {net:<26} {l:>4}/{t:<4} ({100*l/t:>3.0f}%)")

    say(f"\n[5/5] Percentiles, retested on live level/flow series only...")
    samp = random.sample(live, min(args.percentile_sample, len(live))) if live else []
    ok = 0
    if samp:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(has_percentiles, sess, r["series_id"]): r for r in samp}
            for f in as_completed(futs):
                futs[f]["percentiles"] = f.result()
                ok += bool(futs[f]["percentiles"])
        say(f"      {ok} of {len(samp)} have precomputed percentiles "
            f"({100*ok/len(samp):.0f}%)")
        say("      >50%: colour the map straight from INA.")
        say("      <50%: pull history via getObservaciones and compute your own "
            "day-of-year distributions.")

    fields = ["series_id", "station_id", "station", "river", "variable", "var_id",
              "unit", "network", "procedure", "lat", "lon", "live", "n_obs",
              "last_seen", "percentiles"]
    with open("a5_hydro_catalogue_v2.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in hydro:
            for k in ("live", "n_obs", "last_seen", "percentiles"):
                r.setdefault(k, "")
            w.writerow(r)

    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
         "properties": {k: v for k, v in r.items() if k not in ("lat", "lon")}}
        for r in live if coords_ok(r)]}
    with open("a5_live_stations_v2.geojson", "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False)

    with open("a5_coverage_report_v2.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print("\nWrote a5_hydro_catalogue_v2.csv, a5_live_stations_v2.geojson, "
          "a5_coverage_report_v2.txt")


if __name__ == "__main__":
    main()
