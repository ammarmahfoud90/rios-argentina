#!/usr/bin/env python3
"""
probe_a5_join_v4.py — the two questions that decide whether the map gets built.

v3 established that limit/offset paging works and that a quarter of short-record
live stations sit at ~0 km from a BDHI historical station. This run turns both
of those from samples into facts.

  A. TRUE CATALOGUE SIZE. v3 proved 5000 was a cap. Page the whole thing with
     limit/offset and count what is really there, by variable and by network.
     Everything downstream is a fraction of this number.

  B. CAN THE METADATA BE TRUSTED? The catalogue carries date_range.timestart /
     timeend for most series. If those agree with what getObservaciones actually
     returns, you never probe for liveness or record length again — the whole
     roster is one HTTP call. This validates the shortcut on a sample instead of
     assuming it. This is the step that makes every future run cheap.

  C. THE SAME-SITE JOIN. For each live modern series, is there a BDHI series at
     the same site? Two keys, reported separately:
       - same estacion_id            (definitive: the agency says it's one site)
       - within N km                 (probable: needs the river name to agree)
     Then: how long is the COMBINED record, and how many stations clear the bar
     for an honest day-of-year distribution?

  D. THE ROSTER. Writes every station with a tier:
       tier 1  own record long enough — colour it, full confidence
       tier 2  joined to a historical twin and the combined record is long enough
       tier 3  live but no usable history — show the reading, do not colour it

Usage:
  python3 probe_a5_join_v4.py                  # validation sample of 60
  python3 probe_a5_join_v4.py --validate 150   # trust the metadata less
  python3 probe_a5_join_v4.py --max-km 2       # tighter same-site radius
  python3 probe_a5_join_v4.py --skip-validation

Runtime: paging is ~10-30 requests. Validation is 2 requests per sampled series.
The join itself is local computation. Expect 2-4 minutes at defaults.

Outputs:
  a5_catalogue_full_v4.csv   every hydro series in the real catalogue
  a5_joins_v4.csv            every candidate pairing with distance + river check
  a5_roster_v4.geojson       tiered stations, ready for deck.gl
  a5_report_v4.txt           the summary
"""

import argparse
import csv
import json
import math
import os
import random
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://alerta.ina.gob.ar/a5"
TIMEOUT = 45
UA = {"User-Agent": "coverage-probe/4.0 (hydrology portfolio research)"}

HYDRO_VARS = {2: "Altura hidrométrica", 39: "Altura h. media diaria",
              101: "Altura h. 4-horaria", 4: "Caudal", 40: "Caudal medio diario"}
BBOX = (-74.0, -56.0, -52.0, -20.0)

HISTORICAL_NETWORKS = {"alturas_bdhi"}
MIN_YEARS = 10           # bar for an honest day-of-year distribution
LIVE_DAYS = 7            # last data within this many days = live
PAGE_SIZE = 1000
GRID_DEG = 0.5           # spatial index cell size for the join


# ---------------------------------------------------------------- plumbing

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
        "station_id": pick(props, "estacion_id", "unid"),
        "station": pick(props, "nombre", "estacion", "nombre_estacion"),
        "river": pick(props, "rio", "curso", "nombre_rio"),
        "var_id": pick(props, "var_id"),
        "network": pick(props, "red", "red_nombre", "fuente"),
        "lon": lon, "lat": lat,
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


def as_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


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


def haversine(lon1, lat1, lon2, lat2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def norm_name(s):
    """Loose river-name comparison. Accents and case vary across networks."""
    s = (str(s) or "").lower().strip()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"),
                 ("ü", "u"), ("ñ", "n")):
        s = s.replace(a, b)
    for junk in ("rio ", "río ", "arroyo ", "aº ", "a\u00b0 "):
        if s.startswith(junk):
            s = s[len(junk):]
    return " ".join(s.split())


# ---------------------------------------------------------------- [A] paging

def page_catalogue(sess, say, page_size=PAGE_SIZE, hard_stop=60000):
    """Page with limit/offset until a short page or no new ids arrive."""
    say("\n[A] Paging the full catalogue...")
    seen, feats, offset = set(), [], 0
    while offset < hard_stop:
        d = get_json(sess, "obs/puntual/series",
                     params={"format": "geojson", "limit": page_size,
                             "offset": offset})
        page = (d or {}).get("features", []) or []
        if not page:
            break
        fresh = 0
        for f in page:
            sid = str(parse_feature(f)["series_id"])
            if sid and sid not in seen:
                seen.add(sid)
                feats.append(f)
                fresh += 1
        say(f"    offset {offset:>6}: {len(page):>5} returned, {fresh:>5} new "
            f"(running total {len(feats)})")
        if fresh == 0:
            say("    -> no new ids; offset is being ignored. Stopping.")
            break
        if len(page) < page_size:
            break
        offset += page_size
    say(f"    TRUE CATALOGUE SIZE: {len(feats)} unique series")
    if len(feats) <= 5000:
        say("    (at or below the old 5000 ceiling — the cap was the whole thing)")
    return feats


