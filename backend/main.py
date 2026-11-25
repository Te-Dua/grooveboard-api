"""
BoardGen API - FastAPI service wrapping the generator engine.

Endpoints:
  POST /generate   spec JSON -> zip of all parts (STL + wiring + pinmap)
  POST /preview    spec JSON -> single base STL (for the 3D viewer)
  GET  /components  list available component footprints
  GET  /examples    list bundled example specs
  GET  /health      liveness
"""
import os
import io
import sys
import json
import glob
import shutil
import tempfile
import time
import zipfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE_DIR = os.path.join(ROOT, 'engine')
COMPONENTS_DIR = os.path.join(ENGINE_DIR, 'components')
EXAMPLES_DIR = os.path.join(ROOT, 'examples')

sys.path.insert(0, HERE)
sys.path.insert(0, ENGINE_DIR)       # so `import roadmap` works for /layout
from generate import generate  # noqa: E402
import logging_db  # noqa: E402

app = FastAPI(title='Grooveboard API', version='0.1.0',
              description='Spec -> printable solderless circuit board.')

# allow the web frontend (separate repo/host) to call this
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=['*'],
    allow_headers=['*'],
)

logging_db.init_db()


@app.middleware('http')
async def access_log_middleware(request: Request, call_next):
    # first-party request logging (no third-party analytics service needed).
    # Deliberately lightweight: one sqlite insert per request, skipped for
    # /stats itself so dashboard polling doesn't pollute its own counts.
    start = time.time()
    response = await call_next(request)
    if request.url.path != '/stats':
        try:
            logging_db.log_request(
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=(time.time() - start) * 1000,
                ip=(request.client.host if request.client else None),
                user_agent=request.headers.get('user-agent'),
                referer=request.headers.get('referer'),
            )
        except Exception:
            pass   # logging must never break a real request
    return response


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/stats')
def stats(key: str = '', days: float = 7):
    """Simple first-party dashboard data. Gated by STATS_KEY env var (not
    linked anywhere public) rather than full auth, since this is read-only
    aggregate counts, not sensitive per-user data."""
    required = os.environ.get('STATS_KEY')
    if required and key != required:
        raise HTTPException(403, 'bad or missing key')
    since = time.time() - days * 86400
    return logging_db.stats_summary(since)


def _editor_component(d):
    """Normalize a library component into an editor-friendly shape:
    footprint + rows[{side, pins:[names]}] + default row_gap. Handles both the
    'bottom_row/top_row' and 'all' pin layouts, and 'control/power/outputs'."""
    pins = d.get('pins', {})
    rows = []
    # Orientation matches the physical TOP view (silkscreen up). The JSON names
    # the two long sides left_row / right_row = the LEFT and RIGHT columns as you
    # read the board from the top. (Legacy bottom_row/top_row are treated as
    # left/right respectively for older component files.)
    left_pins = pins.get('left_row') or pins.get('bottom_row')
    right_pins = pins.get('right_row') or pins.get('top_row')
    if left_pins or right_pins:
        if left_pins:
            rows.append({'side': 'left', 'pins': left_pins})
        if right_pins:
            rows.append({'side': 'right', 'pins': right_pins})
    elif 'control' in pins or 'power' in pins or 'outputs' in pins:
        left = pins.get('control', [])
        right = pins.get('power', []) + pins.get('outputs', [])
        if left:
            rows.append({'side': 'left', 'pins': left})
        if right:
            rows.append({'side': 'right', 'pins': right})
    elif 'all' in pins:
        rows.append({'side': 'left', 'pins': pins['all']})
    fp = d.get('footprint', {})
    # default row gap = footprint width if 2 rows, else a sensible value
    row_gap = fp.get('width', 14) if len(rows) == 2 else 10
    return {
        'id': d.get('id'),
        'name': d.get('name'),
        'footprint': fp,
        'rows': rows,
        'row_gap': row_gap,
        'notes': d.get('notes', ''),
    }


