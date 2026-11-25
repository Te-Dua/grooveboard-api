#!/usr/bin/env python3
"""
BoardGen road-network generator.

Roads are INFRASTRUCTURE, built from component geometry regardless of wiring
(roads ready even with no traffic). A connection is just traffic that rides:

   PIN -green street-> BLUE town road -> ORANGE highway -> BLUE town road
        -green street-> PIN

Roads are wide/deep channels that hold a whole bundle of loose wires, so no
per-wire lane math is needed. Changing connections = re-route traffic, the
board (roads) stays the same.

JSON spec:
{
  "name": "...",
  "board": { "length", "width", "base_h", "cap_h", "wall" },
  "components": [
    { "id":"esp", "x":-37, "y":0, "rows":[
        {"side":"left","pins":[...names...]},
        {"side":"right","pins":[...names...]} ] },
    ...
  ],
  "connections": [ {"from":"esp.D4","to":"tb.PWMA"} ]
}

Output: <name>_base.scad + <name>_cap.scad  (+ auto-repaired STL if OpenSCAD present)
        <name>_roads.png  (schematic: streets/town/highway + traffic)
"""
import json
import math
import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

PITCH = 2.54
STREET_W = 1.8          # green street width (fits 2.54 pitch, lanes stay separate)
STREET_LEN = 8.0        # street length (pin -> town road). Longer = town road sits
                        # further from pins, giving room for the bare/stripped
                        # wire end to lie in the street, clear of the pin cluster.
TOWN_W = 5.0            # blue town road width (holds a bundle)
HIGHWAY_W = 8.0         # orange highway width (holds many bundles)
# grooves deepened so the WIRE sits in the bottom half and a road-shaped CLAMP
# PLUG fills the top half (presses wire down + friction-locks). See WIRE_DIA.
WIRE_DIA = 1.3
CLAMP_PLUG_H = 1.2      # how deep the clamp plug sits into the groove top
CLAMP_PLUG_FIT = 0.3    # clearance so the plug slides in snug (not jammed)
STREET_DEPTH = WIRE_DIA + CLAMP_PLUG_H      # 2.5
TOWN_DEPTH = WIRE_DIA + CLAMP_PLUG_H + 0.4  # 2.9 (bundle a bit more room)
HIGHWAY_DEPTH = 3.2


# ---------- component model ----------
class Comp:
    """A placed component: computes pin world-positions + its town road line.
    row_gap = real distance between the component's two pin rows (mm), measured
    from your actual chip. Each component carries its own."""
    def __init__(self, c, board_defaults=None):
        self.id = c['id']
        self.x, self.y = c['x'], c['y']
        # half the row-to-row distance; default if a single-row component
        self.offset = c.get('row_gap', 12.0) / 2.0
        self.pins = {}          # unique_key -> (x,y)
        self.pin_labels = {}    # unique_key -> original display name (e.g. "GND")
        self.rows = []          # (orient, side, [unique_keys], town_a, town_b)
        # per-component WIDTH / gap overrides. Precedence: per-component override
        # > board global default > module constant. board_defaults carries the
        # resolved board globals (or the constants when absent).
        bd = board_defaults or {
            'street_w': STREET_W, 'town_w': TOWN_W,
            'street_len': STREET_LEN,
        }
        self.street_w = c.get('street_w', bd['street_w'])
        self.town_w = c.get('town_w', bd['town_w'])
        # junction_gap removed: holes are inline at fixed JUNCTION_HOLE_SPACING
        # street length = distance from the pin row out to the town road. Longer
        # pushes the town road farther from the component.
        self.street_len = c.get('street_len', bd.get('street_len', STREET_LEN))
        # junctions: pin name -> N extra holes (for external in/out wires).
        # accepts {"junctions": {"AO1": 2, "VM": 2}} on the component.
        self.junctions = c.get('junctions', {})
        self._layout(c['rows'])

    def _unique_key(self, nm):
        """Return a unique dict key for pin display-name `nm`. First occurrence
        keeps the bare name; repeats get suffixed `_2`, `_3`, ... (real parts
        have multiple pins called GND/VCC etc., which a plain dict would collapse).
        Records the bare display name in self.pin_labels."""
        if nm not in self.pins:
            key = nm
        else:
            k = 2
            while f'{nm}_{k}' in self.pins:
                k += 1
            key = f'{nm}_{k}'
        self.pin_labels[key] = nm
        return key

    def _layout(self, rows):
        off = self.offset
        for r in rows:
            side = r['side']
            names = r['pins']
            n = len(names)
            span = (n - 1) * PITCH
            if side in ('left', 'right'):
                px = self.x - (off if side == 'left' else -off)
                # list order reads TOP -> bottom (index 0 = topmost pin), to
                # match how a component's silkscreen is read from the top view.
                y_top = self.y + span / 2
                keys = []
                for i, nm in enumerate(names):
                    key = self._unique_key(nm)
                    self.pins[key] = (px, y_top - i * PITCH)
                    keys.append(key)
                # town must sit far enough to fit the LONGEST pin's inline holes +
                # clamp clearance -> move the town OUT when junctions are added.
                reach = self._row_reach(keys)
                town_x = px - (reach if side == 'left' else -reach)
                self.rows.append(('v', side, keys,
                                  (town_x, self.y - span / 2 - 3),
                                  (town_x, self.y + span / 2 + 3)))
            else:  # top/bottom
                py = self.y - (off if side == 'bottom' else -off)
                x0 = self.x - span / 2
                keys = []
                for i, nm in enumerate(names):
                    key = self._unique_key(nm)
                    self.pins[key] = (x0 + i * PITCH, py)
                    keys.append(key)
                reach = self._row_reach(keys)
                town_y = py - (reach if side == 'bottom' else -reach)
                self.rows.append(('h', side, keys,
                                  (self.x - span / 2 - 3, town_y),
                                  (self.x + span / 2 + 3, town_y)))

    def _row_reach(self, keys):
        """Pin->town distance for a row: the base street length, or more if any
        pin's inline holes + clamp clearance need it. Junctions are keyed by the
        deduped pin key (matching what the editor sends)."""
        max_jn = max((self.junctions.get(k, 0) for k in keys), default=0)
        need = (max_jn * JUNCTION_HOLE_SPACING + CLAMP_CLEARANCE) if max_jn else 0
        return max(self.street_len, need)


# ---------- geometry helpers ----------
NUB_D = 1.4             # serpentine nub diameter (bigger = real grip on wire)
NUB_SPACING = 3.0       # distance between nubs along a road
NUB_OFFSET = 0.55       # how far nub intrudes toward wire centerline (more pinch)


