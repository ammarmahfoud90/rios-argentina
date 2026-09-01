#!/usr/bin/env python3
"""
repair_climatology.py — fix the two faults the first build exposed.

FAULT 1 — silent splice failures.
    Corrientes: the 126-year series says 2.69 m and normal, the spliced 26-year
    series at the same wharf says 7.01 m and never-higher. The two records sit
    on different gauge zeros and the offset correction did not catch it, so the
    "history" is two populations stacked on top of each other and today's value
    scores 100 against the older, lower one.

    The old test compared the two series only where they overlapped in time, and
    used a fixed 1.0 m threshold. Both were wrong. Two records that never
    overlap got no test at all, and 1.0 m means something different on the
    Paraná than on an arroyo.

    The new test removes seasonality first — it compares the donor's day-of-year
    median against the modern series' day-of-year median, so it works with no
    temporal overlap at all — and judges the resulting shift against the
    record's OWN spread, not against a fixed number of metres.

    Where the shift can be verified against a real overlap it is corrected.
    Where it cannot, the station is DEMOTED rather than guessed at, because a
    seasonal-median offset also absorbs genuine hydrological change, and
    correcting with it would quietly erase the signal the map exists to show.
    --correct-inferred overrides this; the stations it affects are listed.

FAULT 2 — one place counted many times.
    399 published stations were 155 physical sites. INA lists the same gauge as
    instantaneous level, daily mean and 4-hourly as separate series. At 29 sites
    those siblings disagreed about today. The map should show a place once.
    Collapse prefers daily-mean series, then the longer record — a spot reading
    at an arbitrary hour is what drove most of the disagreements.

No downloads except the catalogue (cached after the first run). Everything else
is read from ./cache, so this is fast and repeatable.

Usage:
  python3 repair_climatology.py
  python3 repair_climatology.py --group-km 0.5 --shift-ratio 0.5

Outputs:
  stations_final.geojson   one feature per site — this is the map layer
  climatology_final.json   percentile curves for the survivors
  repair_report.txt        every demotion and every disagreement, with reasons
"""

import argparse
import csv
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

import requests

BASE = "https://alerta.ina.gob.ar/a5"
CACHE_DIR = "cache"
CAT_CACHE = os.path.join(CACHE_DIR, "_catalogue.json")

LEVEL_VARS = {2, 39, 101}
FLOW_VARS = {4, 40}
DAILY_MEAN_VARS = {39, 40}          # preferred: a full day, not one instant
INSTANT_VARS = {2, 4}
PCTLS = [5, 10, 25, 50, 75, 90, 95]
WINDOW = 7
MIN_YEARS_PER_DOY = 8
MIN_DONOR_DAYS = 400                # below this a donor cannot be datum-tested
MIN_OVERLAP_DAYS = 30


# ----------------------------------------------------------------- helpers

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


def load_catalogue(say):
    if os.path.exists(CAT_CACHE):
        with open(CAT_CACHE, encoding="utf-8") as fh:
            cat = json.load(fh)
        say(f"  catalogue from cache: {len(cat)} series")
        return cat
    say("  fetching catalogue (once; cached afterwards)...")
    sess = requests.Session()
    sess.headers.update({"User-Agent": "climatology-repair/1.0"})
    if os.environ.get("A5_TOKEN"):
        sess.headers["Authorization"] = f"Bearer {os.environ['A5_TOKEN']}"
    cat, seen, offset = {}, set(), 0
    while offset < 60000:
        r = sess.get(f"{BASE}/obs/puntual/series",
                     params={"format": "geojson", "limit": 1000, "offset": offset},
                     timeout=60)
        feats = (r.json() or {}).get("features", []) or []
        if not feats:
            break
        fresh = 0
        for f in feats:
            props = flatten(f.get("properties", f))
            sid = str(pick(props, "id", "series_id"))
            if sid and sid not in seen:
                seen.add(sid)
                fresh += 1
                cat[sid] = {"var_id": pick(props, "var_id")}
        if fresh == 0 or len(feats) < 1000:
            break
        offset += 1000
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CAT_CACHE, "w", encoding="utf-8") as fh:
        json.dump(cat, fh)
    say(f"  catalogue: {len(cat)} series")
    return cat


def load_daily(sid):
    p = os.path.join(CACHE_DIR, f"{sid}.json")
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


def doy(d):
    n = d.timetuple().tm_yday
    if d.month > 2 and (d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)):
        n -= 1
    return min(max(n, 1), 365)


def quantile(sv, q):
    if not sv:
        return None
    if len(sv) == 1:
        return sv[0]
    pos = q * (len(sv) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sv) - 1)
    return sv[lo] * (1 - (pos - lo)) + sv[hi] * (pos - lo)


