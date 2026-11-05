import argparse
import json
import os
import sys
import time

import psycopg2
import requests
from psycopg2.extras import execute_values


DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "asteroid_tracker"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

SBDB_URL = "https://ssd-api.jpl.nasa.gov/sbdb_query.api"
PAGE_SIZE = 5000


FIELDS = [
    "spkid", "full_name", "pdes", "neo", "pha", "moid", "H",
    "diameter", "albedo", "rot_per", "spec_B", "spec_T",
    "epoch", "a", "e", "i", "om", "w", "ma",
]

BASE_PARAMS = {
    "fields": ",".join(FIELDS),
    "sb-group": "neo",   
    "sb-kind": "a",      # asteroids only 
    "full-prec": "true",
}

def fetch_page(offset, limit, retries=4):
    """Fetch one page from SBDB, retrying on network errors and 5xx responses."""
    params = {**BASE_PARAMS, "limit": limit, "limit-from": offset}
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(SBDB_URL, params=params, timeout=60)
            if resp.status_code == 400:
                sys.exit(f"SBDB rejected the query (400): {resp.text[:300]}")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as err:
            if attempt == retries:
                raise
            wait = 2 ** attempt
            print(f"  page at {offset} failed ({err}); retrying in {wait}s")
            time.sleep(wait)


def fetch_all(max_rows=None):
    """Yield (fields, rows) pages until SBDB runs out of records."""
    offset = 0
    while True:
        limit = PAGE_SIZE if max_rows is None else min(PAGE_SIZE, max_rows - offset)
        if limit <= 0:
            return
        page = fetch_page(offset, limit)
        rows = page.get("data", [])
        total = page.get("count", "?")
        print(f"  fetched {offset + len(rows):>6} / {total}")
        if not rows:
            return
        yield page["fields"], rows
        if len(rows) < limit:
            return
        offset += len(rows)
        time.sleep(0.5)  # be polite to JPL's servers


def load_file(path):
    with open(path, encoding="utf-8") as f:
        page = json.load(f)
    print(f"  read {len(page['data'])} rows from {path}")
    yield page["fields"], page["data"]


def num(v):
    """SBDB sends numbers as strings, and missing values as null or ''."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def text(v):
    return v.strip() if isinstance(v, str) and v.strip() else None


def parse(fields, rows):
    """Split SBDB rows into asteroid tuples and orbital-element tuples."""
    asteroids, elements, skipped = [], [], 0
    for raw in rows:
        r = dict(zip(fields, raw))
        spk_id = int(r["spkid"])
        orbit = [num(r.get(k)) for k in ("epoch", "a", "e", "i", "om", "w", "ma")]

        asteroids.append((
            spk_id,
            text(r.get("full_name")) or str(spk_id),
            text(r.get("pdes")) or str(spk_id),
            r.get("neo") == "Y",
            r.get("pha") == "Y",
            num(r.get("moid")),
            num(r.get("H")),
            num(r.get("diameter")),
            num(r.get("albedo")),
            num(r.get("rot_per")),
            text(r.get("spec_B")) or text(r.get("spec_T")),  # prefer SMASSII, fall back to Tholen
        ))

        if any(v is None for v in orbit):
            skipped += 1  # keep the asteroid, but there's no usable orbit to store
            continue
        elements.append((spk_id, *orbit))
    return asteroids, elements, skipped


ASTEROID_SQL = """
    INSERT INTO asteroids
        (spk_id, full_name, pdes, neo, pha, moid, h_mag,
         diameter_km, albedo, rot_per, spec_type)
    VALUES %s
    ON CONFLICT (spk_id) DO UPDATE SET
        full_name   = EXCLUDED.full_name,
        pdes        = EXCLUDED.pdes,
        neo         = EXCLUDED.neo,
        pha         = EXCLUDED.pha,
        moid        = EXCLUDED.moid,
        h_mag       = EXCLUDED.h_mag,
        diameter_km = COALESCE(EXCLUDED.diameter_km, asteroids.diameter_km),
        albedo      = COALESCE(EXCLUDED.albedo,      asteroids.albedo),
        rot_per     = COALESCE(EXCLUDED.rot_per,     asteroids.rot_per),
        spec_type   = COALESCE(EXCLUDED.spec_type,   asteroids.spec_type),
        updated_at  = now()
"""

ELEMENTS_SQL = """
    INSERT INTO orbital_elements
        (asteroid_id, epoch_jd, a_au, e, i_deg, om_deg, w_deg, ma_deg)
    VALUES %s
    ON CONFLICT (asteroid_id, epoch_jd) DO UPDATE SET
        a_au   = EXCLUDED.a_au,
        e      = EXCLUDED.e,
        i_deg  = EXCLUDED.i_deg,
        om_deg = EXCLUDED.om_deg,
        w_deg  = EXCLUDED.w_deg,
        ma_deg = EXCLUDED.ma_deg
"""


def save(conn, asteroids, elements):
    with conn.cursor() as cur:
        execute_values(cur, ASTEROID_SQL, asteroids, page_size=1000)
        if elements:
            execute_values(cur, ELEMENTS_SQL, elements, page_size=1000)
    conn.commit()  # one commit per page, so a crash mid-run keeps finished pages


def main():
    ap = argparse.ArgumentParser(description="Load NEO asteroids from JPL SBDB into Postgres.")
    ap.add_argument("--from-file", help="load a saved SBDB JSON file instead of calling the API")
    ap.add_argument("--limit", type=int, help="fetch at most this many rows (for testing)")
    args = ap.parse_args()

    started = time.time()
    pages = load_file(args.from_file) if args.from_file else fetch_all(args.limit)

    print(f"Connecting to {DB_CONFIG['dbname']} on {DB_CONFIG['host']}:{DB_CONFIG['port']}")
    conn = psycopg2.connect(**DB_CONFIG)
    n_ast = n_orb = n_skip = 0
    try:
        for fields, rows in pages:
            asteroids, elements, skipped = parse(fields, rows)
            save(conn, asteroids, elements)
            n_ast += len(asteroids)
            n_orb += len(elements)
            n_skip += skipped
    finally:
        conn.close()

    print(f"Done in {time.time() - started:.0f}s: {n_ast} asteroids, "
          f"{n_orb} orbits saved, {n_skip} skipped (incomplete orbit).")


if __name__ == "__main__":
    main()