@app.get('/components')
def components():
    """List component footprints (editor-friendly: footprint + rows + row_gap)."""
    out = []
    for p in sorted(glob.glob(os.path.join(COMPONENTS_DIR, '*.json'))):
        try:
            out.append(_editor_component(json.load(open(p))))
        except Exception:
            pass
    return {'components': out}


@app.get('/examples')
def examples():
    out = []
    for p in sorted(glob.glob(os.path.join(EXAMPLES_DIR, '*.json'))):
        out.append({'name': os.path.basename(p)[:-5]})
    return {'examples': out}


@app.get('/examples/{name}')
def example(name: str):
    p = os.path.join(EXAMPLES_DIR, f'{name}.json')
    if not os.path.exists(p):
        raise HTTPException(404, 'example not found')
    return json.load(open(p))


def _route_connection(conn, comps, towns, highway):
    """Trace a connection's physical path through the road graph:
    pinA -> townA -> highway -> townB -> pinB. Returns a list of [x,y] points.
    Deterministic (no pathfinding) - just follows the known roads."""
    hy = highway[0][0][1] if highway else 0        # highway = (a, b, w)

    def pin_xy(ref):
        cid, pn = ref.split('.', 1)
        for c in comps:
            if c.id == cid and pn in c.pins:
                return c.pins[pn], c
        return None, None

    def comp_center_x(comp):
        xs = [p[0] for p in comp.pins.values()]
        return sum(xs) / len(xs)

    (ax, ay), ca = pin_xy(conn['from'])
    (bx, by), cb = pin_xy(conn['to'])
    if ca is None or cb is None:
        return None

    # the town a pin routes to = the one its STREET heads toward. A pin's street
    # runs outward from the component center, so pick the town on that side.
    def town_x_for_pin(comp, px):
        cx = comp_center_x(comp)
        cand = [a[0] for (a, b, w) in towns if abs(a[0] - b[0]) < 0.01]
        if not cand:
            return px
        side = 1 if px >= cx else -1              # which side of center the pin is
        same_side = [tx for tx in cand if (tx - cx) * side > 0]
        pool = same_side or cand
        return min(pool, key=lambda tx: abs(tx - px))

    tax = town_x_for_pin(ca, ax)
    tbx = town_x_for_pin(cb, bx)
    # path: pinA -> townA(@ay) -> highway -> townB(@by) -> pinB
    return [[ax, ay], [tax, ay], [tax, hy], [tbx, hy], [tbx, by], [bx, by]]


@app.post('/layout')
def layout(spec: dict):
    """FAST 2D layout (no STL render): road polylines + pins + junction holes +
    ROUTED connection paths (each wire traced along the roads). mm, centered."""
    import importlib
    engine = importlib.import_module('roadmap')
    try:
        engine.autosize_board(spec)
        comps, streets, towns, highway, connectors, extra = engine.build_roads(spec)
        b = spec['board']
        fit_err = engine.check_fit(spec)       # None if content fits the board
        cx, cy = engine.board_center(spec)
        routes = []
        for conn in spec.get('connections', []):
            pts = _route_connection(conn, comps, towns, highway)
            if pts:
                routes.append({'from': conn['from'], 'to': conn['to'], 'points': pts})
        return {
            'board': {'length': b['length'], 'width': b['width'],
                      'cx': cx, 'cy': cy},
            'fits': fit_err is None,
            'fit_error': fit_err,
            'streets': [{'a': list(a), 'b': list(bb), 'w': w} for (a, bb, w) in streets],
            'towns': [{'a': list(a), 'b': list(bb), 'w': w} for (a, bb, w) in towns],
            'highway': [{'a': list(a), 'b': list(bb), 'w': w} for (a, bb, w) in highway],
            'connectors': [{'a': list(a), 'b': list(bb), 'w': w} for (a, bb, w) in connectors],
            'pins': [{'id': c.id, 'key': nm, 'name': c.pin_labels.get(nm, nm),
                      'x': p[0], 'y': p[1]}
                     for c in comps for nm, p in c.pins.items()],
            'holes': [{'x': x, 'y': y} for (x, y) in extra],
            'routes': routes,
            'extra_nubs': b.get('extra_nubs') or [],
            'widths': {'street': engine.STREET_W, 'town': engine.TOWN_W,
                       'highway': engine.HIGHWAY_W},
        }
    except Exception as e:
        raise HTTPException(400, f'layout failed: {e}')