def iqr(vals):
    s = sorted(vals)
    if len(s) < 8:
        return None
    return quantile(s, 0.75) - quantile(s, 0.25)


# ------------------------------------------------------- the datum seam test

def doy_medians(daily, window=WINDOW):
    """Median value for each day of year, pooled over +/- window across years."""
    buck = defaultdict(list)
    for d, v in daily:
        buck[doy(d)].append(v)
    out = {}
    for n in range(1, 366):
        pool = []
        for off in range(-window, window + 1):
            pool.extend(buck.get(((n - 1 + off) % 365) + 1, []))
        if len(pool) >= 5:
            out[n] = statistics.median(pool)
    return out


def seasonal_offset(modern, donor):
    """
    Median (modern - donor) matched day-of-year for day-of-year.

    Seasonality is removed before comparing, so this works even when the two
    records never overlap in time — which is exactly the case the old overlap
    test could not see.
    """
    a, b = doy_medians(modern), doy_medians(donor)
    common = [a[n] - b[n] for n in a if n in b]
    if len(common) < 60:
        return None, len(common)
    return statistics.median(common), len(common)


def overlap_offset(modern, donor):
    md = dict(modern)
    diffs = [md[d] - v for d, v in donor if d in md]
    if len(diffs) < MIN_OVERLAP_DAYS:
        return None, len(diffs)
    return statistics.median(diffs), len(diffs)


def assess_join(modern, donor, shift_ratio):
    """
    Returns (verdict, offset, detail).
      verdict 'clean'     — no meaningful shift, splice as-is
              'corrected' — real overlap measured the shift, apply it
              'unverified'— shift is real but unmeasurable against an overlap
              'unusable'  — donor too short to judge at all
    """
    if len(donor) < MIN_DONOR_DAYS:
        return "unusable", 0.0, f"donor only {len(donor)} days"

    spread = iqr([v for _, v in donor]) or iqr([v for _, v in modern])
    if not spread or spread <= 0:
        return "unusable", 0.0, "donor has no usable spread"

    s_off, n_doy = seasonal_offset(modern, donor)
    if s_off is None:
        return "unusable", 0.0, f"only {n_doy} comparable days of year"

    ratio = abs(s_off) / spread
    o_off, n_ov = overlap_offset(modern, donor)

    if ratio < shift_ratio:
        return "clean", (o_off if o_off is not None else 0.0), \
               f"shift {s_off:+.2f} = {ratio:.2f} of spread {spread:.2f}"

    if o_off is not None:
        return "corrected", o_off, \
               (f"shift {s_off:+.2f} ({ratio:.2f} of spread), "
                f"corrected by {o_off:+.2f} from {n_ov} overlapping days")

    return "unverified", s_off, \
           (f"shift {s_off:+.2f} = {ratio:.2f} of spread {spread:.2f}, "
            f"no temporal overlap to verify it")


def splice(modern, donor, offset):
    out = {d: v + offset for d, v in donor}
    out.update(dict(modern))
    return sorted(out.items())


# ------------------------------------------------------------- climatology

def climatology(daily, window, min_years):
    buck = defaultdict(list)
    for d, v in daily:
        buck[doy(d)].append((d.year, v))
    curves, ok_days = {}, 0
    for n in range(1, 366):
        pool, years = [], set()
        for off in range(-window, window + 1):
            for y, v in buck.get(((n - 1 + off) % 365) + 1, []):
                pool.append(v)
                years.add(y)
        if len(pool) < 10 or len(years) < min_years:
            continue
        pool.sort()
        curves[n] = {str(p): round(quantile(pool, p / 100), 4) for p in PCTLS}
        ok_days += 1
    return curves


def rank_against(daily, ref_date, value, window, exclude_year=True):
    n = doy(ref_date)
    pool = []
    for d, v in daily:
        k = doy(d)
        if min(abs(k - n), 365 - abs(k - n)) <= window:
            if exclude_year and d.year == ref_date.year:
                continue
            pool.append(v)
    if len(pool) < 10:
        return None
    below = sum(1 for v in pool if v < value)
    equal = sum(1 for v in pool if v == value)
    return round(100 * (below + 0.5 * equal) / len(pool), 1)


def classify(r):
    if r is None:
        return "unknown"
    return ("much_below" if r < 5 else "below" if r < 25 else
            "normal" if r <= 75 else "above" if r <= 95 else "much_above")


