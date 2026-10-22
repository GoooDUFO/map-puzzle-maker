"""Browser glue for the map-puzzle pipeline (runs inside Pyodide, in a web worker).

The heavy lifting is the same code as the desktop workflow: extrude_map.main() builds
the plate and verify_plate's checks measure it. This module adds the steps that were
done by hand per country:
  load()     parse a geoBoundaries GeoJSON, fix antimeridian wrap, summarise features
  match()    pair source names with a pasted quiz list; fold leftovers into a neighbour
  prepare()  merge units, fill enclave holes, drop small islands, move far parts to
             the spot that gives the largest print, and return an SVG-ready preview
  build()    run extrude_map + verify_plate and return the files

Every call takes and returns JSON strings so the JS side never touches Python objects.
"""
import difflib
import io
import json
import math
import os
import re
import sys
import unicodedata
import zipfile

import numpy as np
import shapely
from shapely.affinity import translate
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.ops import unary_union

S = {"log": None}
KM_PER_DEG = 111.32
WORK = "/work"


# ---------------------------------------------------------------- helpers

def _parts(g):
    if g is None or g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    if g.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [p for x in g.geoms for p in _parts(x)]
    return []


def _valid(g):
    if not g.is_valid:
        g = shapely.make_valid(g)
    return unary_union(_parts(g)) if g.geom_type == "GeometryCollection" else g


def _log(text):
    if S["log"] is not None:
        S["log"](text)


class _JsWriter(io.TextIOBase):
    def write(self, s):
        if s:
            _log(s)
        return len(s)


# ---------------------------------------------------------------- Pyodide workarounds
#
# GEOS is built without C++ exception catching in Pyodide, so a TopologyException is
# FATAL there (it kills the interpreter) instead of becoming a Python error — and GEOS's
# own robust-overlay fallback chain relies on catching them internally. So every overlay
# goes through fixed-precision snap rounding (robust for valid input, never needs the
# fallback), after invalid polygonal inputs are repaired. 1e-6 is ~10cm in degrees and far
# below anything printable in mm, so it never changes a result you could see. (1e-7 was
# too fine: unioning float32 STL triangles left hairline splits along shared edges.)
GRID = 1e-6


def _fix(g):
    if not isinstance(g, shapely.Geometry) or g.is_empty:
        return g
    if g.geom_type == "GeometryCollection":
        polys = _parts(g)
        return unary_union_raw(polys) if polys else g
    if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_valid:
        polys = _parts(shapely.make_valid(g))
        return MultiPolygon(polys) if polys else Polygon()
    return g


def _install_safe_overlay():
    global unary_union_raw
    raw = {n: getattr(shapely, n) for n in ("difference", "intersection", "union", "symmetric_difference")}
    raw_all = shapely.union_all
    unary_union_raw = lambda gs: raw_all(gs, grid_size=GRID)  # noqa: E731

    def binary(name):
        orig = raw[name]

        def op(a, b, grid_size=None, **kw):
            return orig(_fix(a), _fix(b), grid_size=GRID if grid_size is None else grid_size, **kw)
        return op

    for n in raw:
        setattr(shapely, n, binary(n))

    def union_all(geoms, grid_size=None, axis=None, **kw):
        if isinstance(geoms, shapely.Geometry):
            geoms = [geoms]
        if axis is None:
            geoms = [_fix(g) for g in np.asarray(geoms, dtype=object).ravel() if g is not None]
        return raw_all(geoms, grid_size=GRID if grid_size is None else grid_size, axis=axis, **kw)

    shapely.union_all = union_all
    shapely.unary_union = union_all


def _install_trimesh_patches():
    """Trimesh.body_count needs scipy.sparse (a ~13MB download just for this); count the
    connected bodies with networkx instead, which 3MF export needs anyway."""
    import networkx as nx
    import trimesh

    def body_count(self):
        if len(self.faces) == 0:
            return 0
        return nx.number_connected_components(nx.from_edgelist(self.edges_unique))

    trimesh.Trimesh.body_count = property(body_count)


try:
    import pyodide  # noqa: F401
    IN_PYODIDE = True
except ImportError:
    IN_PYODIDE = False
if IN_PYODIDE:
    _install_safe_overlay()
    _install_trimesh_patches()


def set_logger(fn):
    S["log"] = fn