def seg(s, x1, y1, x2, y2, w, z, h):
    length = math.hypot(x2 - x1, y2 - y1)
    if length < 0.001:
        s.append(f'  translate([{x1:.2f},{y1:.2f},{z:.2f}]) cylinder(h={h:.2f},d={w});')
        return
    ang = math.degrees(math.atan2(y2 - y1, x2 - x1))
    s.append(f'  translate([{x1:.2f},{y1:.2f},{z:.2f}]) rotate([0,0,{ang:.3f}]) '
             f'translate([0,{-w/2:.2f},0]) cube([{length:.3f},{w},{h:.2f}]);')
    # rounded caps at BOTH ends: cylinder-into-crossing-box booleans cleanly
    # (avoids coplanar box-edge T-junctions -> no non-manifold edges)
    s.append(f'  translate([{x1:.2f},{y1:.2f},{z:.2f}]) cylinder(h={h:.2f},d={w});')
    s.append(f'  translate([{x2:.2f},{y2:.2f},{z:.2f}]) cylinder(h={h:.2f},d={w});')


def street_bump(x1, y1, x2, y2, z, h):
    """A single small centered floor bump near the middle of a narrow street.
    The wire clips over/past it (light speed-bump hold), doesn't block it."""
    length = math.hypot(x2 - x1, y2 - y1)
    if length < 3:
        return []
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    # place the bump in the clamp zone, clear of the exposed pin end AND clear
    # of the town-road crossing: a fixed 5mm from the pin (CLAMP_SKIP region).
    t = min(5.0, length * 0.45)
    cx, cy = x1 + ux * t, y1 + uy * t
    bump_h = min(h * 0.5, 0.8)            # low: clips the wire, doesn't block
    # start 0.3mm below groove floor so it fuses into the solid floor (overlap)
    return [f'  translate([{cx:.2f},{cy:.2f},{z - 0.3:.2f}]) '
            f'cylinder(h={bump_h + 0.3:.2f},d=0.9);']


HWAY_NUB_D = 2.6        # highway center-nub diameter (thick = won't break)
HWAY_NUB_SPACING = 12.0 # spacing between highway center nubs (few, spread out)
HWAY_END_CLEAR = 10.0   # keep nubs this far from the highway ends (wire entry)


def highway_center_nubs(x1, y1, x2, y2, z, h):
    """Thick nubs down the CENTER of the highway (not on the walls), spaced out
    and kept clear of both ends so wire can be inserted. The bundle weaves
    around them; being central + fat they don't break like thin edge nubs."""
    out = []
    length = math.hypot(x2 - x1, y2 - y1)
    if length < 2 * HWAY_END_CLEAR + HWAY_NUB_SPACING:
        return out
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    t = HWAY_END_CLEAR
    while t <= length - HWAY_END_CLEAR:
        cx, cy = x1 + ux * t, y1 + uy * t
        out.append(f'  translate([{cx:.2f},{cy:.2f},{z - 0.3:.2f}]) '
                   f'cylinder(h={h + 0.3:.2f},d={HWAY_NUB_D});  // highway center nub')
        t += HWAY_NUB_SPACING
    return out


def serpentine_nub_points(x1, y1, x2, y2, w):
    """(x, y) for alternating nubs along a road (world coords), so a wire laid
    in it must weave and friction-locks. Pure geometry — no SCAD here, so
    callers can both place the nub AND notch a clamp plug around it from the
    same source of truth."""
    out = []
    length = math.hypot(x2 - x1, y2 - y1)
    if length < NUB_SPACING:
        return out
    ux, uy = (x2 - x1) / length, (y2 - y1) / length     # unit along road
    pxn, pyn = -uy, ux                                   # perpendicular
    inset = w / 2 - NUB_D / 2 - NUB_OFFSET
    if inset < 0:
        inset = 0
    # keep nubs AWAY from the road ends/corners (so a wire turning at a junction
    # or corner has clear room to fit). margin = half the road width + a bit.
    margin = w / 2 + 2.0
    t = margin
    i = 0
    while t <= length - margin:
        cx, cy = x1 + ux * t, y1 + uy * t
        side = 1 if i % 2 == 0 else -1                   # alternate walls
        nx, ny = cx + pxn * inset * side, cy + pyn * inset * side
        out.append((nx, ny))
        t += NUB_SPACING
        i += 1
    return out


def serpentine_nubs(x1, y1, x2, y2, w, z, h):
    """OpenSCAD cylinder strings for serpentine_nub_points at depth z..z+h.
    Nubs are ADDED (union), so they belong outside the difference() cut block."""
    return [f'  translate([{nx:.2f},{ny:.2f},{z:.2f}]) cylinder(h={h:.2f},d={NUB_D});'
            for (nx, ny) in serpentine_nub_points(x1, y1, x2, y2, w)]


def clampable_nub_points(spec, streets, towns):
    """(x, y, d) for every nub that sits in the TOWN/STREET groove z-range —
    i.e. everywhere a clamp plug might also drop in. Excludes highway center
    nubs (highways never get a clamp plug, so there's nothing to notch).
    Used to punch clearance holes through the clamp plug so it can still seat
    around a nub instead of colliding with it. Takes the already-computed
    streets/towns (from build_roads) so callers don't recompute the road graph."""
    b = spec['board']
    pts = []
    if b.get('town_nubs'):
        for (a, bb, w) in towns:
            pts += [(x, y, NUB_D) for (x, y) in serpentine_nub_points(a[0], a[1], bb[0], bb[1], w)]
    if b.get('street_nubs'):
        for (a, bb, w) in streets:
            pts += [(x, y, NUB_D) for (x, y) in serpentine_nub_points(a[0], a[1], bb[0], bb[1], w)]
    for n in (b.get('extra_nubs') or []):
        pts.append((n['x'], n['y'], n.get('d', 1.8)))
    return pts


JUNCTION_HOLE_SPACING = PITCH  # 2.54mm between holes (standard pin pitch)
CLAMP_CLEARANCE = 6.0          # gap after last hole -> town: room for the clamp
                               # plug to sit + exposed-wire breathing space
# ---- per-row clamp bar (pressed on before the cap, verify-then-cap) ----
# Clamp starts just 1.2mm from the pin hole: close enough to hold the wire
# RIGHT at the pin (for contact) without covering the pin hole itself.
CLAMP_SKIP = 1.2
# RECTANGULAR pegs (thin, won't snap, placed on SOLID floor with a socket)
PEG_W = 2.5                # peg length (along the row)
PEG_T = 1.4                # peg thickness (thin, flexes instead of breaking)
PEG_SOCKET_FIT = 0.25      # socket clearance (snug lock)
PEG_LAND = 5.0             # how far PAST the pin row the peg sits (on solid floor)