def modern_era_bias(daily, window):
    """
    Where does the recent era sit inside its own climatology?

    A harmonised record puts it near the middle. Far off centre means either a
    residual datum step or a genuine multi-year trend — a bajante looks like
    this too, so it is reported, never acted on automatically.
    """
    if not daily:
        return None
    last = daily[-1][0]
    cutoff = date(last.year - 2, last.month, last.day)
    recent = [(d, v) for d, v in daily if d >= cutoff]
    if len(recent) < 200:
        return None
    ranks = [rank_against(daily, d, v, window) for d, v in recent[::7]]
    ranks = [r for r in ranks if r is not None]
    return round(statistics.median(ranks), 1) if ranks else None


# ------------------------------------------------------------------ layout

def haversine_km(lon1, lat1, lon2, lat2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def group_sites(recs, max_km):
    """Cluster records into physical sites by proximity."""
    grid = defaultdict(list)
    for r in recs:
        grid[(round(r["lat"] / 0.02), round(r["lon"] / 0.02))].append(r)
    seen, sites = set(), []
    for r in recs:
        if id(r) in seen:
            continue
        gy, gx = round(r["lat"] / 0.02), round(r["lon"] / 0.02)
        members = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for o in grid.get((gy + dy, gx + dx), []):
                    if id(o) not in seen and \
                            haversine_km(r["lon"], r["lat"], o["lon"], o["lat"]) <= max_km:
                        members.append(o)
                        seen.add(id(o))
        if members:
            sites.append(members)
    return sites


def preference(rec):
    """Lower sorts better: daily mean first, then long record, then coverage."""
    v = rec["var_id"]
    kind = 0 if v in DAILY_MEAN_VARS else 1 if v in INSTANT_VARS else 2
    return (kind, -rec["years"], -rec["days_covered"])


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--joins", default="a5_joins_v4.csv")
    ap.add_argument("--window", type=int, default=WINDOW)
    ap.add_argument("--min-years", type=int, default=MIN_YEARS_PER_DOY)
    ap.add_argument("--group-km", type=float, default=1.0,
                    help="two series this close are treated as one place. The "
                         "real Corrientes pair sits 0.74 km apart, so 0.5 misses it")
    ap.add_argument("--shift-ratio", type=float, default=0.5,
                    help="datum shift counts as real above this fraction of the "
                         "record's own interquartile spread")
    ap.add_argument("--correct-inferred", action="store_true",
                    help="apply seasonal offsets that no overlap can verify "
                         "(off by default — it can erase real trends)")
    args = ap.parse_args()

    lines = []

    def say(m=""):
        print(m)
        lines.append(m)

    say(f"Climatology repair — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    say("=" * 66)

    cat = load_catalogue(say)
    with open(args.joins, encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("tier") in ("1", "2")]
    say(f"  {len(rows)} tier 1/2 stations to reassess\n")

    say("[1] Datum seam test")
    verdicts = Counter()
    demoted, records = [], []

    for r in rows:
        sid = str(r["series_id"])
        try:
            vid = int(cat.get(sid, {}).get("var_id"))
        except (TypeError, ValueError):
            continue
        if vid not in LEVEL_VARS | FLOW_VARS:
            continue
        daily = load_daily(sid)
        if not daily:
            continue

        verdict, offset, detail = "solo", 0.0, ""
        dsid = str(r.get("donor_series_id") or "")
        if dsid:
            try:
                dvid = int(cat.get(dsid, {}).get("var_id"))
            except (TypeError, ValueError):
                dvid = -1
            same_family = ((vid in LEVEL_VARS) == (dvid in LEVEL_VARS)) and \
                          dvid in LEVEL_VARS | FLOW_VARS
            donor = load_daily(dsid) if same_family else []
            if not same_family:
                verdict, detail = "unusable", "donor measures a different quantity"
            elif not donor:
                verdict, detail = "unusable", "donor not in cache"
            else:
                verdict, offset, detail = assess_join(daily, donor, args.shift_ratio)
                if verdict in ("clean", "corrected") or \
                        (verdict == "unverified" and args.correct_inferred):
                    daily = splice(daily, donor, offset)
                else:
                    demoted.append(f"    {sid:>7} {str(r.get('station'))[:34]:<34} "
                                   f"{verdict}: {detail}")
        verdicts[verdict] += 1

        curves = climatology(daily, args.window, args.min_years)
        if not curves:
            continue
        last_d, last_v = daily[-1]
        rank = rank_against(daily, last_d, last_v, args.window)
        try:
            lon, lat = float(r["lon"]), float(r["lat"])
        except (TypeError, ValueError, KeyError):
            continue
        records.append({
            "sid": sid, "station": r.get("station"), "river": r.get("river"),
            "lat": lat, "lon": lon, "var_id": vid,
            "family": "level" if vid in LEVEL_VARS else "flow",
            "tier": r.get("tier"), "donor": dsid, "verdict": verdict,
            "detail": detail, "offset": round(offset, 3),
            "years": len({d.year for d, _ in daily}),
            "days_covered": len(curves), "curves": curves,
            "last_date": last_d.isoformat(), "last_value": round(last_v, 3),
            "rank": rank, "class": classify(rank),
            "era_bias": modern_era_bias(daily, args.window),
        })

    for k in ("solo", "clean", "corrected", "unverified", "unusable"):
        if verdicts[k]:
            say(f"    {k:<11} {verdicts[k]:>4}")
    say(f"    {len(records)} series carry a usable climatology")
    if demoted:
        say(f"\n    demoted to no-history ({len(demoted)}):")
        for d in demoted[:20]:
            say(d)
        if len(demoted) > 20:
            say(f"    ... {len(demoted)-20} more in repair_report.txt")

    # ---- collapse to one series per physical site
    say(f"\n[2] Collapsing to one series per site (within {args.group_km} km)")
    sites = group_sites(records, args.group_km)
    say(f"    {len(records)} series -> {len(sites)} sites")

    ORD = ["much_below", "below", "normal", "above", "much_above"]
    conflicts, chosen = [], []
    for members in sites:
        classes = {m["class"] for m in members if m["class"] != "unknown"}
        if len(classes) > 1:
            span = max(ORD.index(c) for c in classes) - min(ORD.index(c) for c in classes)
            conflicts.append((span, members))
        members.sort(key=preference)
        w = members[0]
        w["siblings"] = len(members) - 1
        w["sibling_classes"] = sorted(classes)
        chosen.append(w)

    say(f"    {len(conflicts)} sites had siblings disagreeing before collapse")
    conflicts.sort(key=lambda x: -x[0])
    for span, members in conflicts[:10]:
        say(f"      span {span}  {str(members[0]['station'])[:30]:<30} " +
            " ".join(f"{m['class']}({m['rank']},v{m['var_id']})" for m in members))

    # ---- final distribution
    say("\n[3] Final map layer")
    cls = Counter(c["class"] for c in chosen)
    n = len(chosen)
    for k in ORD:
        if cls[k]:
            say(f"    {k:<12} {cls[k]:>4}  ({100*cls[k]/n:.0f}%)")
    hi = 100 * (cls["above"] + cls["much_above"]) / n
    lo = 100 * (cls["below"] + cls["much_below"]) / n
    say(f"    above-ish {hi:.0f}%, below-ish {lo:.0f}%  (25/25 expected by chance)")

    ys = sorted(c["years"] for c in chosen)
    say(f"    years of record: median {ys[len(ys)//2]}, min {ys[0]}, max {ys[-1]}")

    skewed = [c for c in chosen if c["era_bias"] is not None
              and (c["era_bias"] < 15 or c["era_bias"] > 85)]
    if skewed:
        say(f"\n    {len(skewed)} sites where the last two years sit far off centre "
            f"in their own record.")
        say("    Could be a residual datum step, could be a real multi-year trend. "
            "Not corrected — check by hand:")
        for c in sorted(skewed, key=lambda x: x["era_bias"])[:12]:
            say(f"      {str(c['station'])[:32]:<32} era rank {c['era_bias']:>5} "
                f"| {c['years']} yr | {c['verdict']}")

    # ---- write
    feats = []
    clim = {}
    for c in chosen:
        clim[c["sid"]] = {
            "station": c["station"], "river": c["river"],
            "variable_family": c["family"], "years": c["years"],
            "days_covered": c["days_covered"], "percentiles": c["curves"],
        }
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
            "properties": {
                "series_id": c["sid"], "station": c["station"], "river": c["river"],
                "tier": c["tier"], "variable": c["family"], "var_id": c["var_id"],
                "last_date": c["last_date"], "last_value": c["last_value"],
                "percentile": c["rank"], "class": c["class"],
                "years_of_record": c["years"], "days_with_climatology": c["days_covered"],
                "join_verdict": c["verdict"], "datum_offset": c["offset"],
                "siblings_collapsed": c["siblings"],
                "sibling_classes": c["sibling_classes"],
                "recent_era_rank": c["era_bias"],
            },
        })

    with open("stations_final.geojson", "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh, ensure_ascii=False)
    with open("climatology_final.json", "w", encoding="utf-8") as fh:
        json.dump(clim, fh, ensure_ascii=False)
    with open("repair_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n\nALL DEMOTIONS\n" + "\n".join(demoted) + "\n")

    print("\nWrote stations_final.geojson, climatology_final.json, repair_report.txt")


if __name__ == "__main__":
    main()
