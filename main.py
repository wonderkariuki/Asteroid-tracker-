import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
import psycopg2
import requests
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from ingest import DB_CONFIG

HERE = Path(__file__).parent

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null", "http://127.0.0.1:5500", "http://localhost:5500"],
    allow_methods=["GET"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5) 
app.mount("/images", StaticFiles(directory=HERE / "images"), name="images")   # Earth/Moon textures
app.mount("/vendor", StaticFiles(directory=HERE / "vendor"), name="vendor")   # the three.js 3D library


POOL_SIZE = 10
pool = None   # created on first use, so the site still starts while PostgreSQL is down
pool_lock = threading.Lock()
pool_slots = threading.BoundedSemaphore(POOL_SIZE)   # extra requests wait instead of the pool raising an error


def get_pool():
    global pool
    with pool_lock:
        if pool is None:
            pool = ThreadedConnectionPool(1, POOL_SIZE, **DB_CONFIG)
        return pool


def query(sql, params=()):
    """Run one SELECT on a pooled connection and return the rows; always hands the connection back."""
    unavailable = HTTPException(503, "Database unavailable. Is PostgreSQL running?")
    with pool_slots:

        for can_retry in (True, False):
            try:
                db = get_pool()
                conn = db.getconn()
            except psycopg2.OperationalError:
                raise unavailable
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, params)
                    return cur.fetchall()
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                if not can_retry:
                    raise unavailable
            finally:
                db.putconn(conn)   # ends the transaction; a dead connection is discarded


def jpl_get(url, params, what):
    """Call a JPL API, turning network/server failures into a clear 502 for the page."""
    try:
        res = requests.get(url, params=params, timeout=30)
        res.raise_for_status()
        return res.json()
    except (requests.RequestException, ValueError):
        raise HTTPException(502, f"JPL {what} service unavailable. Try again shortly.")


@app.get("/")
def home():
    # Serve the 3D page itself, so the whole site runs from this one address.
    # no-cache = browsers must check for a newer version, instead of showing a saved old copy.
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/all-asteroids")
def get_all_asteroids():
    # Every asteroid and its latest orbital elements; "pha" marks the hazardous ones
    rows = query("""
        SELECT DISTINCT ON (a.spk_id)
            a.spk_id, a.full_name, a.pha, a.h_mag, a.diameter_km, a.albedo,
            e.epoch_jd, e.a_au, e.e, e.i_deg, e.om_deg, e.w_deg, e.ma_deg
        FROM asteroids a
        JOIN orbital_elements e ON a.spk_id = e.asteroid_id
        ORDER BY a.spk_id, e.epoch_jd DESC;
    """)

    # Build the JSON directly 
    return Response(json.dumps([{
        "spk_id": row['spk_id'],
        "name": row['full_name'].strip(),
        "pha": row['pha'],
        "h_mag": row['h_mag'],
        "diameter_km": row['diameter_km'],
        "albedo": row['albedo'],
        "epoch_jd": row['epoch_jd'],
        "a_au": row['a_au'],
        "e": row['e'],
        "i_deg": row['i_deg'],
        "om_deg": row['om_deg'],
        "w_deg": row['w_deg'],
        "ma_deg": row['ma_deg'],
    } for row in rows]), media_type="application/json")


# Any asteroid (hazardous or not), with its most recent orbital elements
ASTEROID_DETAILS = """
    SELECT a.spk_id, a.full_name, a.pdes, a.neo, a.pha, a.moid, a.h_mag,
           a.diameter_km, a.albedo, a.rot_per, a.spec_type,
           e.epoch_jd, e.a_au, e.e, e.i_deg, e.om_deg, e.w_deg, e.ma_deg
    FROM asteroids a
    JOIN LATERAL (
        SELECT * FROM orbital_elements o
        WHERE o.asteroid_id = a.spk_id
        ORDER BY o.epoch_jd DESC
        LIMIT 1
    ) e ON true
"""


