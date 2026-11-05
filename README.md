# Asteroid Tracker

A 3D view of the solar system that tracks all near-Earth asteroids (about 43,000), with
potentially hazardous ones in red. Zoom from the whole solar system down to Earth, search
any near-Earth asteroid, see its orbit, its live distance from Earth, and watch its close
approaches at adjustable speed.

## Files

| File | What it does |
|---|---|
| `index.html` | The 3D page (three.js): scene, search, time bar, keyboard controls |
| `main.py` | FastAPI backend: serves the page and the asteroid data |
| `ingest.py` | Downloads near-Earth asteroids from JPL into PostgreSQL |
| `schema.sql` | Creates the database tables |
| `asteroid_tracker.sql` | Creates the `asteroid_tracker` database |
| `images/` | Earth and Moon textures |
| `sbdb_query2.json` | A saved JPL download, loadable with `--from-file` |

## First-time setup

These steps are for Linux. On Ubuntu, you may first need
`sudo apt install postgresql python3-venv`, and to give the `postgres` database user a password.

1. Create the database and tables:
   ```
   psql -h 127.0.0.1 -U postgres -f asteroid_tracker.sql
   psql -h 127.0.0.1 -U postgres -d asteroid_tracker -f schema.sql
   ```
2. Create a virtual environment and install the Python packages:
   ```
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```
3. Set the database password for this terminal, then load the asteroids from JPL (takes a few minutes):
   ```
   export DB_PASSWORD=your-password
   .venv/bin/python ingest.py
   ```

Database settings come from environment variables, with these defaults:
`DB_HOST=127.0.0.1`, `DB_PORT=5432`, `DB_NAME=asteroid_tracker`, `DB_USER=postgres`,
`DB_PASSWORD` (empty).

## Running the site

From this folder, in a terminal with `DB_PASSWORD` set:
```
.venv/bin/python -m uvicorn main:app --port 8000 --reload
```
Then open **http://127.0.0.1:8000**. Keep that terminal open while you use the site.

## Controls

| Input | Action |
|---|---|
| Click a name / a dot | Select it and turn to face it |
| **Hazardous only**, **Orbit**, **Size** | Filter which dots show |
| Mouse wheel, pinch, `+` / `−` | Zoom toward the selected object (`Shift` = faster) |
| `G` or **Fly there** | Fly to the selected object |
| `L` / `J` | Time faster / slower (past paused = reverse) |
| `K` | Stop time |
| **Inspect** (side panel) | See a generated 3D shape (an illustration, not the real one), spin and size comparison; `Esc` closes |
| **Upcoming flybys** | List every asteroid passing within 0.05 AU of Earth soon |
| **Watch** (either close-approach list) | Jump to a close approach and watch it pass Earth |

## Data sources

- Asteroid list and orbits: JPL Small-Body Database (`ingest.py`)
- Close approaches: JPL CNEOS close-approach API (`/close-approaches`, `/upcoming-flybys`)
- Precise paths during close approaches: JPL Horizons (`/approach-path`)

## Keeping data fresh

Run the loader again whenever you want the latest asteroids and orbits. It's safe to re-run:
```
.venv/bin/python ingest.py
```