def _log_board_event(event, spec, request: Request):
    try:
        logging_db.log_board_event(
            event=event,
            board_name=spec.get('name'),
            n_components=len(spec.get('components') or []),
            n_connections=len(spec.get('connections') or []),
            ip=(request.client.host if request.client else None),
        )
    except Exception:
        pass   # analytics must never break a real request


@app.post('/generate')
def generate_board(spec: dict, request: Request):
    """Generate all parts and return them as a zip."""
    job = tempfile.mkdtemp(prefix='boardgen_')
    try:
        meta = generate(spec, job)
        # zip everything in the job dir
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for f in glob.glob(os.path.join(job, '*')):
                if f.endswith(('.stl', '.md', '.h')):
                    z.write(f, os.path.basename(f))
            z.writestr('metadata.json', json.dumps(meta, indent=2))
        buf.seek(0)
        _log_board_event('generate', spec, request)
        return StreamingResponse(
            buf, media_type='application/zip',
            headers={'Content-Disposition':
                     f'attachment; filename="{meta["name"]}.zip"'})
    except Exception as e:
        raise HTTPException(400, f'generation failed: {e}')
    finally:
        # keep job dir only if STREAMING already read it; zip is in memory so safe to clean
        shutil.rmtree(job, ignore_errors=True)


@app.post('/preview')
def preview(spec: dict):
    """Generate just the base STL for the 3D viewer (legacy single-part)."""
    job = tempfile.mkdtemp(prefix='boardgen_')
    try:
        meta = generate(spec, job)
        base = os.path.join(job, meta['parts']['base']['stl'])
        if not os.path.exists(base):
            raise HTTPException(500, 'base STL not produced')
        # read into memory so we can clean the temp dir
        data = open(base, 'rb').read()
        return StreamingResponse(io.BytesIO(data), media_type='model/stl',
                                 headers={'Content-Disposition':
                                          f'inline; filename="{meta["name"]}_base.stl"',
                                          'X-Board-Meta': json.dumps(meta['board_mm'])})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f'preview failed: {e}')
    finally:
        shutil.rmtree(job, ignore_errors=True)


@app.post('/preview_parts')
def preview_parts(spec: dict, request: Request):
    """Generate EVERY enabled part (base + clamps + lid, per the board toggles)
    and return them as base64 STLs so the 3D viewer can show/hide each one.
    Each part also carries a hint color for the viewer."""
    import base64
    job = tempfile.mkdtemp(prefix='boardgen_')
    try:
        meta = generate(spec, job)
        colors = {'base': '#c9a227', 'clamps': '#3a7bd5', 'cap': '#2a9d5a'}
        labels = {'base': 'Base', 'clamps': 'Clamps', 'cap': 'Lid'}
        out = []
        for part, info in meta['parts'].items():
            p = os.path.join(job, info['stl'])
            if not os.path.exists(p):
                continue
            out.append({
                'part': part,
                'label': labels.get(part, part),
                'color': colors.get(part, '#999999'),
                'stl_b64': base64.b64encode(open(p, 'rb').read()).decode('ascii'),
            })
        if not out:
            raise HTTPException(500, 'no parts produced')
        _log_board_event('preview_parts', spec, request)
        return {'name': meta['name'], 'board_mm': meta['board_mm'], 'parts': out}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f'preview failed: {e}')
    finally:
        shutil.rmtree(job, ignore_errors=True)