def set_triangulator(earcut):
    """trimesh needs a polygon triangulator; its usual engines (mapbox_earcut, triangle,
    manifold3d) are C extensions with no Pyodide build, so hand it the JS earcut."""
    import trimesh
    from pyodide.ffi import to_js

    def triangulate_polygon(polygon, triangle_args=None, engine=None, force_vertices=False, **kw):
        rings = [np.asarray(polygon.exterior.coords)[:-1, :2]]
        rings += [np.asarray(r.coords)[:-1, :2] for r in polygon.interiors]
        verts = np.vstack(rings).astype(np.float64)
        holes, n = [], len(rings[0])
        for r in rings[1:]:
            holes.append(n)
            n += len(r)
        idx = earcut(to_js(verts.ravel().tolist()), to_js(holes), 2).to_py()
        return verts, np.asarray(idx, dtype=np.int64).reshape(-1, 3)

    trimesh.creation.triangulate_polygon = triangulate_polygon


# ---------------------------------------------------------------- load

def load(text, name_prop="shapeName"):
    gj = json.loads(text)
    feats = []
    for f in gj.get("features", []):
        if not f.get("geometry"):
            continue
        g = _valid(shape(f["geometry"]))
        props = f.get("properties") or {}
        name = props.get(name_prop) or props.get("shapeName") or props.get("name") or f"Region {len(feats) + 1}"
        feats.append([str(name), g])
    if not feats:
        raise ValueError("no polygon features in that file")

    # antimeridian: a country straddling 180 deg (NZ's Chathams, Fiji) comes through
    # with parts near -180, so shift the western parts east by 360
    allb = np.array([g.bounds for _, g in feats])
    wrapped = allb[:, 2].max() - allb[:, 0].min() > 180
    if wrapped:
        for f in feats:
            f[1] = unary_union([translate(p, 360, 0) if p.bounds[2] < 0 else p for p in _parts(f[1])])

    S["src"] = feats
    bs = np.array([g.bounds for _, g in feats])
    lat0 = (bs[:, 1].min() + bs[:, 3].max()) / 2.0
    S["k"] = k = math.cos(math.radians(lat0))
    rows = []
    for i, (n, g) in enumerate(feats):
        ps = _parts(g)
        rows.append({
            "i": i, "name": n, "parts": len(ps),
            "holes": sum(len(p.interiors) for p in ps),
            "km2": round(g.area * k * KM_PER_DEG ** 2, 1),
            "verts": sum(len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors) for p in ps),
        })
    return json.dumps({"features": rows, "wrapped": bool(wrapped),
                       "bounds": [float(bs[:, 0].min()), float(bs[:, 1].min()),
                                  float(bs[:, 2].max()), float(bs[:, 3].max())]})


# ---------------------------------------------------------------- name matching

_FILLER = (r"\b(region|regions|province|provincia|provinces|department|departement|departamento|"
           r"governorate|district|county|parish|municipality|municipio|state|oblast|prefecture|"
           r"republic|autonomous|territory|division|city|of|de|del|la|le|les|el|des|du|the|and|y)\b")