# ---------------------------------------------------------- [B] metadata test

def validate_metadata(sess, rows, now, n, say):
    """Does date_range agree with reality? If yes, stop probing forever."""
    say(f"\n[B] Validating catalogue date_range against real observations "
        f"(sample of {n})...")
    dated = [r for r in rows if r.get("cat_end") and r.get("cat_start")]
    if not dated:
        say("    no series carry date_range. Metadata shortcut unavailable.")
        return None
    sample = random.sample(dated, min(n, len(dated)))

    def check(r):
        end = as_date(r["cat_end"])
        start = as_date(r["cat_start"])
        out = {"end_ok": None, "start_ok": None}
        if end:
            got = obs_between(sess, r["series_id"],
                              end - timedelta(days=20), end + timedelta(days=2))
            out["end_ok"] = bool(got)
        if start:
            got = obs_between(sess, r["series_id"],
                              start - timedelta(days=2), start + timedelta(days=20))
            out["start_ok"] = bool(got)
        r["_val"] = out
        return r

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(as_completed([ex.submit(check, r) for r in sample]))

    e_ok = sum(1 for r in sample if r.get("_val", {}).get("end_ok"))
    s_ok = sum(1 for r in sample if r.get("_val", {}).get("start_ok"))
    say(f"    stated END has data around it:   {e_ok}/{len(sample)} "
        f"({100*e_ok/len(sample):.0f}%)")
    say(f"    stated START has data after it:  {s_ok}/{len(sample)} "
        f"({100*s_ok/len(sample):.0f}%)")
    good = (e_ok + s_ok) / (2 * len(sample))
    if good >= 0.8:
        say("    -> metadata is reliable. Build the roster from the catalogue "
            "alone; no per-series probing needed, ever.")
    elif good >= 0.5:
        say("    -> metadata is roughly right but not dependable. Use it to "
            "rank, verify the shortlist you actually publish.")
    else:
        say("    -> metadata does not match reality. Keep probing per series.")
    return good


# ------------------------------------------------------------- [C] the join

