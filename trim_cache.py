#!/usr/bin/env python3
"""
trim_cache.py — make a small, committable copy of the cache.

The full cache is ~95 MB because it holds every daily reading back to 1900.
That belongs on your machine, not in git, where a daily rewrite of 148 files
would bloat the history forever.

The daily build does not need it. Percentiles come from the curves frozen in
climatology_final.json. The cache is only read for the guards, and the
deepest of those — the sustained-step test — looks back about three years.

So: keep the full cache locally for rerunning the repair, and commit a
recent slice that is enough for the guards to work in CI.

    python3 trim_cache.py            # default 6 years
    python3 trim_cache.py --years 4

Writes cache_recent/
"""
import argparse, glob, json, os, shutil
from datetime import datetime, date, timedelta

ap = argparse.ArgumentParser()
ap.add_argument("--years", type=int, default=6,
                help="how much history to keep; guards need ~3, so 6 is slack")
ap.add_argument("--src", default="cache")
ap.add_argument("--dst", default="cache_recent")
ap.add_argument("--all-series", action="store_true",
                help="keep every cached series, not just the ones on the map")
a = ap.parse_args()

# The cache holds every series ever probed — donors, rejected siblings,
# candidates that lost the collapse. The map reads only the chosen ones,
# so by default that is all we commit.
wanted = None
if not a.all_series and os.path.exists("stations_snapped.json"):
    wanted = {str(x.get("series_id"))
              for x in json.load(open("stations_snapped.json", encoding="utf-8"))}
    print(f"keeping only the {len(wanted)} series the map actually reads")

cutoff = date.today() - timedelta(days=int(a.years * 365.25))
if os.path.isdir(a.dst):
    shutil.rmtree(a.dst)
os.makedirs(a.dst)

files = sorted(glob.glob(os.path.join(a.src, "*.json")))
kept_rows = dropped = 0
for p in files:
    name = os.path.basename(p)
    if name.startswith("_"):
        continue
    if wanted is not None and name[:-5] not in wanted:
        continue
    try:
        rows = json.load(open(p, encoding="utf-8"))
    except (ValueError, OSError):
        continue
    keep = []
    for d, v in rows:
        try:
            if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff:
                keep.append([d, v])
            else:
                dropped += 1
        except (ValueError, TypeError):
            continue
    kept_rows += len(keep)
    with open(os.path.join(a.dst, name), "w", encoding="utf-8") as fh:
        json.dump(keep, fh, separators=(",", ":"))

def size(d):
    return sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))

print(f"{len(files)} series in {a.src}, "
      f"{len(os.listdir(a.dst))} written to {a.dst}")
print(f"kept {kept_rows:,} rows since {cutoff}, dropped {dropped:,} older ones")
print(f"{a.src}: {size(a.src)/1e6:.0f} MB  ->  {a.dst}: {size(a.dst)/1e6:.1f} MB")
print(f"\nCommit {a.dst}/. Keep {a.src}/ out of git — you need it locally to")
print("rerun repair_climatology.py, but CI never does.")