def clamp_rows(spec):
    """One clamp bar per pin row, but only covering USED pins (those with a
    street). Pusher ribs are placed only where a street exists, so the clamp
    sits flat (no rib pressing on solid floor). A row with no used pins gets
    no clamp bar."""
    comps = [Comp(c, board_defaults(spec)) for c in spec['components']]
    bars = []
    for comp in comps:
        for (orient, side, names, a, b) in comp.rows:
            # clamp covers exactly the pins that have streets
            u_names = streeted_names(spec, comp.id, names)
            if not u_names:
                continue                                 # no streets -> no clamp
            pins = [comp.pins[nm] for nm in u_names]
            max_jn = max((comp.junctions.get(nm, 0) for nm in u_names), default=0)
            # Clamp now SKIPS the inline holes (near the pin) and covers the rest
            # of the street (clamp clearance zone + approach to town).
            # start = pin + CLAMP_SKIP, end = pin + holes + clearance + a bit.
            # Actually simpler: clamp covers from (pin + skip) to town, and it
            # just sits OVER the holes (holes are drilled through the clamp too).
            jn_ext = 0  # no extension past town anymore (holes are inline)
            if orient == 'v':
                px = pins[0][0]
                tx = a[0]
                direction = 1 if tx > px else -1
                start_x = px + direction * CLAMP_SKIP        # clear pin zone
                # clamp ends at the town (no extension)
                end_x = tx
                ys = [p[1] for p in pins]
                cxm = (start_x + end_x) / 2
                # per-pin rib: each rib spans from (pin+skip) to town (holes inline)
                ribs = []
                for nm in u_names:
                    ribs.append((comp.pins[nm][1], start_x, tx))
                bars.append({
                    'orient': 'v', 'direction': direction,
                    'x0': start_x, 'x1': end_x,
                    'ymin': min(ys), 'ymax': max(ys),
                    'ribs': ribs,
                    # pegs sit PAST the row ends in Y (solid floor, no streets there)
                    'pegs': [(cxm, min(ys) - PEG_LAND), (cxm, max(ys) + PEG_LAND)],
                })
            else:
                py = pins[0][1]
                ty = a[1]
                direction = 1 if ty > py else -1
                start_y = py + direction * CLAMP_SKIP
                # clamp ends at town (no extension, holes inline)
                end_y = ty
                xs = [p[0] for p in pins]
                cym = (start_y + end_y) / 2
                ribs = []
                for nm in u_names:
                    ribs.append((comp.pins[nm][0], start_y, ty))
                bars.append({
                    'orient': 'h', 'direction': direction,
                    'y0': start_y, 'y1': end_y,
                    'xmin': min(xs), 'xmax': max(xs),
                    'ribs': ribs,
                    'pegs': [(min(xs) - PEG_LAND, cym), (max(xs) + PEG_LAND, cym)],
                })
    return bars


BOARD_MARGIN = 3.0      # default gap between outermost road/feature and the wall
                        # (was 6; tighter = smaller board. Override: board.margin)


def board_center(spec):
    """The board's center in world coords. Defaults to the origin (the legacy
    centered-on-origin behavior). When the user drags the board around, the
    editor sets board['center'] = {'x':..,'y':..} and the box/walls/rails/doors
    all offset by this; content (components/roads) stays at its own coords."""
    c = spec['board'].get('center') or {}
    return float(c.get('x', 0.0)), float(c.get('y', 0.0))


def feature_extents(spec):
    """World-coord bounding box of everything that must sit inside the board:
    roads, pins, junction holes, highway Y bands. Returns (x0,x1,y0,y1) or None
    if there's nothing placed."""
    comps, streets, towns, highway, connectors, extra = build_roads(spec)
    xs, ys = [], []
    for (a, bb, w) in streets:
        xs += [a[0], bb[0]]; ys += [a[1], bb[1]]
    for (a, bb, w) in towns + connectors:
        xs += [a[0], bb[0]]; ys += [a[1], bb[1]]
    for (ex, ey) in extra:
        xs.append(ex); ys.append(ey)
    for comp in comps:
        for nm, (px, py) in comp.pins.items():
            xs.append(px); ys.append(py)
    for (a, bb, w) in highway:
        ys += [a[1] - w / 2, a[1] + w / 2]
    if not xs:
        return None
    return min(xs), max(xs), min(ys), max(ys)


def autosize_board(spec):
    """Size the board so every road/street/highway/junction fits with
    BOARD_MARGIN. Two modes:

      AUTO (default): compute the smallest board that fits and center it on the
      feature bounding box (writes board.center so the box hugs the content).

      LOCKED (board['auto_size']=false): honor the user's exact length/width and
      center VERBATIM - no clamping. The board may be smaller than the content;
      use check_fit() to detect that and refuse to render.
    """
    b = spec['board']
    wall = b.get('wall', 4)
    locked = b.get('auto_size', True) is False      # user set an explicit size
    b.setdefault('length', 50)
    b.setdefault('width', 50)

    if locked:
        # trust the editor: fixed size + center, no auto-expand, no clamp.
        b.setdefault('center', {'x': 0.0, 'y': 0.0})
        return

    # AUTO: iterate (highway spans board width, so extents shift until stable).
    margin = b.get('margin', BOARD_MARGIN)
    for _ in range(4):
        ext = feature_extents(spec)
        if ext is None:
            return
        x0, x1, y0, y1 = ext
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        newL = round((x1 - x0) + 2 * (margin + wall), 1)
        newW = round((y1 - y0) + 2 * (margin + wall), 1)
        b['center'] = {'x': round(cx, 2), 'y': round(cy, 2)}
        if abs(newL - b['length']) < 0.5 and abs(newW - b['width']) < 0.5:
            b['length'], b['width'] = newL, newW
            break
        b['length'], b['width'] = newL, newW


def check_fit(spec):
    """Return a human-readable error string if the content doesn't fit inside
    the board rectangle (with wall thickness), else None. Only meaningful when
    locked; auto sizing always fits by construction."""
    b = spec['board']
    ext = feature_extents(spec)
    if ext is None:
        return None
    wall = b.get('wall', 4)
    cx, cy = board_center(spec)
    L, W = b['length'], b['width']
    # usable inner cavity (inside the walls)
    ix0, ix1 = cx - L / 2 + wall, cx + L / 2 - wall
    iy0, iy1 = cy - W / 2 + wall, cy + W / 2 - wall
    x0, x1, y0, y1 = ext
    if x0 < ix0 or x1 > ix1 or y0 < iy0 or y1 > iy1:
        return (f'board too small: parts/roads fall outside the {L:.0f}×{W:.0f}mm '
                f'board. Enlarge the board (drag its edges out) or move parts in.')
    return None


def used_pins(spec):
    """Set of 'comp.pin' names that are electrically used (in a connection or a
    junction). Used for optional minimal-routing mode."""
    used = set()
    for conn in spec.get('connections', []):
        used.add(conn['from']); used.add(conn['to'])
    for c in spec['components']:
        cid = c['id']
        for nm in c.get('junctions', {}):
            used.add(f'{cid}.{nm}')
    return used


def streeted_names(spec, comp_id, names):
    """Which pins in a row get a street. By DEFAULT every pin does (streets are
    infrastructure). With board.streets_used_only, only used pins do."""
    if spec['board'].get('streets_used_only'):
        used = used_pins(spec)
        return [nm for nm in names if f'{comp_id}.{nm}' in used]
    return list(names)


def board_defaults(spec):
    """Resolve the board-wide default widths/gap, falling back to the module
    constants when a key is absent. Per-component overrides still beat these."""
    b = spec['board']
    return {
        'street_w': b.get('street_w', STREET_W),
        'town_w': b.get('town_w', TOWN_W),
        # junction_gap removed (holes inline at fixed spacing now)
        'street_len': b.get('street_len', STREET_LEN),
    }


