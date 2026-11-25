# Grooveboard API

Backend for **Grooveboard** — *design & 3D-print your own PWB (Printed Wired
Board). Like a PCB, but solderless: lay plain wires in printed grooves and clamp
them in place.*

This repo = the generator **engine** + a **FastAPI** service. The web editor is a
separate repo (**grooveboard-web**).

## What it makes

From a board **spec** (components + connections) it generates print-ready parts:
- **base** — tray with routed grooves (streets → town roads → highway) + pin holes
- **clamp plugs** — press each wire into its groove (mechanical, no solder)
- **lid** — lift-off (*nest*) or sliding (*pencil-box*) style

Plus a wiring guide + firmware pin map. Reusable — rewire by moving wires.

## How it works

```
POST /generate { spec }  →  engine (Python + OpenSCAD)
  →  base.stl + clamps.stl + lid.stl + wiring.md + pinmap.h  (zip, validated)
```

Road-network model: grooves are infrastructure from the component layout;
connections are traffic that rides them.

## Run locally

```bash
pip install -r requirements.txt     # fastapi, uvicorn, trimesh, numpy
# OpenSCAD must be installed (STL rendering)
./run.sh                            # docs at http://localhost:8000/docs
```

## Endpoints

| Method | Path | Does |
|--------|------|------|
| POST | /generate | spec → zip of STLs + wiring + pinmap |
| POST | /preview | spec → base STL only (legacy single-part preview) |
| POST | /preview_parts | spec → every enabled part as base64 STLs (3D viewer) |
| POST | /layout | spec → fast 2D road/pin/route geometry, no STL render |
| GET  | /components | list component footprints |
| GET  | /examples | list example specs |
| GET  | /examples/{name} | fetch one example spec |
| GET  | /health | liveness check |
| GET  | /stats | first-party request/usage stats (see note below) |

### A note on `/stats`

`/stats` returns aggregate request counts, unique visitor IPs, and recent
board-generation metadata from a local SQLite log — no third-party analytics
service involved. If you deploy this yourself, set the `STATS_KEY` env var;
without it, `/stats` is **unauthenticated and publicly readable**. Pass the
key as `?key=...` to query it.

## License
MIT.