def build_grid(donors, cell=GRID_DEG):
    grid = defaultdict(list)
    for h in donors:
        grid[(int(h["lat"] // cell), int(h["lon"] // cell))].append(h)
    return grid


def near_donors(grid, r, cell=GRID_DEG):
    ci, cj = int(r["lat"] // cell), int(r["lon"] // cell)
    out = []
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            out.extend(grid.get((ci + di, cj + dj), []))
    return out


def join_sites(modern, donors, max_km, say):
    say(f"\n[C] Same-site join: {len(modern)} live modern series against "
        f"{len(donors)} historical series")

    by_station = defaultdict(list)
    for h in donors:
        if h.get("station_id"):
            by_station[str(h["station_id"])].append(h)

    grid = build_grid(donors)
    id_hits = coord_hits = 0

    for r in modern:
        match, how, dist = None, "", None

        cand = by_station.get(str(r.get("station_id") or ""), [])
        if cand:
            match = max(cand, key=lambda h: span_years(h) or 0)
            how, dist = "station_id", 0.0
            id_hits += 1
        else:
            best, bd = None, 1e9
            for h in near_donors(grid, r):
                dkm = haversine(r["lon"], r["lat"], h["lon"], h["lat"])
                if dkm < bd:
                    best, bd = h, dkm
            if best and bd <= max_km:
                match, how, dist = best, "coords", round(bd, 2)
                coord_hits += 1

        r["donor_series_id"] = match["series_id"] if match else ""
        r["donor_station"] = match["station"] if match else ""
        r["donor_river"] = match["river"] if match else ""
        r["match_type"] = how
        r["match_km"] = dist if dist is not None else ""
        r["river_agrees"] = ""
        if match:
            a, b = norm_name(r["river"]), norm_name(match["river"])
            if a and b:
                r["river_agrees"] = "yes" if (a == b or a in b or b in a) else "NO"
            else:
                r["river_agrees"] = "unknown"
            r["combined_years"] = combined_span(r, match)
        else:
            r["combined_years"] = span_years(r)

    say(f"    matched by station_id: {id_hits}")
    say(f"    matched by coordinates within {max_km} km: {coord_hits}")
    say(f"    unmatched: {len(modern) - id_hits - coord_hits}")

    checked = [r for r in modern if r["river_agrees"] in ("yes", "NO")]
    bad = [r for r in checked if r["river_agrees"] == "NO"]
    if checked:
        say(f"    river name agrees on {len(checked)-len(bad)}/{len(checked)} "
            f"checkable pairs ({len(bad)} disagree — inspect these by hand)")
    return id_hits, coord_hits


def span_years(r):
    s, e = as_date(r.get("cat_start")), as_date(r.get("cat_end"))
    if not s or not e:
        return None
    return round((e - s).days / 365.25, 1)


def combined_span(a, b):
    starts = [d for d in (as_date(a.get("cat_start")), as_date(b.get("cat_start"))) if d]
    ends = [d for d in (as_date(a.get("cat_end")), as_date(b.get("cat_end"))) if d]
    if not starts or not ends:
        return None
    return round((max(ends) - min(starts)).days / 365.25, 1)


# ------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", type=int, default=60,
                    help="how many series to check catalogue dates against")
    ap.add_argument("--skip-validation", action="store_true")
    ap.add_argument("--max-km", type=float, default=1.0,
                    help="radius that counts as the same physical site")
    ap.add_argument("--min-years", type=float, default=MIN_YEARS)
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE)
    args = ap.parse_args()

    sess = session_with_token()
    now = datetime.now(timezone.utc)
    lines = []

    def say(m=""):
        print(m)
        lines.append(m)

    say(f"INA a5 join probe v4 — {now:%Y-%m-%d %H:%M UTC}")
    say("=" * 66)

    feats = page_catalogue(sess, say, page_size=args.page_size)
    if not feats:
        say("Catalogue empty. Check the endpoint.")
        return

    rows = [parse_feature(f) for f in feats]
    hydro = [r for r in rows if str(r["var_id"]).isdigit()
             and int(r["var_id"]) in HYDRO_VARS and coords_ok(r)]
    say(f"\n    georeferenced level/flow series: {len(hydro)}")
    for vid, n in Counter(int(r["var_id"]) for r in hydro).most_common():
        say(f"      {n:>5}  {vid}: {HYDRO_VARS[vid]}")
    say("\n    by network:")
    for net, n in Counter(r["network"] or "(none)" for r in hydro).most_common(12):
        say(f"      {n:>5}  {net}")

    dated = sum(1 for r in hydro if r.get("cat_start"))
    say(f"\n    carry a date_range: {dated} of {len(hydro)} "
        f"({100*dated/max(len(hydro),1):.0f}%)")

    if not args.skip_validation:
        validate_metadata(sess, hydro, now, args.validate, say)

    # live = catalogue says data within LIVE_DAYS
    cutoff = now - timedelta(days=LIVE_DAYS)
    donors = [r for r in hydro if r["network"] in HISTORICAL_NETWORKS]
    modern = [r for r in hydro if r["network"] not in HISTORICAL_NETWORKS
              and (as_date(r.get("cat_end")) or datetime.min.replace(
                  tzinfo=timezone.utc)) >= cutoff]
    say(f"\n    live per catalogue (data within {LIVE_DAYS}d): {len(modern)}")
    say(f"    historical donor series: {len(donors)}")

    join_sites(modern, donors, args.max_km, say)

    for r in modern:
        own = span_years(r)
        comb = r.get("combined_years")
        if own is not None and own >= args.min_years:
            r["tier"] = 1
        elif comb is not None and comb >= args.min_years and r["donor_series_id"]:
            r["tier"] = 2
        else:
            r["tier"] = 3

    say(f"\n[D] Roster (bar = {args.min_years} yr of record)")
    tc = Counter(r["tier"] for r in modern)
    say(f"    tier 1  own record long enough        {tc[1]:>5}")
    say(f"    tier 2  long enough once joined       {tc[2]:>5}")
    say(f"    tier 3  live, no usable history       {tc[3]:>5}")
    colourable = tc[1] + tc[2]
    say(f"    -> {colourable} of {len(modern)} live stations can be coloured "
        f"honestly ({100*colourable/max(len(modern),1):.0f}%)")
    if tc[2]:
        say(f"    the join adds {tc[2]} stations that tier 1 alone would miss "
            f"({100*tc[2]/max(colourable,1):.0f}% of the coloured map)")

    say("\n    tier 1 and 2 by river (top 15):")
    for riv, n in Counter(r["river"] or "(none)" for r in modern
                          if r["tier"] < 3).most_common(15):
        say(f"      {n:>4}  {riv}")

    cat_fields = ["series_id", "station_id", "station", "river", "var_id",
                  "network", "lat", "lon", "cat_start", "cat_end"]
    with open("a5_catalogue_full_v4.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cat_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(hydro)

    join_fields = ["series_id", "station", "river", "network", "lat", "lon",
                   "cat_start", "cat_end", "donor_series_id", "donor_station",
                   "donor_river", "match_type", "match_km", "river_agrees",
                   "combined_years", "tier"]
    with open("a5_joins_v4.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=join_fields, extrasaction="ignore")
        w.writeheader()
        for r in modern:
            for k in join_fields:
                r.setdefault(k, "")
            w.writerow(r)

    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
         "properties": {k: v for k, v in r.items()
                        if k not in ("lat", "lon") and not k.startswith("_")}}
        for r in modern]}
    with open("a5_roster_v4.geojson", "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False)

    with open("a5_report_v4.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("\nWrote a5_catalogue_full_v4.csv, a5_joins_v4.csv, "
          "a5_roster_v4.geojson, a5_report_v4.txt")


if __name__ == "__main__":
    main()