def build_roads(spec):
    """Return road geometry. Every pin gets a street by default (streets are
    infrastructure); board.streets_used_only limits to wired pins. Junction
    streets are longer (in-line break-out runway). streets = [(a, b, width)]."""
    bd = board_defaults(spec)
    comps = [Comp(c, bd) for c in spec['components']]
    streets, towns, extra_holes = [], [], []
    for comp in comps:
        for (orient, side, names, a, b) in comp.rows:
            # towns carry the owning comp's town_w (per-segment width)
            towns.append((a, b, comp.town_w))
            street_set = set(streeted_names(spec, comp.id, names))
            for nm in names:
                px, py = comp.pins[nm]
                jn = comp.junctions.get(nm, 0)         # # of extra break-out holes
                # Streets are INFRASTRUCTURE: every pin gets a street by default
                # (board.streets_used_only limits to wired pins).
                if nm not in street_set:
                    continue
                # INLINE JUNCTION HOLES: holes sit ON the main street between the
                # pin and the town. Layout: PIN → [holes] → [clamp clearance] →
                # TOWN. The town road position (a[0]/a[1]) already accounts for the
                # hole reach (see Comp._row_reach), so the street simply runs from
                # the pin to the town + overlap, and the holes drop inline right
                # after the pin. No tail past the town.
                # Holes spaced at JUNCTION_HOLE_SPACING (2.54mm, standard pitch).
                overlap = comp.town_w / 2
                if orient == 'v':
                    tx = a[0]
                    direction = 1 if tx > px else -1   # street runs toward town
                    end_x = tx + direction * overlap   # into the town road
                    streets.append(((px, py), (end_x, py), comp.street_w))
                    for k in range(jn):
                        hx = px + direction * ((k + 1) * JUNCTION_HOLE_SPACING)
                        extra_holes.append((hx, py))
                else:
                    ty = a[1]
                    direction = 1 if ty > py else -1
                    end_y = ty + direction * overlap
                    streets.append(((px, py), (px, end_y), comp.street_w))
                    for k in range(jn):
                        hy = py + direction * ((k + 1) * JUNCTION_HOLE_SPACING)
                        extra_holes.append((px, hy))
    # HIGHWAYS: horizontal corridors across the board. Defined by the spec as
    #   "highways": [{"y": 10}, {"y": -15}]  (one or more).
    # If none given, auto-place a single one just below the lowest town road.
    # Each highway runs edge-to-edge (exit doors). Every town road connects to
    # its NEAREST highway by a short perpendicular drop.
    bl = spec['board']['length']
    bcx = float((spec['board'].get('center') or {}).get('x', 0.0))
    hx0, hx1 = bcx - bl / 2 - 1, bcx + bl / 2 + 1
    hw_defs = spec['board'].get('highways')
    if not hw_defs:
        town_ylow = min([min(a[1], bb[1]) for (a, bb, tw) in towns], default=0)
        # auto single highway keeps the default width
        hw_defs = [{'y': town_ylow - (HIGHWAY_W / 2 + 1.5)}]
    # highway carries its own width: that def's "w" or the default HIGHWAY_W
    hw_default = spec['board'].get('highway_w', HIGHWAY_W)
    highway = [((hx0, hwd['y']), (hx1, hwd['y']), hwd.get('w', hw_default))
               for hwd in hw_defs]
    hw_ys = [hwd['y'] for hwd in hw_defs]

    # connectors: each town road drops to its NEAREST highway (from whichever
    # end of the town road is closer to that highway). A connector keeps the
    # town-road width it drops from (town_w of that town road).
    connectors = []
    for (a, bb, tw) in towns:
        ylo, yhi = min(a[1], bb[1]), max(a[1], bb[1])
        tx = a[0]
        # nearest highway to this town road's span
        def dist(y):
            if ylo <= y <= yhi:
                return 0
            return min(abs(y - ylo), abs(y - yhi))
        ny = min(hw_ys, key=dist)
        # connect from the town-road end closest to that highway
        start_y = ylo if abs(ny - ylo) <= abs(ny - yhi) else yhi
        connectors.append(((tx, start_y), (tx, ny), tw))
    return comps, streets, towns, highway, connectors, extra_holes


