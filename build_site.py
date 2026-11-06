"""Builds the online version of the site: the page plus data files downloaded from JPL.

Online there is no server or database, so these files stand in for main.py and PostgreSQL.
Vercel runs this on every deployment (see vercel.json), and .github/workflows/refresh.yml
triggers one every day. Uses only the Python standard library.

    python build_site.py [output folder, default: site]
"""
import json
import shutil
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
SBDB_URL = "https://ssd-api.jpl.nasa.gov/sbdb_query.api"
CAD_URL = "https://ssd-api.jpl.nasa.gov/cad.api"
HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"
PAGE_SIZE = 5000
SHARDS = 256
FLYBY_DAYS = 60
# Famous passes that get a precise path even when they aren't coming up soon
EXTRA_PATHS = ["99942"]   # Apophis in 2029


def get_json(url, params, retries=4, timeout=300):
    full = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(full, timeout=timeout) as res:
                return json.load(res)
        except (OSError, ValueError) as err:
            if attempt == retries:
                raise
            wait = 5 * attempt
            print(f"  {url} failed ({err}); retrying in {wait}s", flush=True)
            time.sleep(wait)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")


def num(v, digits=10):
    return None if v in (None, "") else round(float(v), digits)


def fetch_catalogue():
    """Every near-Earth asteroid with a complete orbit, as compact rows."""
    wanted = ["spkid", "full_name", "pdes", "pha", "moid", "H", "diameter", "albedo", "rot_per",
              "spec_B", "spec_T", "epoch", "a", "e", "i", "om", "w", "ma"]
    rows, offset = [], 0
    while True:
        page = get_json(SBDB_URL, {"fields": ",".join(wanted), "sb-group": "neo", "sb-kind": "a",
                                   "full-prec": "true", "limit": PAGE_SIZE, "limit-from": offset})
        batch = page.get("data", [])
        for raw in batch:
            r = dict(zip(page["fields"], raw))
            orbit = [num(r[k]) for k in ("epoch", "a", "e", "i", "om", "w", "ma")]
            if any(v is None for v in orbit):
                continue
            rows.append([int(r["spkid"]), r["full_name"].strip(), r["pdes"], r["pha"] == "Y", num(r["moid"]),
                         num(r["H"]), num(r["diameter"]), num(r["albedo"]), num(r["rot_per"]),
                         (r["spec_B"] or r["spec_T"] or "").strip() or None, *orbit])
        print(f"  catalogue: {offset + len(batch)} / {page.get('count', '?')}", flush=True)
        if len(batch) < PAGE_SIZE:
            return rows
        offset += len(batch)
        time.sleep(0.5)


def shard_of(des):
    # The page works out the same number to find the right file
    x = 5381
    for c in des:
        x = (x * 33 + ord(c)) & 0xFFFFFFFF
    return x % SHARDS


def build_approaches(out):
    """All close approaches within 0.2 AU, split up so one click downloads only a small file."""
    body = get_json(CAD_URL, {"date-min": "1950-01-01", "date-max": "2150-01-01", "dist-max": "0.2"})
    f = {name: k for k, name in enumerate(body["fields"])}
    shards = defaultdict(lambda: defaultdict(list))
    for r in body["data"]:
        shards[shard_of(r[f["des"]])][r[f["des"]]].append(
            [float(r[f["jd"]]), r[f["cd"]], float(r[f["dist"]]), float(r[f["v_rel"]])])
    for k, shard in shards.items():
        write_json(out / "data" / "approaches" / f"{k}.json", shard)
    print(f"  close approaches: {body['count']}", flush=True)
    return shards


def build_flybys(out, rows):
    # Same list as main.py's /upcoming-flybys, for the longest period the page offers
    body = get_json(CAD_URL, {"date-min": "now", "date-max": f"+{FLYBY_DAYS}", "dist-max": "0.05",
                              "sort": "date", "fullname": "true"})
    f = {name: k for k, name in enumerate(body["fields"])}
    known = {r[2]: r for r in rows}
    flybys = []
    for r in body.get("data", []):
        des = r[f["des"]]
        flybys.append({"des": des, "name": r[f["fullname"]].strip(), "jd": float(r[f["jd"]]), "cd": r[f["cd"]],
                       "dist": float(r[f["dist"]]), "v_rel": float(r[f["v_rel"]]),
                       "spk_id": known[des][0] if des in known else None,
                       "pha": des in known and known[des][3]})
    write_json(out / "data" / "flybys.json", flybys)
    print(f"  upcoming flybys: {len(flybys)}", flush=True)
    return flybys


def fetch_path(des, jd):
    """JPL Horizons' precise path around one close approach, as in main.py's /approach-path."""
    command = f"'{des};'" if des.isdigit() else f"'DES={des};'"
    body = get_json(HORIZONS_URL, {
        "format": "json", "COMMAND": command, "EPHEM_TYPE": "VECTORS", "CENTER": "'500@399'",
        "REF_PLANE": "ECLIPTIC", "REF_SYSTEM": "ICRF", "OUT_UNITS": "AU-D", "VEC_TABLE": "1", "CSV_FORMAT": "YES",
        "START_TIME": f"'JD {jd - 1.1}'", "STOP_TIME": f"'JD {jd + 1.1}'", "STEP_SIZE": "'5 m'",
    }, retries=2, timeout=90)
    text = body.get("result", "")
    if "$$SOE" not in text:
        return None
    points = []
    for line in text.split("$$SOE")[1].split("$$EOE")[0].strip().splitlines():
        cols = [c.strip() for c in line.split(",")]
        points.append([float(cols[0]), float(cols[2]), float(cols[3]), float(cols[4])])
    return points


def build_paths(out, flybys, approaches):
    wanted = [(f["des"], f["jd"]) for f in flybys]
    now_jd = datetime.now(timezone.utc).timestamp() / 86400 + 2440587.5
    for des in EXTRA_PATHS:
        future = [a for a in approaches[shard_of(des)].get(des, []) if a[0] > now_jd]
        if future:
            wanted.append((des, min(future, key=lambda a: a[2])[0]))

    manifest = []
    for n, (des, jd) in enumerate(wanted):
        try:
            points = fetch_path(des, jd)
        except (OSError, ValueError, IndexError) as err:
            # Not fatal: without a path the page falls back to its orbit estimate
            print(f"  no path for {des}: {err}", flush=True)
            continue
        if points and len(points) > 1:
            name = f"{n}.json"
            write_json(out / "data" / "paths" / name, points)
            manifest.append({"des": des, "jd": jd, "file": name})
        time.sleep(0.5)
    print(f"  precise paths: {len(manifest)} of {len(wanted)}", flush=True)
    return manifest


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "site"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copy(HERE / "index.html", out / "index.html")
    shutil.copytree(HERE / "images", out / "images")
    shutil.copytree(HERE / "vendor", out / "vendor")
    (out / ".nojekyll").touch()   # tells GitHub to publish the files exactly as they are

    rows = fetch_catalogue()
    write_json(out / "data" / "asteroids.json", rows)
    approaches = build_approaches(out)
    flybys = build_flybys(out, rows)
    paths = build_paths(out, flybys, approaches)

    # Written last: the page takes this file's presence to mean "no server, read data/ instead"
    write_json(out / "data" / "meta.json", {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "shards": SHARDS,
        "paths": paths,
    })
    print(f"Done: {len(rows)} asteroids in {out}", flush=True)


if __name__ == "__main__":
    main()
