"""
Programmatic wrapper around the engine (engine/roadmap.py).

generate(spec, out_dir) -> dict of produced file paths + metadata.
Reuses the engine's scad_base/scad_cap/scad_clamps + render_repair, but writes
into a caller-chosen directory (so each API request is isolated) instead of the
engine's own folder.
"""
import os
import sys
import json

# make the engine importable
ENGINE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'engine')
sys.path.insert(0, ENGINE_DIR)

import roadmap  # noqa: E402


def _render_repair_in(out_dir, name, part):
    """Like roadmap.render_repair but renders a .scad living in out_dir."""
    import subprocess
    scad = os.path.join(out_dir, f'{name}_{part}.scad')
    stl = os.path.join(out_dir, f'{name}_{part}.stl')
    openscad = os.environ.get('OPENSCAD_BIN',
                              '/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD')
    if not os.path.exists(openscad):
        # try PATH
        openscad = 'openscad'
    r = subprocess.run([openscad, '--backend=Manifold', '-o', stl, scad],
                       capture_output=True, timeout=300)
    nonmanifold = None
    try:
        import trimesh
        import numpy as np
        m = trimesh.load(stl)
        m.merge_vertices()
        _, c = np.unique(m.edges_sorted, axis=0, return_counts=True)
        nonmanifold = int((c != 2).sum())
        if nonmanifold:
            m.merge_vertices(digits_vertex=3)
            m.update_faces(m.unique_faces())
            m.update_faces(m.nondegenerate_faces())
            m.fill_holes()
            m.fix_normals()
            m.export(stl)
            _, c2 = np.unique(trimesh.load(stl).edges_sorted, axis=0, return_counts=True)
            nonmanifold = int((c2 != 2).sum())
    except Exception:
        pass
    return {'stl': stl, 'nonmanifold': nonmanifold, 'rc': r.returncode}


def generate(spec: dict, out_dir: str) -> dict:
    """Run the engine on `spec`, writing .scad/.stl into out_dir.
    Returns metadata + file paths."""
    os.makedirs(out_dir, exist_ok=True)
    name = spec.get('name', 'board')

    # 1. size the board (auto-hug, or honor the user's locked size/center)
    roadmap.autosize_board(spec)

    # 1b. locked boards may be too small -> refuse rather than print a board
    # with roads/pins hanging outside the walls.
    err = roadmap.check_fit(spec)
    if err:
        raise ValueError(err)

    # 2. write the enabled .scad parts. Base is always produced; clamps and the
    # cap/lid are toggleable from the board settings (make_clamps / make_lid).
    b = spec['board']
    parts = {'base': roadmap.scad_base(spec)}
    if b.get('make_clamps', True):
        parts['clamps'] = roadmap.scad_clamps(spec)
    if b.get('make_lid', True):
        parts['cap'] = roadmap.scad_cap(spec)
    for part, src in parts.items():
        with open(os.path.join(out_dir, f'{name}_{part}.scad'), 'w') as f:
            f.write(src)

    # 3. render + repair each to STL
    results = {}
    for part in parts:
        results[part] = _render_repair_in(out_dir, name, part)

    # 4. wiring guide + pin map (reuse engine helpers if present)
    aux = {}
    try:
        from roadmap import wiring_guide, pinmap_header  # may not exist in all versions
        aux['wiring'] = wiring_guide(spec)
        aux['pinmap'] = pinmap_header(spec)
    except Exception:
        pass
    for k, v in aux.items():
        ext = 'md' if k == 'wiring' else 'h'
        with open(os.path.join(out_dir, f'{name}_{k}.{ext}'), 'w') as f:
            f.write(v)

    return {
        'name': name,
        'board_mm': [b['length'], b['width'], b['base_h']],
        'parts': {p: {'stl': f'{name}_{p}.stl',
                      'nonmanifold': results[p]['nonmanifold']} for p in parts},
        'aux': list(aux.keys()),
        'out_dir': out_dir,
    }


if __name__ == '__main__':
    spec = json.load(open(sys.argv[1]))
    import tempfile
    out = tempfile.mkdtemp(prefix='boardgen_')
    print(json.dumps(generate(spec, out), indent=2))