# ---------- SCAD ----------
def scad_base(spec):
    comps, streets, towns, highway, connectors, extra_holes = build_roads(spec)
    b = spec['board']
    L, W, H, wall = b['length'], b['width'], b['base_h'], b.get('wall', 4)
    cx, cy = board_center(spec)           # board box offset; content stays put
    s = [f'// {spec["name"]} base - road network', '$fn=20;', 'difference(){']
    s.append(f'  translate([{cx-L/2},{cy-W/2},0]) cube([{L},{W},{H}]);')
    iL, iW = L - 2 * wall, W - 2 * wall
    s.append(f'  translate([{cx-iL/2},{cy-iW/2},{wall}]) cube([{iL},{iW},{H}]);')

    def z_h(depth):
        d = min(depth, wall - 0.6)
        return wall - d, d + 0.4

    nub_lines = []
    # highway: THICK CENTER nubs (not edge nubs), clear of the ends so wire can
    # be inserted and the bundle weaves around them. Town roads + streets rely
    # on the clamp plug by default, but can OPT IN to extra serpentine nubs
    # (board.town_nubs / board.street_nubs) for boards without clamps, or for
    # belt-and-suspenders retention on top of the clamp.
    z, h = z_h(HIGHWAY_DEPTH)
    for (a, bb, w) in highway:
        seg(s, a[0], a[1], bb[0], bb[1], w, z, h)
        nub_lines += highway_center_nubs(a[0], a[1], bb[0], bb[1], z,
                                         min(h - 0.4, (wall - z) - 0.3))
    # connectors (town->highway drop): plain grooves, no nubs
    for (a, bb, w) in connectors:
        seg(s, a[0], a[1], bb[0], bb[1], w, z, h)
    # town roads (optional serpentine nubs via board.town_nubs)
    z, h = z_h(TOWN_DEPTH)
    for (a, bb, w) in towns:
        seg(s, a[0], a[1], bb[0], bb[1], w, z, h)
        if b.get('town_nubs'):
            nub_lines += serpentine_nubs(a[0], a[1], bb[0], bb[1], w, z,
                                         min(h - 0.4, (wall - z) - 0.3))
    # streets (optional serpentine nubs via board.street_nubs)
    z, h = z_h(STREET_DEPTH)
    for (a, bb, w) in streets:
        seg(s, a[0], a[1], bb[0], bb[1], w, z, h)
        if b.get('street_nubs'):
            nub_lines += serpentine_nubs(a[0], a[1], bb[0], bb[1], w, z,
                                         min(h - 0.4, (wall - z) - 0.3))

    # MANUAL EXTRA NUBS: user click-placed retention bumps anywhere on a road
    # (highway/town/street), for spot-fixing a wire that keeps slipping at one
    # specific point. board.extra_nubs = [{"x":.., "y":.., "d":2.0}, ...].
    extra_nubs = b.get('extra_nubs') or []
    if extra_nubs:
        ez, eh = z_h(STREET_DEPTH)   # shallow default; fine under any groove depth
        for n in extra_nubs:
            nd = n.get('d', 1.8)
            nub_lines.append(f'  translate([{n["x"]:.2f},{n["y"]:.2f},{ez - 0.3:.2f}]) '
                             f'cylinder(h={eh + 0.3:.2f},d={nd});  // manual nub')

    # pin holes: BLIND holes (sealed bottom). Component pins + junction extras.
    # Optional FUNNEL: a countersink cone at the top mouth so a pin that isn't
    # perfectly straight self-centers onto the wire end below, instead of
    # needing to be forced in. board.pin_funnel = mouth diameter (0 = off,
    # must be > pin_hole_d to actually taper).
    pin_d = b.get('pin_hole_d', 1.0)
    hole_bottom = max(wall - STREET_DEPTH - 0.6, 0.6)
    hole_h = (wall + 2) - hole_bottom
    funnel_d = b.get('pin_funnel', 0) or 0
    funnel_h = 1.0   # countersink depth, cut from the top face downward
    def pin_hole(x, y, d):
        if funnel_d > d:
            s.append(f'  translate([{x:.2f},{y:.2f},{hole_bottom:.2f}]) '
                     f'cylinder(h={hole_h:.2f},d={d});')
            s.append(f'  translate([{x:.2f},{y:.2f},{(wall + 2) - funnel_h:.2f}]) '
                     f'cylinder(h={funnel_h + 0.6:.2f},d1={d},d2={funnel_d});  // contact funnel')
        else:
            s.append(f'  translate([{x:.2f},{y:.2f},{hole_bottom:.2f}]) '
                     f'cylinder(h={hole_h:.2f},d={d});')
    for comp in comps:
        for nm, (px, py) in comp.pins.items():
            pin_hole(px, py, pin_d)
    for (ex, ey) in extra_holes:
        # d=1.4 (vs 1.0): clearly wider than needed so the hole OVERLAPS the
        # street groove rather than sitting edge-tangent to its wall/cap, which
        # avoids non-manifold tangent edges.
        pin_hole(ex, ey, 1.4)

    # NOTE: no peg sockets needed. The clamp is a road-shaped plug that drops
    # into the TOP HALF of the street/town grooves (the groove itself is the
    # socket), pressing the wire in the bottom half and friction-locking.

    # EXIT DOORS: cut the edge wall where the highway reaches each end, so wires
    # on the highway can pass out to the outside world (motors, battery, etc.).
    # Toggled by board.exit_doors (default True).
    if b.get('exit_doors', True):
        z, h = z_h(HIGHWAY_DEPTH)
        for (a, bb, w) in highway:
            door_w = w                               # door scales with highway
            for (ex, ey) in (a, bb):
                # cut a gap through the nearest side wall at the highway height
                if abs(ex - (cx - L / 2)) < abs(ex - (cx + L / 2)):
                    wall_x = cx - L / 2 - 1              # left wall
                else:
                    wall_x = cx + L / 2 - wall - 1       # right wall
                s.append(f'  translate([{wall_x:.2f},{ey - door_w/2:.2f},{z:.2f}]) '
                         f'cube([{wall + 2},{door_w},{h:.2f}]);  // exit door')

    cap_type = b.get('cap_type', 'nest')     # 'nest' (lift-off lid) or 'slide'

    # SLIDE RAILS (only for cap_type='slide'): lid rides grooves cut into the
    # top of the LEFT and RIGHT walls, and slides IN from the +Y end.
    if cap_type == 'slide':
        rail_z = H - RAIL_H
        for sx in (-1, 1):
            wx = cx + sx * (L / 2 - wall)
            rail_x = wx - sx * RAIL_DEPTH
            x0 = min(wx, rail_x)
            s.append(f'  translate([{x0:.2f},{cy-W/2 - 1:.2f},{rail_z:.2f}]) '
                     f'cube([{RAIL_DEPTH},{W + 2},{RAIL_H + 1}]);  // slide rail')
        # ENTRY SLOT: remove the top of the +Y END wall (full inner width) so
        # the lid can slide in there. The -Y end wall stays = the stop that
        # prevents the lid sliding out the far side.
        entry_w = L - 2 * wall + 2 * RAIL_DEPTH + 2   # spans both rails
        s.append(f'  translate([{cx-entry_w/2:.2f},{cy+W/2 - wall - 1:.2f},{rail_z:.2f}]) '
                 f'cube([{entry_w:.2f},{wall + 2},{RAIL_H + 2}]);  // lid entry slot')
    s.append('}')  # end difference

    # serpentine nubs - ADDED into the grooves so the wire must weave + lock
    if nub_lines:
        s.append('// serpentine retention nubs')
        s += nub_lines

    # DETENT bumps (slide lid only): lid clicks past these at the far (-Y) end.
    if cap_type == 'slide':
        det_y = cy - W / 2 + wall + 4
        for sx in (-1, 1):
            wx = cx + sx * (L / 2 - wall) - sx * (RAIL_DEPTH / 2)
            s.append(f'  translate([{wx:.2f},{det_y:.2f},{H - RAIL_H/2:.2f}]) '
                     f'sphere(d={DETENT_D});  // slide detent')
    return '\n'.join(s) + '\n'


PUSHER_H = 0.3          # how far a pusher rib presses the wire down
PUSHER_W = 1.2          # pusher rib width (narrower than the street)
CAP_FIT = 0.4           # clearance so the cap lip slides into the base cavity
# ---- sliding lid (pencil-box style) ----
RAIL_H = 2.0            # rail groove height (lid thickness that slides in)
RAIL_DEPTH = 2.5        # how far the rail cuts into each side wall (more capture)
SLIDE_FIT = 0.4         # clearance so the lid slides smoothly
DETENT_D = 2.2          # click-lock detent bump diameter (bigger = won't break)
CLAMP_PLATE_T = 1.5     # clamp bar top plate thickness
CLAMP_BAR_W = 0.0       # (computed per bar)


def _row_key(comp_id, orient, fixed_coord):
    return f'{comp_id}|{orient}|{round(fixed_coord,1)}'