@app.get("/asteroid/{spk_id}")
def get_asteroid(spk_id: int):
    rows = query(ASTEROID_DETAILS + "WHERE a.spk_id = %s;", (spk_id,))
    if not rows:
        raise HTTPException(404, "No asteroid with that ID.")
    rows[0]["full_name"] = rows[0]["full_name"].strip()
    return rows[0]


@app.get("/search")
def search_asteroids(q: str):
    rows = query(ASTEROID_DETAILS + """
        WHERE lower(a.full_name) LIKE lower(%s)
        ORDER BY a.full_name
        LIMIT 20;
    """, (f"%{q.strip()}%",))

    for row in rows:
        row["full_name"] = row["full_name"].strip()
    return rows


@app.get("/close-approaches")
def close_approaches(des: str):
    # JPL's close-approach API doesn't allow browser (CORS) calls, so proxy it here
    body = jpl_get("https://ssd-api.jpl.nasa.gov/cad.api", {
        "des": des,
        "date-min": "1950-01-01",
        "date-max": "2150-01-01",
        "dist-max": "0.2",   # AU
    }, "close-approach")
    fields = body.get("fields", [])
    return [dict(zip(fields, row)) for row in body.get("data", [])]


@app.get("/upcoming-flybys")
def upcoming_flybys(days: int = 60):
    # Same defaults as the CNEOS close-approach table: nearer than 0.05 AU, soonest first
    if not 1 <= days <= 365:
        raise HTTPException(400, "Days must be between 1 and 365.")
    body = jpl_get("https://ssd-api.jpl.nasa.gov/cad.api", {
        "date-min": "now",
        "date-max": f"+{days}",
        "dist-max": "0.05",   # AU
        "sort": "date",
        "fullname": "true",
    }, "close-approach")
    fields = body.get("fields", [])
    flybys = [dict(zip(fields, row)) for row in body.get("data", [])]
    if not flybys:
        return []

    # Link each one to our catalogue. A very new discovery may not be loaded yet, so it has no spk_id.
    known = {row["pdes"]: row for row in query(
        "SELECT pdes, spk_id, pha FROM asteroids WHERE pdes = ANY(%s);", ([f["des"] for f in flybys],))}
    return [{
        "des": f["des"],
        "name": f["fullname"].strip(),
        "jd": f["jd"],
        "cd": f["cd"],
        "dist": f["dist"],
        "v_rel": f["v_rel"],
        "spk_id": known[f["des"]]["spk_id"] if f["des"] in known else None,
        "pha": f["des"] in known and known[f["des"]]["pha"],
    } for f in flybys]


@app.get("/approach-path")
def approach_path(des: str, start_jd: float, stop_jd: float, step_min: int = 5):
    """JPL Horizons' precise path of an asteroid relative to Earth's centre (ecliptic J2000, AU).

    Includes Earth's gravity and planetary pulls, which the page's simple orbit math leaves out,
    so distances during a close approach match JPL's official numbers.
    """
    if not 0 < stop_jd - start_jd <= 10:
        raise HTTPException(400, "Window must be between 0 and 10 days.")
    
    command = f"'{des};'" if des.isdigit() else f"'DES={des};'"
    body = jpl_get("https://ssd.jpl.nasa.gov/api/horizons.api", {
        "format": "json",
        "COMMAND": command,
        "EPHEM_TYPE": "VECTORS",
        "CENTER": "'500@399'",         
        "REF_PLANE": "ECLIPTIC",
        "REF_SYSTEM": "ICRF",
        "OUT_UNITS": "AU-D",
        "VEC_TABLE": "1",              
        "CSV_FORMAT": "YES",
        "START_TIME": f"'JD {start_jd}'",
        "STOP_TIME": f"'JD {stop_jd}'",
        "STEP_SIZE": f"'{step_min} m'",
    }, "Horizons")

    text = body.get("result", "")
    if "$$SOE" not in text:
        raise HTTPException(404, "Horizons has no path for this object.")
    points = []
    for line in text.split("$$SOE")[1].split("$$EOE")[0].strip().splitlines():
        cols = [c.strip() for c in line.split(",")]
        
        points.append([float(cols[0]), float(cols[2]), float(cols[3]), float(cols[4])])
    return points