def _norm(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(re.sub(_FILLER, " ", s).split())


def parse_quiz(text):
    text = re.sub(r"\(\s*\d+\s*\)\s*$", "", text.strip())
    out = []
    for tok in re.split(r"[,\n;]+", text):
        tok = tok.strip().strip(".")
        if tok and tok not in out:
            out.append(tok)
    return out


def _score(a, b):
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    r = difflib.SequenceMatcher(None, na, nb).ratio()
    if na in nb or nb in na:
        r = max(r, 0.9)
    return r


def match(quiz_text):
    """Assign each source feature an output name from the quiz list. Unmatched features
    (e.g. a capital city the quiz folds into its province) default to merging into the
    unit they share the longest border with; if they touch nothing, they're dropped."""
    src = S["src"]
    quiz = parse_quiz(quiz_text)
    assign = [None] * len(src)
    notes = []
    if not quiz:
        return json.dumps({"assign": [n for n, _ in src], "quiz": [], "missing": [], "notes": []})

    # ties on the normalised name (Tashkent city vs Tashkent Region both -> "tashkent")
    # are broken by the raw spelling
    pairs = sorted(((_score(q, n), difflib.SequenceMatcher(None, q.lower(), n.lower()).ratio(), qi, si)
                    for qi, q in enumerate(quiz) for si, (n, _) in enumerate(src)), reverse=True)
    used_q, used_s = set(), set()
    for sc, _raw, qi, si in pairs:
        if sc < 0.6:
            break
        if qi in used_q or si in used_s:
            continue
        used_q.add(qi)
        used_s.add(si)
        assign[si] = quiz[qi]
        if src[si][0] != quiz[qi]:
            notes.append(f"renamed {src[si][0]} -> {quiz[qi]}")

    for si, (n, g) in enumerate(src):
        if assign[si] is not None:
            continue
        best, best_len = None, 0.0
        edge = g.boundary
        tol = 1e-4
        for sj, (_, h) in enumerate(src):
            if assign[sj] is None or not g.buffer(tol).intersects(h):
                continue
            L = edge.intersection(h.buffer(tol)).length
            if L > best_len:
                best, best_len = assign[sj], L
        if best is not None and best_len > 0.15 * edge.length:
            assign[si] = best
            notes.append(f"merged {n} into {best} (shares {100 * best_len / edge.length:.0f}% of its border)")
        else:
            assign[si] = ""
            notes.append(f"dropped {n} (no quiz name matched)")
    missing = [q for qi, q in enumerate(quiz) if qi not in used_q]
    return json.dumps({"assign": assign, "quiz": quiz, "missing": missing, "notes": notes})


# ---------------------------------------------------------------- layout

def _fill_enclave_holes(units):
    """Fill holes no other unit occupies (foreign enclaves, lakes); keep holes another
    unit sits in, so that unit nests in like a puzzle piece (Vienna, Prague, Arima)."""
    out, filled = {}, 0
    for name, g in units.items():
        others = [h for m, h in units.items() if m != name]
        new = []
        for p in _parts(g):
            keep = []
            for r in p.interiors:
                hole = Polygon(r)
                occ = sum(hole.intersection(h).area for h in others if h.intersects(hole))
                if occ > 0.01 * hole.area:
                    keep.append(r)
                else:
                    filled += 1
            new.append(Polygon(p.exterior, keep))
        out[name] = unary_union(new)
    return out, filled


def _bbox(geoms):
    b = np.array([g.bounds for g in geoms])
    return b[:, 0].min(), b[:, 1].min(), b[:, 2].max(), b[:, 3].max()


def _place_group(fixed, fixed_bb, comp, clear_u, bw=1.0, bh=1.0, n=70):
    """Translation (dx, dy) for `comp` that gives the largest print on a bw x bh bed, i.e.
    minimises max(W / bw, H / bh) of the combined bbox, keeping `clear_u` from `fixed`.
    Among exact ties, the one nearest the true position wins (distance 0 = stay put)."""
    cx0, cy0, cx1, cy1 = comp.bounds
    cw, ch = cx1 - cx0, cy1 - cy0
    fx0, fy0, fx1, fy1 = fixed_bb
    fw, fh = fx1 - fx0, fy1 - fy0
    rel = max(fw / bw, fh / bh)          # the fixed part's fill of the bed, the floor
    span = max(fw, fh)
    ex, ey = max(0.0, rel * bw - fw) + clear_u, max(0.0, rel * bh - fh) + clear_u
    xs = np.linspace(fx0 - cw - ex, fx1 + ex, n)
    ys = np.linspace(fy0 - ch - ey, fy1 + ey, n)
    X, Y = (a.ravel() for a in np.meshgrid(xs, ys))
    X, Y = np.append(X, cx0), np.append(Y, cy0)

    tol = max(span, 1e-9) * 0.004
    comp_s = comp.simplify(tol, preserve_topology=True)
    guard = fixed.simplify(tol, preserve_topology=True).buffer(clear_u)
    shapely.prepare(guard)

    def pick(X, Y):
        W = np.maximum(fx1, X + cw) - np.minimum(fx0, X)
        H = np.maximum(fy1, Y + ch) - np.minimum(fy0, Y)
        score = np.round(np.maximum(W / bw, H / bh) / rel, 9)
        dist = np.hypot(X - cx0, Y - cy0)
        order = np.lexsort((dist, score))
        for chunk in np.array_split(order, max(1, len(order) // 400)):
            moved = [translate(comp_s, X[j] - cx0, Y[j] - cy0) for j in chunk]
            hit = shapely.intersects(guard, np.array(moved, dtype=object))
            for j, h in zip(chunk, hit):
                if not h:
                    return X[j], Y[j], score[j], dist[j]
        return cx0, cy0, None, 0.0

    bx, by, bs, _ = pick(X, Y)
    if bs is None:
        return 0.0, 0.0
    # refine near the winner: same score, closer to the true spot
    step_x, step_y = xs[1] - xs[0], ys[1] - ys[0]
    rx = np.linspace(bx - step_x, bx + step_x, 15)
    ry = np.linspace(by - step_y, by + step_y, 15)
    RX, RY = (a.ravel() for a in np.meshgrid(rx, ry))
    fx, fy, fs, _ = pick(np.append(RX, bx), np.append(RY, by))
    if fs is not None and fs <= bs:
        bx, by = fx, fy
    return bx - cx0, by - cy0


def prepare(cfg_json):
    cfg = json.loads(cfg_json)
    src = S["src"]
    assign = cfg["assign"]
    # usable bed: the printer's build plate minus a margin on every side
    margin = float(cfg.get("margin", 2.5))
    bed_w, bed_h = float(cfg.get("bed_w", 180)), float(cfg.get("bed_h", 180))
    uw, uh = max(bed_w - 2 * margin, 10.0), max(bed_h - 2 * margin, 10.0)
    island = float(cfg.get("island_mm2", 15))
    clear_mm = float(cfg.get("clearance_mm", 2.0))
    overrides = cfg.get("overrides", {})

    # 1. merge sources into output units
    order, groups = [], {}
    for (n, g), out in zip(src, assign):
        if not out:
            continue
        if out not in groups:
            order.append(out)
            groups[out] = []
        groups[out].append(g)
    units = {u: _valid(unary_union(groups[u])) for u in order}
    filled = 0
    if cfg.get("fill_holes", True):
        units, filled = _fill_enclave_holes(units)

    # 2. project (equirectangular about the mid-latitude, same as extrude_map)
    bb = _bbox(list(units.values()))
    k = math.cos(math.radians((bb[1] + bb[3]) / 2.0))
    units = {u: shapely.affinity.affine_transform(g, [k, 0, 0, 1, 0, 0]) for u, g in units.items()}
    parts = {u: sorted(_parts(g), key=lambda p: -p.area) for u, g in units.items()}

    def kept_ids(scale):
        ids = set()
        for u, ps in parts.items():
            for i, p in enumerate(ps):
                pid = f"{u}#{i}"
                if pid in overrides:
                    if overrides[pid]:
                        ids.add(pid)
                elif i == 0 or p.area * scale ** 2 >= island:
                    ids.add(pid)
        return ids

    def fit(x0, y0, x1, y1, bw, bh):
        return min(bw / max(x1 - x0, 1e-12), bh / max(y1 - y0, 1e-12))

    def layout(ids, scale, move, bw, bh):
        geoms = [(pid, parts[pid.rsplit("#", 1)[0]][int(pid.rsplit("#", 1)[1])]) for pid in sorted(ids)]
        clear_u = clear_mm / scale
        blob = unary_union([g.buffer(clear_u / 2) for _, g in geoms])
        comps = sorted(_parts(blob), key=lambda c: -c.area)
        member = {}
        for pid, g in geoms:
            rp = g.representative_point()
            member[pid] = next((ci for ci, c in enumerate(comps) if c.contains(rp)), 0)
        shifts = {0: (0.0, 0.0)}
        comp_geom = {ci: unary_union([g for pid, g in geoms if member[pid] == ci]) for ci in range(len(comps))}
        fixed = comp_geom[0]
        for ci in range(1, len(comps)):
            if move:
                dx, dy = _place_group(fixed, fixed.bounds, comp_geom[ci], clear_u, bw, bh)
            else:
                dx, dy = 0.0, 0.0
            shifts[ci] = (dx, dy)
            fixed = unary_union([fixed, translate(comp_geom[ci], dx, dy)])
        placed = {pid: translate(g, *shifts[member[pid]]) for pid, g in geoms}
        x0, y0, x1, y1 = _bbox(list(placed.values()))
        return placed, member, shifts, fit(x0, y0, x1, y1, bw, bh), (x0, y0, x1, y1)

    move = bool(cfg.get("move_far", True))
    allb = _bbox([p for ps in parts.values() for p in ps])

    def solve(bw, bh):
        scale = fit(*allb, bw, bh)
        ids = kept_ids(scale)
        for _ in range(4):
            res = layout(ids, scale, move, bw, bh)
            new_ids = kept_ids(res[3])
            scale = res[3]
            if new_ids == ids:
                break
            ids = new_ids
        return ids, res

    # a rectangular bed can take the map either way round (turn it in the slicer)
    best = None
    for bw, bh in {(uw, uh), (uh, uw)}:
        ids, res = solve(bw, bh)
        if best is None or res[3] > best[2][3] * (1 + 1e-9):
            best = (bw, bh, res, ids)
    bw, bh, (placed, member, shifts, scale, ext), ids = best
    true_scale = layout(ids, scale, False, bw, bh)[3] if move else scale
    turned = (bw, bh) != (uw, uh)

    # 3. assemble the prepared units (planar, projected degrees) for extrude_map
    prepared = []
    for u in order:
        ps = [placed[f"{u}#{i}"] for i in range(len(parts[u])) if f"{u}#{i}" in placed]
        if ps:
            prepared.append((u, unary_union(ps)))
    S["prepared"] = prepared
    S["scale"] = scale

    # 4. preview geometry in plate mm (y up; the JS flips it)
    x0, y0, x1, y1 = ext

    def to_mm(g):
        return shapely.affinity.affine_transform(g, [scale, 0, 0, scale, -x0 * scale, -y0 * scale])

    def path(g):
        g = to_mm(g).simplify(0.12, preserve_topology=True)
        d = []
        for p in _parts(g):
            for r in [p.exterior, *p.interiors]:
                c = np.asarray(r.coords)
                d.append("M" + "L".join(f"{x:.2f},{y:.2f}" for x, y in c) + "Z")
        return "".join(d)

    moved_groups = {}
    for ci, (dx, dy) in shifts.items():
        if ci and (abs(dx) > 1e-12 or abs(dy) > 1e-12):
            names = sorted({pid.rsplit("#", 1)[0] for pid, c in member.items() if c == ci})
            moved_groups[ci] = {"units": names, "km": round(math.hypot(dx, dy) * KM_PER_DEG, 1),
                                "mm": round(math.hypot(dx, dy) * scale, 1)}
    out_units = []
    for ui, u in enumerate(order):
        ps = []
        for i, p in enumerate(parts[u]):
            pid = f"{u}#{i}"
            kept = pid in placed
            g = placed[pid] if kept else p
            ci = member.get(pid)
            ps.append({"id": pid, "kept": kept, "main": i == 0,
                       "moved": bool(kept and ci in moved_groups),
                       "mm2": round(p.area * scale ** 2, 1),
                       "km2": round(p.area * KM_PER_DEG ** 2, 1),
                       "d": path(g)})
        out_units.append({"name": u, "parts": ps,
                          "mm2": round(sum(placed[f"{u}#{i}"].area for i in range(len(parts[u]))
                                           if f"{u}#{i}" in placed) * scale ** 2, 1)})
    return json.dumps({
        "units": out_units,
        "plate": [round((x1 - x0) * scale, 1), round((y1 - y0) * scale, 1)],
        "target": round(max(x1 - x0, y1 - y0) * scale, 3),
        "bed": [bed_h, bed_w] if turned else [bed_w, bed_h], "turned": turned,
        "scale": round(scale, 3), "true_scale": round(true_scale, 3),
        "gain_pct": round(100 * (scale / true_scale - 1), 1),
        "moved": list(moved_groups.values()),
        "filled_holes": filled,
        "dropped": sum(1 for u in out_units for p in u["parts"] if not p["kept"]),
    })


# ---------------------------------------------------------------- build + verify

def _clear(path):
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))


def build(cfg_json):
    import extrude_map
    import verify_plate

    cfg = json.loads(cfg_json)
    scale = S["scale"]
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", cfg.get("base", "puzzle")).strip("_") or "puzzle"
    outdir = f"{WORK}/out"
    os.makedirs(WORK, exist_ok=True)
    _clear(outdir)

    gj = {"type": "FeatureCollection",
          "features": [{"type": "Feature", "properties": {"shapeName": n}, "geometry": mapping(g)}
                       for n, g in S["prepared"]]}
    src = f"{WORK}/{base}.geojson"
    with open(src, "w") as f:
        json.dump(gj, f)

    smallest = min(p.area for _, g in S["prepared"] for p in _parts(g)) * scale ** 2
    min_area = min(float(cfg.get("min_area", 5)), 0.5 * smallest)
    argv = ["extrude_map.py", "--geojson", src, "--planar",
            "--target", str(cfg.get("target", 175)),
            "--thickness", str(cfg.get("thickness", 1.6)),
            "--label-depth", str(cfg.get("label_depth", 0.6)),
            "--gap", str(cfg.get("gap", 0.6)),
            "--simplify", "0.3",
            "--pre-simplify", f"{0.2 / scale:.6g}",
            "--min-area", f"{min_area:.3g}",
            "--min-glyph", str(cfg.get("min_glyph", 0.7)),
            "--outdir", outdir, "--out-name", base]
    if cfg.get("labels", True):
        argv.append("--labels")
    _log("$ " + " ".join(a if " " not in a else repr(a) for a in argv[1:]) + "\n")

    old_argv, old_out = sys.argv, sys.stdout
    sys.argv, sys.stdout = argv, _JsWriter()
    try:
        extrude_map.main()
    finally:
        sys.argv, sys.stdout = old_argv, old_out

    # verify, with the same checks verify_plate prints on the desktop. Its face_parts
    # unions the STL's float32 triangles, which needs overlay noding; under snap rounding
    # that left hairline splits along shared edges. Instead: group the face's triangles
    # by shared vertices (one group = one physical piece), then rebuild each group's
    # outline from its boundary edges (exact), falling back to a welded union.
    import networkx as nx
    import trimesh

    def face_parts(mesh, z, tol=1e-4):
        v, f = mesh.vertices, mesh.faces
        sel = f[np.all(np.abs(v[f][:, :, 2] - z) < tol, axis=1)]
        g = nx.Graph()
        g.add_edges_from(np.vstack([sel[:, [0, 1]], sel[:, [1, 2]]]).tolist())
        label = {vi: ci for ci, comp in enumerate(nx.connected_components(g)) for vi in comp}
        groups = {}
        for t in sel:
            groups.setdefault(label[t[0]], []).append(t)
        out = []
        for tris in groups.values():
            tris = np.array(tris)
            xy = v[tris][:, :, :2]
            want = 0.5 * np.abs(np.cross(xy[:, 1] - xy[:, 0], xy[:, 2] - xy[:, 0])).sum()
            if want <= 0:
                continue
            e = np.sort(np.vstack([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), axis=1)
            uniq, cnt = np.unique(e, axis=0, return_counts=True)
            poly = shapely.build_area(shapely.multilinestrings(v[uniq[cnt == 1]][:, :, :2]))
            if abs(poly.area - want) > 0.01 * want:
                poly = shapely.union_all([Polygon(t) for t in xy if Polygon(t).area > 0], grid_size=1e-4)
            ps = _parts(poly)
            if len(ps) > 1:    # lobes that meet at a single vertex: still one piece
                ps = _parts(poly.buffer(1e-4, join_style=2))
            if ps:
                out.append(max(ps, key=lambda p: p.area))
        return sorted(out, key=lambda p: -p.area)

    verify_plate.face_parts = face_parts
    _log("\nverifying...\n")
    rows = []
    stl_dir = f"{outdir}/stl"
    meshes = []
    for fn in sorted(os.listdir(stl_dir)):
        m = trimesh.load(f"{stl_dir}/{fn}")
        g, ink = verify_plate.glyph_height(m)
        parts = verify_plate.face_parts(m, m.vertices[:, 2].max())
        rows.append({"tile": fn[:-4], "glyph": round(g, 2), "ink": round(ink, 1),
                     "waist": round(float(verify_plate.waist(parts[0])), 1),
                     "area": round(parts[0].area, 1),
                     "extra": [round(p.area, 1) for p in parts[1:]], "_parts": parts})
        meshes.append(fn)
    pairs = verify_plate.fit_pairs({r["tile"]: unary_union(r["_parts"]) for r in rows})
    for r in rows:
        r.pop("_parts")
    plate = f"{outdir}/{base}_plate.3mf"
    verify_plate.render_back(plate, f"{outdir}/back_preview.png", (12, 12 * 0.75))

    zpath = f"{outdir}/{base}_stl.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in meshes:
            z.write(f"{stl_dir}/{fn}", f"{base}_stl/{fn}")
    report = {
        "tiles": rows,
        "faint": [r["tile"] for r in rows if 0 < r["glyph"] < verify_plate.FAINT],
        "pairs": len(pairs),
        "gap": [round(min(p[0] for p in pairs), 2), round(max(p[0] for p in pairs), 2)] if pairs else None,
        "overlaps": [f"{a}/{b}" for _, a, b, ov in pairs if ov > 1e-6],
        "files": {"plate": plate, "zip": zpath, "geojson": src,
                  "preview": f"{outdir}/{base}_preview.png", "back": f"{outdir}/back_preview.png"},
        "stls": [f"{stl_dir}/{fn}" for fn in meshes],
        "min_area": min_area,
    }
    _log(f"done: {len(rows)} tiles, {len(pairs)} neighbour pairs, overlaps: {report['overlaps'] or 'none'}\n")
    return json.dumps(report)