def scad_clamps(spec):
    """Clamp = a ROAD-SHAPED PLUG per pin row that traces the EXACT groove
    layout (town road + full streets, minus the pin/exposed zone) at plug
    depth, dropping into the top of the deepened grooves. Wire in bottom,
    plug on top presses it + press-fits. One connected plug per row.

    Streets are traced with the SAME coordinates build_roads() produced (so the
    plug lines up with the grooves exactly), just trimmed at the pin end by
    CLAMP_SKIP. Press-fit: plug outline == groove width (CLAMP_PLUG_FIT=0)."""
    comps, streets, towns, highway, connectors, extra_holes = build_roads(spec)
    plug_d = CLAMP_PLUG_H
    # clamp tightness: `gap` is how much NARROWER the plug is than the groove.
    #   gap = 0.0  -> plug == groove: tightest press-fit (may need a hard push)
    #   gap = 0.3  -> snug default (slides in with firm pressure)
    #   gap = 0.6  -> loose (easy to insert/remove, lighter wire grip)
    # board.clamp_fit overrides; clamped to a sane 0..1.0mm range.
    gap = spec['board'].get('clamp_fit', CLAMP_PLUG_FIT)
    gap = max(0.0, min(1.0, float(gap)))
    s = [f'// {spec["name"]} CLAMP PLUGS (trace groove shape, drop into top)',
         '$fn=20;']

    # NUB CLEARANCE: town/street serpentine nubs and manual extra_nubs sit at
    # the same z-depth the clamp plug drops into. Without a notch the plug
    # would collide with (or fail to seat over) every nub on its row, so wrap
    # the whole clamp body in a difference() that punches a clearance hole
    # through the plug at each nub point (nub pokes through, plug still grips
    # everywhere else).
    nub_pts = clampable_nub_points(spec, streets, towns)
    if nub_pts:
        s.append('difference(){')
        s.append('union(){')

    # town-road coord -> its component/orientation, to group streets per row
    for comp in comps:
        for (orient, side, names, ta, tb) in comp.rows:
            u = streeted_names(spec, comp.id, names)
            if not u:
                continue
            s.append(f'// --- clamp plug: {comp.id} {side} row ---')
            s.append('union(){')
            # plug widths MATCH this component's base-groove widths (press-fit).
            # Floored at 0.3mm so a narrow street + loose clamp_fit never yields
            # a zero/negative-width plug (degenerate OpenSCAD geometry).
            tw = max(comp.town_w - gap, 0.3)
            sw = max(comp.street_w - gap, 0.3)
            # town-road bar (full town road length, matches the groove)
            seg(s, ta[0], ta[1], tb[0], tb[1], tw, 0, plug_d)
            # street plugs: ONE SOLID finger covering the full street from near
            # the pin all the way to just before the extra hole at the END.
            # Extra holes are placed at the very TIP of the street, so the finger
            # covers everything up to them (no orphan piece past the hole).
            for nm in u:
                px, py = comp.pins[nm]
                jn = comp.junctions.get(nm, 0)
                overlap = comp.town_w / 2
                # Clamp plug now spans from (pin+CLAMP_SKIP) to town. Holes are
                # inline on the street (drilled through the plug), not past the town.
                if orient == 'v':
                    tx = ta[0]; fixed = py
                    direction = 1 if tx > px else -1
                    start = px + direction * CLAMP_SKIP
                    finish = tx + direction * overlap   # into town road
                    seg(s, start, fixed, finish, fixed, sw, 0, plug_d)
                else:
                    ty = ta[1]; fixed = px
                    direction = 1 if ty > py else -1
                    start = py + direction * CLAMP_SKIP
                    finish = ty + direction * overlap
                    seg(s, fixed, start, fixed, finish, sw, 0, plug_d)
            s.append('}')

    if nub_pts:
        s.append('}')   # end union (all clamp bars)
        clearance = 0.6   # extra radius beyond the nub itself, so it isn't a tangent fit
        for (nx, ny, nd) in nub_pts:
            s.append(f'  translate([{nx:.2f},{ny:.2f},{-0.5:.2f}]) '
                     f'cylinder(h={plug_d + 1.0:.2f},d={nd + clearance:.2f});  // nub clearance')
        s.append('}')   # end difference
    return '\n'.join(s) + '\n'


LABEL_EMBOSS_H = 0.6        # raised-text height above the lid top face
LABEL_FS_MIN = 2.5          # clamp: smallest font (mm)
LABEL_FS_MAX = 6.0          # clamp: largest font (mm)
LABEL_WIDTH_FRAC = 0.70     # text width ~ 70% of the component's pin x-span


def label_solids(spec, top_z, depth):
    """Return OpenSCAD strings: one text solid per component, labeling that
    component, kept CLEAR of the pin holes. `depth` controls the mode/height:
      depth > 0  -> RAISED emboss: text extruded UP from the top face by depth.
                    Caller UNIONs these onto the lid body.
      depth < 0  -> ENGRAVED: text extruded starting just below the top face up
                    through it (so it cleanly subtracts). Caller SUBTRACTS these
                    inside the hole-cutting difference().
      depth == 0 -> no label geometry (returns []).

    Placement keeps text off the pin-hole cylinders by fitting the text block
    inside the HOLE-FREE rectangle:
      * left/right (x-separated) rows -> pins occupy the left and right columns,
        so the clear zone is the CENTER strip in X: allowed WIDTH =
        2*(min|pin_x - cx| - cap_pin_d - margin), full pin y-span is clear.
      * top/bottom (y-separated) rows -> clear zone is the center strip in Y:
        allowed HEIGHT = 2*(min|pin_y - cy| - cap_pin_d - margin).
      * single row -> shift the label center OFF the pin row onto solid lid.
    The label is shortened first (friendly name, else id with the trailing
    instance suffix stripped and capped). Font is shrunk so the block fits the
    clear width; if even LABEL_FS_MIN won't fit, fs drops below the min (or the
    text is truncated with an ellipsis) so it never crosses a hole."""
    import re
    if depth == 0:
        return []
    bd = board_defaults(spec)
    cap_pin_d = spec['board'].get('pin_hole_d', 1.0) + 0.6
    margin = 1.0
    # optional user font-size cap (mm). The auto fit still computes the LARGEST
    # size that clears the pin holes; label_size lets the user force something
    # SMALLER (or up to the fit). 0 / absent -> pure auto-fit.
    user_size = spec['board'].get('label_size', 0) or 0
    out = []
    for c in spec['components']:
        comp = Comp(c, bd)
        if not comp.pins:
            continue
        xs = [p[0] for p in comp.pins.values()]
        ys = [p[1] for p in comp.pins.values()]
        cx_comp = sum(xs) / len(xs)
        cy_comp = sum(ys) / len(ys)
        # SHORT label: prefer the friendly name; else strip the trailing
        # instance suffix (_1, _2 ...) from the id and cap the length.
        label = str(c.get('name') or '').strip()
        if not label:
            label = re.sub(r'_\d+$', '', str(comp.id or ''))
            if len(label) > 10:
                label = label[:10]
        if not label:
            continue

        orients = {r[0] for r in comp.rows}
        two_row = len(comp.rows) >= 2
        n = max(len(label), 1)
        cx_lbl, cy_lbl = cx_comp, cy_comp

        if two_row:
            if 'v' in orients:
                # left/right rows: pins in the left+right columns -> clear zone is
                # the CENTER strip in X; full pin y-span is clear in Y.
                dx = min(abs(px - cx_comp) for px in xs)   # centroid->nearest column
                allowed_w = max(2 * (dx - cap_pin_d / 2 - margin), 0.1)
                allowed_h = (max(ys) - min(ys)) or LABEL_FS_MAX
            else:
                # top/bottom rows: clear zone is the center strip in Y; full pin
                # x-span is clear in X.
                dy = min(abs(py - cy_comp) for py in ys)
                allowed_h = max(2 * (dy - cap_pin_d / 2 - margin), 0.1)
                allowed_w = (max(xs) - min(xs)) or (LABEL_FS_MAX * 0.6 * n)
            # font that fits the clear rectangle in BOTH axes
            fs = min(allowed_w / (0.6 * n), allowed_h)
            fs = min(fs, LABEL_FS_MAX)
            # If LABEL_FS_MIN would overflow the clear WIDTH, truncate the label
            # (ellipsis) so the block still fits rather than crossing a hole.
            if LABEL_FS_MIN * 0.6 * n > allowed_w:
                max_chars = max(int(allowed_w / (0.6 * LABEL_FS_MIN)), 1)
                if max_chars < len(label):
                    label = (label[:max(max_chars - 1, 1)] + '.') if max_chars > 1 else label[:1]
                    n = max(len(label), 1)
                fs = max(fs, LABEL_FS_MIN)
            else:
                fs = max(fs, LABEL_FS_MIN)
        else:
            # single-row: size to the pin x-span, then shift the center OFF the
            # pin row onto solid lid (away from the pins).
            x_span = max(xs) - min(xs)
            target_w = LABEL_WIDTH_FRAC * (x_span if x_span > 0 else LABEL_FS_MAX * 0.6 * n)
            fs = target_w / (0.6 * n)
            fs = max(LABEL_FS_MIN, min(LABEL_FS_MAX, fs))
            shift = cap_pin_d + fs
            if 'v' in orients:   # pins on a vertical line (constant x)
                cx_lbl = cx_comp + (shift if cx_comp >= comp.x else -shift)
            else:                # pins on a horizontal line (constant y)
                cy_lbl = cy_comp + (shift if cy_comp >= comp.y else -shift)

        # user size cap: never larger than the fit (so holes stay clear), but
        # the user can dial it smaller. Keep a sane floor.
        if user_size > 0:
            fs = max(0.8, min(fs, user_size))

        if depth > 0:
            out.append(
                f'  translate([{cx_lbl:.2f},{cy_lbl:.2f},{top_z:.2f}]) '
                f'linear_extrude({depth:.2f}) '
                f'text("{label}", size={fs:.2f}, halign="center", valign="center", '
                f'$fn=24);  // emboss label {label}')
        else:
            d = abs(depth)
            out.append(
                f'  translate([{cx_lbl:.2f},{cy_lbl:.2f},{top_z - d:.2f}]) '
                f'linear_extrude({d + 0.1:.2f}) '
                f'text("{label}", size={fs:.2f}, halign="center", valign="center", '
                f'$fn=24);  // engrave label {label}')
    return out


def scad_cap(spec):
    """Cap/lid. Two types selectable via board['cap_type']:
      'nest'  (default) - top plate + lip that drops into the cavity (lift off)
      'slide' - flat lid that slides into the wall rails, detent click-lock"""
    b = spec['board']
    if b.get('cap_type', 'nest') == 'slide':
        return scad_cap_slide(spec)
    return scad_cap_nest(spec)


def scad_cap_nest(spec):
    """NEST lid = a single plug that drops into the base cavity and sits FLUSH
    with the wall tops (no overhanging top plate — saves material, flush finish).
    Fit is set by board.lid_fit (clearance per side between plug and cavity wall):
      smaller = tighter. Presets in the UI: loose 0.5 / snug 0.3 / tight 0.15."""
    comps, streets, towns, highway, connectors, extra_holes = build_roads(spec)
    b = spec['board']
    L, W, wall = b['length'], b['width'], b.get('wall', 4)
    cx, cy = board_center(spec)
    cavity_depth = b['base_h'] - wall           # how deep the cavity is
    # plug fills the full cavity depth so its top is flush with the wall tops.
    plug_h = max(cavity_depth, 0.8)
    # clearance per side between the plug and the cavity walls (lid tightness)
    lid_fit = b.get('lid_fit', CAP_FIT)
    iL, iW = L - 2 * wall - 2 * lid_fit, W - 2 * wall - 2 * lid_fit
    # cap pin-hole clearance tracks the base pin-hole size (pin_hole_d + 0.6),
    # widened to clear the base's contact funnel mouth if one is enabled.
    cap_pin_d = max(b.get('pin_hole_d', 1.0) + 0.6, b.get('pin_funnel', 0) or 0)
    top_z = cavity_depth                        # top surface of the flush plug
    s = [f'// {spec["name"]} cap (NEST plug: flush, lift off)', '$fn=20;']
    s.append('union(){')      # body (+ optional labels) minus pin holes
    s.append('difference(){')
    # the plug: sits from the cavity floor (z=0) up to flush with the walls
    s.append(f'  translate([{cx-iL/2},{cy-iW/2},0]) cube([{iL},{iW},{plug_h:.2f}]);')
    for comp in comps:
        for nm, (px, py) in comp.pins.items():
            s.append(f'  translate([{px:.2f},{py:.2f},{-1}]) '
                     f'cylinder(h={plug_h + 2},d={cap_pin_d:.2f});')
    for (ex, ey) in extra_holes:
        s.append(f'  translate([{ex:.2f},{ey:.2f},{-1}]) '
                 f'cylinder(h={plug_h + 2},d={cap_pin_d:.2f});')
    # LABELS. depth>0 raised (unioned after the difference), depth<0 engraved
    # (subtracted inside this difference, kept clear of the pin holes), depth==0
    # none. Default height = LABEL_EMBOSS_H when board.label_depth is absent.
    depth = b.get('label_depth', LABEL_EMBOSS_H)
    want_labels = b.get('labels') and depth != 0
    if want_labels and depth < 0:
        s += label_solids(spec, top_z, depth)  # engraved -> subtract (inside diff)
    s.append('}')            # end difference (body minus holes + engraving)
    if want_labels and depth > 0:
        s += label_solids(spec, top_z, depth)  # raised -> union onto the lid
    s.append('}')            # end outer union
    return '\n'.join(s) + '\n'


def scad_cap_slide(spec):
    """Flat sliding lid that rides in the base wall rails. No finger tab.
    Lid edges land INSIDE the rail grooves (between the inner wall face and the
    groove's far edge), with SLIDE_FIT clearance so it slides smoothly."""
    comps, streets, towns, highway, connectors, extra_holes = build_roads(spec)
    b = spec['board']
    L, W, wall = b['length'], b['width'], b.get('wall', 4)
    cx, cy = board_center(spec)
    base_h = b['base_h']
    # rail groove in X runs INWARD from the inner wall face (L/2-wall) by
    # RAIL_DEPTH. The lid edge must land INSIDE that groove: reach up to the
    # inner wall face minus slide fit (so it's captured by the rail overhang).
    inner_face = L / 2 - wall                  # e.g. 37.5
    lid_half = inner_face - SLIDE_FIT          # edge sits in the groove (e.g. 37.15)
    lid_L = 2 * lid_half
    lid_W = W - 2 * SLIDE_FIT                   # length (Y) nearly full board
    lid_t = RAIL_H - SLIDE_FIT                  # thickness fits rail height
    z0 = base_h - RAIL_H
    # cap pin-hole clearance tracks the base pin-hole size (pin_hole_d + 0.6),
    # widened to clear the base's contact funnel mouth if one is enabled.
    cap_pin_d = max(b.get('pin_hole_d', 1.0) + 0.6, b.get('pin_funnel', 0) or 0)
    s = [f'// {spec["name"]} cap (SLIDE: rides wall rails, no tab)', '$fn=20;']
    s.append('union(){')      # body (+ optional labels) minus pin holes
    s.append('difference(){')
    s.append(f'  translate([{cx-lid_L/2},{cy-lid_W/2},{z0:.2f}]) '
             f'cube([{lid_L:.2f},{lid_W:.2f},{lid_t:.2f}]);')
    for comp in comps:
        for nm, (px, py) in comp.pins.items():
            s.append(f'  translate([{px:.2f},{py:.2f},{z0-1:.2f}]) '
                     f'cylinder(h={lid_t + 2},d={cap_pin_d:.2f});')
    for (ex, ey) in extra_holes:
        s.append(f'  translate([{ex:.2f},{ey:.2f},{z0-1:.2f}]) '
                 f'cylinder(h={lid_t + 2},d={cap_pin_d:.2f});')
    # detent notches on the lid edges (receive the base detent bumps near -Y)
    det_y = cy - W / 2 + wall + 4
    for sx in (-1, 1):
        s.append(f'  translate([{cx + sx * lid_half:.2f},{det_y:.2f},{z0 + lid_t/2:.2f}]) '
                 f'sphere(d={DETENT_D + 0.3});  // detent notch')
    # LABELS. depth>0 raised (unioned after the difference), depth<0 engraved
    # (subtracted inside this difference), depth==0 none. top face = z0+lid_t.
    depth = b.get('label_depth', LABEL_EMBOSS_H)
    want_labels = b.get('labels') and depth != 0
    top_z = z0 + lid_t
    if want_labels and depth < 0:
        s += label_solids(spec, top_z, depth)  # engraved -> subtract (inside diff)
    s.append('}')            # end difference (body minus holes + engraving)
    if want_labels and depth > 0:
        s += label_solids(spec, top_z, depth)  # raised -> union onto the lid
    s.append('}')            # end outer union
    return '\n'.join(s) + '\n'


# ---------- schematic ----------
def schematic(spec, path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    comps, streets, towns, highway, connectors, extra_holes = build_roads(spec)
    b = spec['board']; L, W = b['length'], b['width']
    cx, cy = board_center(spec)
    fig, ax = plt.subplots(figsize=(15, 8))
    ax.add_patch(plt.Rectangle((cx-L/2, cy-W/2), L, W, fill=False, ec='black', lw=2.5))
    # roads
    for (a, bb, w) in highway:
        ax.plot([a[0], bb[0]], [a[1], bb[1]], color='orange', lw=10, alpha=0.5, solid_capstyle='round')
    for (a, bb, w) in connectors:
        ax.plot([a[0], bb[0]], [a[1], bb[1]], color='orange', lw=5, alpha=0.4, solid_capstyle='round')
    for (a, bb, w) in towns:
        ax.plot([a[0], bb[0]], [a[1], bb[1]], color='tab:blue', lw=6, alpha=0.5, solid_capstyle='round')
    for (a, bb, w) in streets:
        ax.plot([a[0], bb[0]], [a[1], bb[1]], color='green', lw=max(2, w), alpha=0.8, solid_capstyle='round')
    # pins + labels
    pinmap = {}
    for comp in comps:
        for nm, (px, py) in comp.pins.items():
            pinmap[f'{comp.id}.{nm}'] = (px, py)
            ax.scatter([px], [py], s=70, c='white', ec='black', zorder=5)
        ax.text(comp.x, comp.y + 22, comp.id, ha='center', fontsize=10, weight='bold')
    # junction extra holes (external in/out)
    for (ex, ey) in extra_holes:
        ax.scatter([ex], [ey], s=55, c='yellow', ec='black', marker='s', zorder=6)
    # traffic (red example wires) - just straight guide lines pin->pin for now
    for conn in spec.get('connections', []):
        a = pinmap.get(conn['from']); bb = pinmap.get(conn['to'])
        if a and bb:
            ax.plot([a[0], bb[0]], [a[1], bb[1]], color='red', lw=1.5, alpha=0.7,
                    linestyle='--', zorder=6)
    ax.set_aspect('equal'); ax.set_xlim(cx-L/2-6, cx+L/2+6); ax.set_ylim(cy-W/2-8, cy+W/2+8)
    ax.set_title(f"{spec['name']} road network  "
                 f"(green=street  blue=town  orange=highway  red=traffic)")
    plt.tight_layout(); plt.savefig(path, dpi=130)


def render_repair(name, part):
    scad = os.path.join(HERE, f'{name}_{part}.scad')
    stl = os.path.join(HERE, f'{name}_{part}.stl')
    openscad = '/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD'
    if not os.path.exists(openscad):
        return
    subprocess.run([openscad, '--backend=Manifold', '-o', stl, scad],
                   capture_output=True, timeout=200)
    try:
        import trimesh, numpy as np
        m = trimesh.load(stl); m.merge_vertices()
        _, c = np.unique(m.edges_sorted, axis=0, return_counts=True)
        bad = int((c != 2).sum())
        if bad:
            m.merge_vertices(digits_vertex=3)
            m.update_faces(m.unique_faces()); m.update_faces(m.nondegenerate_faces())
            m.fill_holes(); m.fix_normals(); m.export(stl)
            _, c2 = np.unique(trimesh.load(stl).edges_sorted, axis=0, return_counts=True)
            print(f'  {part}: repaired {bad} -> {int((c2!=2).sum())} non-manifold')
        else:
            print(f'  {part}: manifold OK')
    except Exception as ex:
        print(f'  {part}: check skipped ({ex})')


def main():
    if len(sys.argv) < 2:
        sys.exit('usage: python3 roadmap.py <spec.json>')
    spec = json.load(open(sys.argv[1]))
    name = spec['name']
    autosize_board(spec)                      # grow board so no road is clipped
    b = spec['board']
    print(f'  board auto-sized to {b["length"]} x {b["width"]} mm')
    open(os.path.join(HERE, f'{name}_base.scad'), 'w').write(scad_base(spec))
    open(os.path.join(HERE, f'{name}_cap.scad'), 'w').write(scad_cap(spec))
    open(os.path.join(HERE, f'{name}_clamps.scad'), 'w').write(scad_clamps(spec))
    schematic(spec, os.path.join(HERE, f'{name}_roads.png'))
    ncomp = len(spec['components'])
    npin = sum(len(r['pins']) for c in spec['components'] for r in c['rows'])
    nbars = len(clamp_rows(spec))
    print(f'{name}: {ncomp} components, {npin} pins, '
          f'{len(spec.get("connections",[]))} connections, {nbars} clamp bars')
    render_repair(name, 'base')
    render_repair(name, 'cap')
    render_repair(name, 'clamps')
    print(f'wrote {name}_base/cap/clamps .stl + .png')


if __name__ == '__main__':
    main()
