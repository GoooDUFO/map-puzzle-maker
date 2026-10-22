#!/usr/bin/env python3
"""Turn an ADM-boundary GeoJSON into a multi-piece puzzle plate for 3D printing.

Each region (feature) becomes a separate flat tile. The whole map is scaled to
fit the printer bed, regions are shrunk slightly so they sit apart like a puzzle,
then extruded to a flat thickness. Exports a combined 3MF plate, per-region STLs,
and a preview PNG.

Reusable: point --geojson at any ADM1/ADM2 file and set --name-prop.
"""
import argparse
import re
import json
import math
import os

import numpy as np
import trimesh
from shapely.geometry import shape, Polygon as ShPoly, box
from shapely.ops import unary_union, nearest_points


def short_label(name):
    """Trim long admin names for engraving (e.g. 'Northern Province' -> 'Northern').
    Names may be keyed 'Country/Region' to stay unique continent-wide; the engraved
    text is just the region part."""
    name = name.split("/", 1)[-1]
    name = name.replace("Federal Capital Territory", "FCT")
    name = name.replace(" Autonomous Region", "")   # whole suffix, not just " Region"
    return name.replace(" Province", "").replace(" Region", "").strip()


US_STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "District of Columbia": "DC",
}


def abbreviate(name, length=3):
    """Short form to engrave when the full name would print too small.
    US states use their postal code (Maryland -> MD); multi-word names use
    initials (Los Alamos -> LA); single words use a `length`-letter prefix
    (Sagadahoc -> SAG)."""
    base = short_label(name)
    if base in US_STATE_ABBR:
        return US_STATE_ABBR[base]
    words = [w for w in re.split(r"[\s\-]+", base) if w]
    if len(words) >= 2:
        ini = "".join(w[0].upper() for w in words)
        # longer forms grow the last word, so clashing initials stay distinct
        # (Saint Paul Capisterre / Charlestown -> SPC / SPCh)
        return ini if length <= 3 else ini[:-1] + words[-1][:length - 2].capitalize()
    return base[:length].upper()


def make_abbrev_map(names):
    """Registry of the abbreviations actually engraved on this plate, filled by
    resolve_label() as tiles are built. Only names that really get abbreviated
    have to be unique — pre-assigning every name made two parishes that shared
    initials (Saint Paul Capisterre / Charlestown) both fall back to the full
    name, so the too-small one never got abbreviated at all."""
    return {}


def unique_abbrev(name, abbrev_map):
    used = set(abbrev_map.values())
    for length in range(3, 9):
        ab = abbreviate(name, length)
        if ab not in used:
            return ab
    return short_label(name)


def label_glyph_height(lp):
    """Median engraved glyph height (mm) of a label polygon — the metric used to
    check plates against the accepted floor (Cambodia/Kep printed at 0.70mm)."""
    if lp is None or lp.is_empty:
        return 0.0
    parts = list(lp.geoms) if lp.geom_type == "MultiPolygon" else [lp]
    hs = []
    for p in parts:
        xy = np.array(p.minimum_rotated_rectangle.exterior.coords)
        d = np.linalg.norm(np.diff(xy, axis=0), axis=1)[:2]
        hs.append(float(max(d)))
    return float(np.median(hs)) if hs else 0.0


def resolve_label(name, polys, args, abbrev_map=None):
    """Engraved text for `name`: the full name normally, or its abbreviation when
    the full name's glyphs would fall below --min-glyph."""
    if not args.labels:
        return None
    text = short_label(name)
    if args.min_glyph <= 0 or not polys:
        return text
    largest = max(polys, key=lambda p: p.area)
    h = label_glyph_height(label_polygon(text, largest, args.label_fit, args.label_max_h))
    if h >= args.min_glyph:
        return text
    if abbrev_map is None:
        abbrev_map = {}
    ab = abbrev_map.get(name) or unique_abbrev(name, abbrev_map)
    if ab == text:
        return text
    h2 = label_glyph_height(label_polygon(ab, largest, args.label_fit, args.label_max_h))
    base = short_label(name)
    if h2 < args.min_glyph and " " not in base and "-" not in base:
        # 3 letters still too small (Mexico's Tlaxcala/Morelos): try a 2-letter prefix
        ab2 = base[:2].upper()
        if ab2 not in abbrev_map.values():
            h3 = label_glyph_height(label_polygon(ab2, largest, args.label_fit, args.label_max_h))
            if h3 > h2:
                ab, h2 = ab2, h3
    if h2 <= h:
        return text
    abbrev_map[name] = ab
    print(f"    {name}: full name engraves {h:.2f}mm glyphs -> using \"{ab}\" ({h2:.2f}mm)")
    return ab


def text_to_polygon(text, fontsize=10.0):
    """Convert a text string into a shapely (Multi)Polygon using matplotlib's
    font outlines. Holes in letters (a, o, e, B, ...) handled via even-odd fill."""
    from matplotlib.textpath import TextPath
    from matplotlib.font_manager import FontProperties

    fp = FontProperties(family="DejaVu Sans", weight="bold")
    tp = TextPath((0, 0), text, size=fontsize, prop=fp)
    rings = tp.to_polygons()
    geom = None
    for r in rings:
        if len(r) < 3:
            continue
        p = ShPoly(r)
        if not p.is_valid:
            p = p.buffer(0)
        if p.is_empty:
            continue
        geom = p if geom is None else geom.symmetric_difference(p)  # even-odd -> holes
    return geom


def _segments(text):
    """Break text into atomic pieces that may start a new line, recording the
    separator that precedes each (a space, or '' after a hyphen). Hyphens stay
    attached to the piece before them so 'Provence-Alpes' can wrap as 'Provence-'
    / 'Alpes'."""
    segs = []
    for wi, word in enumerate(text.split(" ")):
        parts = word.split("-")
        for pi, part in enumerate(parts):
            piece = part + ("-" if pi < len(parts) - 1 else "")
            sep = " " if (wi > 0 and pi == 0) else ""
            segs.append((sep, piece))
    return segs


def _join_segs(segs):
    """Render a run of (sep, piece) segments into one line string (no leading space)."""
    s = ""
    for i, (sep, piece) in enumerate(segs):
        s += (sep if i > 0 else "") + piece
    return s


def _balance_lines(words, n_lines):
    """Split text into n_lines roughly equal by character length, allowed to break
    at spaces OR hyphens. `words` may be a pre-split list or a string."""
    text = " ".join(words) if isinstance(words, (list, tuple)) else words
    segs = _segments(text)
    if n_lines <= 1 or len(segs) <= 1:
        return [_join_segs(segs)]
    total = sum(len(p) for _, p in segs)
    target = total / n_lines
    lines, cur, cur_len = [], [], 0
    for i, (sep, piece) in enumerate(segs):
        remaining_incl = len(segs) - i  # segments left including this one
        need = n_lines - len(lines)     # lines still to produce
        # break before this piece if the line is full, or if from here we must go
        # one-segment-per-line to fill all remaining lines
        must_break = remaining_incl <= need
        if cur and need > 1 and (must_break or cur_len + len(piece) > target):
            lines.append(_join_segs(cur))
            cur, cur_len = [], 0
        cur.append((sep, piece))
        cur_len += len(piece)
    if cur:
        lines.append(_join_segs(cur))
    return lines


def multiline_polygon(text, n_lines, fontsize=10.0, line_gap=0.32):
    """Build a centered, multi-line text polygon (line 0 on top). Returns a shapely
    geometry with all lines stacked; each line centered horizontally."""
    from shapely.affinity import translate
    lines = _balance_lines(text.split(), n_lines)
    line_h = fontsize  # nominal line height for spacing
    polys = []
    n = len(lines)
    for li, line in enumerate(lines):
        gp = text_to_polygon(line, fontsize=fontsize)
        if gp is None or gp.is_empty:
            continue
        b = gp.bounds
        cx = (b[0] + b[2]) / 2.0
        # stack downward: top line highest. y offset relative to block center.
        y_off = (n - 1) / 2.0 * line_h * (1 + line_gap) - li * line_h * (1 + line_gap)
        polys.append(translate(gp, -cx, y_off))
    if not polys:
        return None
    out = polys[0]
    for p in polys[1:]:
        out = out.union(p)
    return out


def _pole(poly, nx=26, ny=26):
    """Pole of inaccessibility: interior point with max clearance to the boundary.
    Returns (point, clearance)."""
    minx, miny, maxx, maxy = poly.bounds
    from shapely.geometry import Point
    best, bestd = poly.representative_point(), 0.0
    for i in range(nx):
        for j in range(ny):
            x = minx + (maxx - minx) * (i + 0.5) / nx
            y = miny + (maxy - miny) * (j + 0.5) / ny
            pt = Point(x, y)
            if poly.contains(pt):
                d = pt.distance(poly.boundary)
                if d > bestd:
                    bestd, best = d, pt
    # local refine
    step = max((maxx - minx) / nx, (maxy - miny) / ny)
    cx, cy = best.x, best.y
    for _ in range(4):
        for dx in (-step, 0, step):
            for dy in (-step, 0, step):
                pt = Point(cx + dx, cy + dy)
                if poly.contains(pt):
                    d = pt.distance(poly.boundary)
                    if d > bestd:
                        bestd, cx, cy = d, cx + dx, cy + dy
        step *= 0.5
    return Point(cx, cy), bestd


def _interior_point(poly):
    return _pole(poly)[0]


def smooth_geom(g, r):
    """Round off sharp, frequent edges of a polygon. A morphological close (fills
    concave notches) followed by an open (cuts convex spikes), both with round
    joins, leaves smooth flowing curves while roughly preserving area/position."""
    if r <= 0 or g.is_empty:
        return g
    closed = g.buffer(r, join_style=1, quad_segs=8).buffer(-r, join_style=1, quad_segs=8)
    opened = closed.buffer(-r, join_style=1, quad_segs=8).buffer(r, join_style=1, quad_segs=8)
    if opened.is_empty or not opened.is_valid:
        return g
    return opened


def largest_empty_circle(obstacle, bed_w, bed_h, max_r):
    """Find centre + radius of the largest circle inside [0,bed_w]x[0,bed_h] that
    doesn't touch `obstacle`. Coarse grid search then local refine. Radius capped
    at max_r so the inset stays small relative to the main map."""
    from shapely.geometry import Point
    free = box(0, 0, bed_w, bed_h).difference(obstacle)
    if free.is_empty:
        return None
    best_c, best_r = None, 0.0
    nx, ny = 90, 90
    for i in range(nx):
        for j in range(ny):
            x = bed_w * (i + 0.5) / nx
            y = bed_h * (j + 0.5) / ny
            pt = Point(x, y)
            if free.contains(pt):
                r = pt.distance(free.boundary)
                if r > best_r:
                    best_r, best_c = r, (x, y)
    if best_c is None:
        return None
    # local refine around best
    cx, cy = best_c
    step = bed_w / nx
    for _ in range(3):
        improved = False
        for dx in (-step, 0, step):
            for dy in (-step, 0, step):
                pt = Point(cx + dx, cy + dy)
                if free.contains(pt):
                    r = pt.distance(free.boundary)
                    if r > best_r:
                        best_r, cx, cy, improved = r, cx + dx, cy + dy, True
        step *= 0.5
        if not improved:
            step *= 0.5
    return (cx, cy), min(best_r, max_r)


def nearest_empty_circle(obstacle, bed_w, bed_h, r, near, clearance=2.0):
    """Centre of a radius-r disc as close as possible to `near` -- the region's true
    position on the plate -- that stays on the bed and keeps `clearance` mm from every
    tile. None if a disc that size fits nowhere."""
    from shapely.geometry import Point
    ok = box(r, r, bed_w - r, bed_h - r).difference(obstacle.buffer(r + clearance))
    if ok.is_empty:
        return None
    p = nearest_points(ok, Point(near))[0]
    return (p.x, p.y)


def _long_axis_angle(poly):
    """Angle (degrees) of the longest edge of the minimum rotated rectangle,
    normalised so text never reads upside-down (-90..90)."""
    import numpy as _np
    mrr = poly.minimum_rotated_rectangle
    if mrr.geom_type != "Polygon":
        return 0.0
    c = list(mrr.exterior.coords)[:4]
    best_len, best_ang = 0.0, 0.0
    for k in range(3):
        dx = c[k + 1][0] - c[k][0]
        dy = c[k + 1][1] - c[k][1]
        L = (dx * dx + dy * dy) ** 0.5
        if L > best_len:
            best_len, best_ang = L, math.degrees(math.atan2(dy, dx))
    if best_ang > 90:
        best_ang -= 180
    if best_ang < -90:
        best_ang += 180
    return best_ang


def _place(raw, angle, cpt, scale):
    from shapely.affinity import affine_transform, rotate, translate
    g = affine_transform(raw, [-scale, 0, 0, scale, 0, 0])  # mirror X, scale Y
    g = rotate(g, angle, origin=(0, 0))
    bb = g.bounds
    return translate(g, cpt.x - (bb[0] + bb[2]) / 2.0, cpt.y - (bb[1] + bb[3]) / 2.0)


def _fit_one(raw, footprint, inside, angle, cpt, fit_frac, max_h, clip_tol=0.0):
    """Largest scale at which the placed text sits inside `inside` (allowing up to
    `clip_tol` fraction of its area to spill, for curved shapes). Binary-searches
    the scale. Returns (scale, placed_geom)."""
    b = raw.bounds
    tw, th = b[2] - b[0], b[3] - b[1]
    if tw <= 0 or th <= 0:
        return 0.0, None

    mrr = footprint.minimum_rotated_rectangle
    cc = list(mrr.exterior.coords)
    sides = [((cc[k + 1][0] - cc[k][0]) ** 2 + (cc[k + 1][1] - cc[k][1]) ** 2) ** 0.5 for k in range(2)]
    long_side, short_side = max(sides), min(sides)
    s_hi = min(long_side * fit_frac / tw, short_side * fit_frac / th, max_h / th) * 1.6

    def ok(cand):
        if clip_tol <= 0:
            return inside.contains(cand)
        outside = cand.difference(inside).area
        return outside <= clip_tol * cand.area

    lo, hi, best = 0.0, s_hi, None
    for _ in range(20):
        mid = (lo + hi) / 2.0
        cand = _place(raw, angle, cpt, mid)
        if ok(cand):
            lo, best = mid, cand
        else:
            hi = mid
    return lo, best


def label_polygon(text, footprint, fit_frac, max_h, max_lines=3):
    """Build the 2D engraving polygon for `text` inside `footprint`. Tries 1..N
    line wrappings and keeps whichever yields the *tallest* letters, so labels
    fill the space proportionally instead of shrinking to a tiny single line.
    For thin/curved shapes where strict fitting would be tiny, a relaxed pass
    allows the text to use the curved area (clipped to the tile). Pre-mirrored in
    X to read correctly when the tile is flipped."""
    angle = _long_axis_angle(footprint)
    cpt, clearance = _pole(footprint)
    inside = footprint.buffer(-0.4)
    if inside.is_empty:
        inside = footprint

    n_max = min(max_lines, len(_segments(text)))

    def best_over_lines(clip_tol):
        bg, bh = None, 0.0
        for n_lines in range(1, n_max + 1):
            raw = multiline_polygon(text, n_lines, fontsize=10.0)
            if raw is None or raw.is_empty:
                continue
            scale, geom = _fit_one(raw, footprint, inside, angle, cpt, fit_frac, max_h, clip_tol)
            if geom is None:
                continue
            if scale * 10.0 > bh:
                bh, bg = scale * 10.0, geom
        return bg, bh

    best_geom, best_h = best_over_lines(0.0)

    # rescue only *genuinely tiny* labels (thin/curved slivers where strict fitting
    # collapses to unreadable). Blobby regions keep their clean, fully-contained
    # strict fit — never clipped. Only when strict is tiny AND the relaxed option is
    # much larger do we accept a small clip back to the tile.
    floor_h = min(2.2, 0.4 * clearance)
    if best_h < floor_h:
        relaxed, rh = best_over_lines(0.02)
        if relaxed is not None and rh > best_h * 1.4:
            clipped = relaxed.intersection(inside)
            if not clipped.is_empty:
                best_geom = clipped
    return best_geom


def build_labeled_tile(polys, text, thickness, depth, fit_frac, max_h):
    """Build a multi-part tile mesh with `text` engraved into the bottom of its
    largest part. Uses stacked extrusions instead of 3D boolean ops, so it stays
    robust on complex shapes."""
    polys = list(polys)
    if not polys:
        raise ValueError("no polys")
    largest = max(polys, key=lambda p: p.area)
    text_poly = label_polygon(text, largest, fit_frac, max_h) if text else None

    meshes = []
    for p in polys:
        if text_poly is not None and p is largest:
            bottom_geom = p.difference(text_poly)
            for piece in (bottom_geom.geoms if bottom_geom.geom_type == "MultiPolygon" else [bottom_geom]):
                if piece.area < 0.01 or not piece.is_valid:
                    continue
                m = trimesh.creation.extrude_polygon(piece, height=depth)
                meshes.append(m)
            m_top = trimesh.creation.extrude_polygon(p, height=thickness - depth)
            m_top.apply_translation([0, 0, depth])
            meshes.append(m_top)
        else:
            m = trimesh.creation.extrude_polygon(p, height=thickness)
            meshes.append(m)
    mesh = trimesh.util.concatenate(meshes) if len(meshes) > 1 else meshes[0]
    mesh.merge_vertices()
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    mesh.fix_normals()
    return mesh


def place_flag_grooves(footprint, lines, fw, fh, fit, groove):
    """Scale + centre a flag's traced lines onto `footprint`, buffer them into thin
    grooves, and clip to the tile. Returns the groove (Multi)Polygon to recess into
    the TOP face. `lines` are LineStrings in pixel coords (y-up), flag size fw x fh."""
    from shapely.affinity import scale as sscale, translate
    pole, r = _pole(footprint)
    target_w = fit * 2 * r
    s = target_w / fw
    placed = []
    for ls in lines:
        g = sscale(ls, xfact=s, yfact=s, origin=(0, 0))
        g = translate(g, pole.x - fw * s / 2.0, pole.y - fh * s / 2.0)
        placed.append(g.buffer(groove / 2.0, cap_style=2, join_style=2))
    grooves = unary_union(placed)
    grooves = grooves.intersection(footprint.buffer(-0.8))
    return grooves


def build_two_sided_tile(polys, name_text, flag, thickness, name_depth, flag_depth,
                         fit_frac, max_h, flag_fit, flag_groove):
    """Tile with the region NAME recessed into the bottom (mirrored to read when
    flipped) and a FLAG line-pattern recessed into the top centre. Stacked
    extrusions, no booleans. `flag` = (lines, fw, fh) or None."""
    polys = list(polys)
    if not polys:
        raise ValueError("no polys")
    largest = max(polys, key=lambda p: p.area)
    text_poly = label_polygon(name_text, largest, fit_frac, max_h) if name_text else None
    grooves = None
    if flag is not None:
        lines, fw, fh = flag
        grooves = place_flag_grooves(largest, lines, fw, fh, flag_fit, flag_groove)
        if grooves.is_empty:
            grooves = None

    def parts_of(g):
        return list(g.geoms) if g.geom_type == "MultiPolygon" else [g]

    meshes = []
    for p in polys:
        if p is not largest:
            meshes.append(trimesh.creation.extrude_polygon(p, height=thickness))
            continue
        # bottom layer [0, name_depth] with the name cut out
        bottom = p.difference(text_poly) if text_poly is not None else p
        for piece in parts_of(bottom):
            if piece.area >= 0.01 and piece.is_valid:
                meshes.append(trimesh.creation.extrude_polygon(piece, height=name_depth))
        # solid core [name_depth, thickness - flag_depth]
        core_h = thickness - flag_depth - name_depth
        if core_h > 0:
            mc = trimesh.creation.extrude_polygon(p, height=core_h)
            mc.apply_translation([0, 0, name_depth])
            meshes.append(mc)
        # top layer [thickness - flag_depth, thickness] with flag grooves cut out
        top = p.difference(grooves) if grooves is not None else p
        for piece in parts_of(top):
            if piece.area >= 0.01 and piece.is_valid:
                mt = trimesh.creation.extrude_polygon(piece, height=flag_depth)
                mt.apply_translation([0, 0, thickness - flag_depth])
                meshes.append(mt)
    mesh = trimesh.util.concatenate(meshes) if len(meshes) > 1 else meshes[0]
    mesh.merge_vertices()
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    mesh.fix_normals()
    return mesh


def load_regions(path, name_prop):
    gj = json.load(open(path))
    regions = []
    for f in gj["features"]:
        name = f["properties"].get(name_prop) or f["properties"].get("shapeName") or "region"
        regions.append((name, shape(f["geometry"])))
    return regions


def project(geom, lat0_deg):
    """Equirectangular projection: lon/lat degrees -> local planar units.
    Longitude scaled by cos(mean latitude) so shapes aren't stretched."""
    k = math.cos(math.radians(lat0_deg))
    return trimesh.transformations and _affine(geom, k)


def _affine(geom, k):
    from shapely.affinity import affine_transform
    # x' = k*x, y' = y
    return affine_transform(geom, [k, 0, 0, 1, 0, 0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--geojson", required=True)
    ap.add_argument("--name-prop", default="shapeName")
    ap.add_argument("--target", type=float, default=170.0, help="max footprint dimension in mm")
    ap.add_argument("--thickness", type=float, default=1.6, help="tile height in mm")
    ap.add_argument("--gap", type=float, default=0.6, help="total gap between neighbouring pieces in mm")
    ap.add_argument("--simplify", type=float, default=0.15, help="coastline simplify tolerance in mm")
    ap.add_argument("--min-area", type=float, default=8.0, help="drop disconnected parts smaller than this (mm^2)")
    ap.add_argument("--pre-simplify", type=float, default=0.0,
                    help="coarse simplify in DEGREES applied before projection (handy for very detailed coastlines)")
    ap.add_argument("--close-regions", default="",
                    help="comma-separated region names to morphologically close (merge nearby islands)")
    ap.add_argument("--close-radius", type=float, default=4.0,
                    help="closing radius in mm for --close-regions")
    ap.add_argument("--heavy-simplify-regions", default="",
                    help="comma-separated region names to apply extra simplification to (smooth jagged borders)")
    ap.add_argument("--heavy-simplify", type=float, default=1.5,
                    help="extra simplify tolerance in mm for --heavy-simplify-regions")
    ap.add_argument("--front-flags", default="",
                    help="dir of per-region flag images; trace each into line grooves on the TOP face")
    ap.add_argument("--flag-depth", type=float, default=0.6, help="flag groove depth in mm (top face)")
    ap.add_argument("--flag-groove", type=float, default=0.6, help="flag groove line width in mm")
    ap.add_argument("--flag-fit", type=float, default=1.5,
                    help="flag size as a multiple of the tile's inscribed-circle diameter (>1 crops corners)")
    ap.add_argument("--labels", action="store_true", help="engrave region name into the back of each tile")
    ap.add_argument("--label-depth", type=float, default=0.6, help="engrave depth in mm")
    ap.add_argument("--label-fit", type=float, default=0.8, help="fraction of tile bbox the text may fill")
    ap.add_argument("--label-max-h", type=float, default=14.0, help="max letter height in mm")
    ap.add_argument("--min-glyph", type=float, default=0.70,
                    help="below this engraved glyph height (mm) the tile gets an abbreviation "
                         "instead of the full name; 0.70 = smallest that has printed cleanly")
    ap.add_argument("--circle-regions", default="",
                    help="comma-separated regions to render as a simple circle off to the side "
                         "(excluded from map scaling, e.g. far-flung islands)")
    ap.add_argument("--circle-max-d", type=float, default=42.0, help="max diameter (mm) for circle insets")
    ap.add_argument("--circle-d", default="",
                    help="per-region circle diameter overrides, e.g. 'Ceuta=18,Melilla=18'")
    ap.add_argument("--inset-regions", default="",
                    help="comma-separated regions to place off to the side keeping their real shape "
                         "(each self-projected & scaled, e.g. overseas territories)")
    ap.add_argument("--inset-max-d", type=float, default=40.0, help="max size (mm) for shaped insets")
    ap.add_argument("--smooth-regions", default="",
                    help="comma-separated regions to round off (smooths sharp/frequent coastline edges); "
                         "use 'ALL' to smooth every region")
    ap.add_argument("--smooth-radius", type=float, default=1.2,
                    help="rounding radius in mm for --smooth-regions")
    ap.add_argument("--bed", type=float, default=178.0, help="usable bed size (mm) for placing insets")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--abs-scale", type=float, default=0.0,
                    help="fixed scale in mm per geojson unit (overrides --target fitting); "
                         "e.g. 0.1 with a km-projected file = 1:10,000,000")
    ap.add_argument("--planar", action="store_true",
                    help="geojson is already projected (planar units); skip the cos(lat) projection")
    ap.add_argument("--regions", default="",
                    help="comma-separated subset of region names to OUTPUT. All regions in the file "
                         "still shape the puzzle gaps, so chunk plates stay compatible with neighbours")
    ap.add_argument("--out-name", default="",
                    help="basename for the plate/preview files (default: geojson basename)")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(os.path.join(args.outdir, "stl"), exist_ok=True)

    regions = load_regions(args.geojson, args.name_prop)
    abbrev_map = make_abbrev_map([n for n, _ in regions])

    # coarse simplify in degrees first — important for very detailed coastlines.
    # also drop islets smaller than (tol^2)*10 deg^2; otherwise some sources have
    # tens of thousands of pixel-sized parts that explode geometry ops downstream.
    if args.pre_simplify > 0:
        min_a = (args.pre_simplify ** 2) * 10
        cleaned = []
        for n, g in regions:
            gs = g.simplify(args.pre_simplify, preserve_topology=True)
            if gs.geom_type == "MultiPolygon":
                from shapely.geometry import MultiPolygon
                parts = [p for p in gs.geoms if p.area >= min_a]
                if parts:
                    gs = MultiPolygon(parts) if len(parts) > 1 else parts[0]
            cleaned.append((n, gs))
        n_before = sum(len(g.geoms) if g.geom_type == "MultiPolygon" else 1 for _, g in regions)
        n_after = sum(len(g.geoms) if g.geom_type == "MultiPolygon" else 1 for _, g in cleaned)
        print(f"pre-simplify: {n_before} parts -> {n_after} parts")
        regions = cleaned

    close_set = {s.strip() for s in args.close_regions.split(",") if s.strip()}
    heavy_set = {s.strip() for s in args.heavy_simplify_regions.split(",") if s.strip()}
    circle_set = {s.strip() for s in args.circle_regions.split(",") if s.strip()}
    inset_set = {s.strip() for s in args.inset_regions.split(",") if s.strip()}
    smooth_set = {s.strip() for s in args.smooth_regions.split(",") if s.strip()}
    smooth_all = "ALL" in smooth_set

    # pull side-placed regions out so they don't affect map scaling/layout
    circle_regions = [(n, g) for n, g in regions if n in circle_set]
    inset_regions = [(n, g) for n, g in regions if n in inset_set]
    regions = [(n, g) for n, g in regions if n not in circle_set and n not in inset_set]
    if circle_regions:
        print(f"circle insets: {', '.join(n for n, _ in circle_regions)}")
    if inset_regions:
        print(f"shaped insets: {', '.join(n for n, _ in inset_regions)}")

    if not args.planar:
        # mean latitude for projection (use bounds-only to avoid expensive/fragile union)
        bs = np.array([g.bounds for _, g in regions])
        miny, maxy = bs[:, 1].min(), bs[:, 3].max()
        lat0 = (miny + maxy) / 2.0
        regions = [(n, _affine(g, math.cos(math.radians(lat0)))) for n, g in regions]

    subset = {s.strip() for s in args.regions.split(",") if s.strip()}
    if subset:
        known = {n for n, _ in regions}
        missing = subset - known
        if missing:
            raise SystemExit(f"--regions names not found: {sorted(missing)}")

    # bounds used for the mm placement: subset bounds if chunking, else everything
    bs = np.array([g.bounds for n, g in regions if (not subset or n in subset)])
    minx, miny = bs[:, 0].min(), bs[:, 1].min()
    maxx, maxy = bs[:, 2].max(), bs[:, 3].max()
    w, h = maxx - minx, maxy - miny
    if args.abs_scale > 0:
        scale = args.abs_scale
        print(f"chunk {w:.4f} x {h:.4f} units @ fixed scale {scale} -> {w*scale:.1f} x {h*scale:.1f} mm")
        if max(w, h) * scale > args.target:
            print(f"  ! WARNING: chunk exceeds target {args.target}mm")
    else:
        scale = args.target / max(w, h)
        print(f"island {w:.4f} x {h:.4f} proj-units -> scale {scale:.1f} -> {w*scale:.1f} x {h*scale:.1f} mm")

    from shapely.affinity import affine_transform, scale as sscale, translate

    # when chunking, regions far from this chunk can't influence its puzzle gaps —
    # drop them so the big unions stay fast. 20mm window >> 0.6mm gap.
    if subset:
        pad = 20.0 / scale
        wminx, wminy, wmaxx, wmaxy = minx - pad, miny - pad, maxx + pad, maxy + pad
        kept = []
        for n, g in regions:
            b = g.bounds
            if n in subset or (b[2] >= wminx and b[0] <= wmaxx and b[3] >= wminy and b[1] <= wmaxy):
                kept.append((n, g))
        print(f"chunk mode: {len(kept)}/{len(regions)} regions kept as gap neighbours")
        regions = kept

    def to_mm(g):
        g = affine_transform(g, [scale, 0, 0, scale, -minx * scale, -miny * scale])
        return g

    def to_plate(g):
        """Source (lon/lat or planar) geometry -> plate mm, same transform as the map."""
        if not args.planar:
            g = _affine(g, math.cos(math.radians(lat0)))
        return to_mm(g)

    # PASS 1: pre-compute originals in mm and any processed (closed/simplified) shapes.
    # Closing two neighbours independently can fill the same strait twice; we clip
    # each region against the others' originals + previously-claimed new territory.
    from shapely.validation import make_valid
    originals_mm = {}
    for name, g in regions:
        gm = to_mm(g)
        if args.simplify > 0:
            gm = gm.simplify(args.simplify, preserve_topology=True)
        if not gm.is_valid:
            gm = make_valid(gm)
        originals_mm[name] = gm
    others_union_cache = {}
    def others_union(name):
        if name in others_union_cache:
            return others_union_cache[name]
        try:
            u = unary_union([originals_mm[k] for k in originals_mm if k != name])
        except Exception:
            u = unary_union([originals_mm[k].buffer(0) for k in originals_mm if k != name])
        others_union_cache[name] = u
        return u
    processed = {}
    claimed_extra = None  # geom of water already claimed by previously-processed close regions
    for name, _ in regions:
        g = originals_mm[name]
        if name in close_set and args.close_radius > 0:
            r = args.close_radius
            closed = g.buffer(r, join_style=1).buffer(-r, join_style=1)
            closed = closed.difference(others_union(name))  # don't steal other regions' land
            if claimed_extra is not None and not claimed_extra.is_empty:
                closed = closed.difference(claimed_extra)  # don't double-claim a strait
            new_extra = closed.difference(g)
            claimed_extra = new_extra if claimed_extra is None else unary_union([claimed_extra, new_extra])
            g = closed
            print(f"    closed {name} (radius {r}mm)")
        if name in heavy_set and args.heavy_simplify > 0:
            g = g.simplify(args.heavy_simplify, preserve_topology=True)
            print(f"    heavy-simplified {name} (tol {args.heavy_simplify}mm)")
        if (smooth_all or name in smooth_set) and args.smooth_radius > 0:
            g = smooth_geom(g, args.smooth_radius)
            print(f"    smoothed {name} (radius {args.smooth_radius}mm)")
        processed[name] = g

    # neighbour-aware gap: only shrink each region where another region is nearby.
    # This way an isolated island (no adjacent province) keeps its full size.
    gap_r = args.gap / 2.0
    try:
        all_processed = unary_union(list(processed.values()))
    except Exception:
        all_processed = unary_union([p.buffer(0) for p in processed.values()])

    scene = trimesh.Scene()
    colors = _palette(len(regions))
    summary = []

    # optional front-face flags: map region name -> flag image, traced lazily
    import re as _re
    flag_lookup, flag_cache = {}, {}
    if args.front_flags:
        import glob
        for fp in glob.glob(os.path.join(args.front_flags, "*")):
            if os.path.isdir(fp):
                continue
            key = _re.sub(r"[^a-z0-9]", "", os.path.splitext(os.path.basename(fp))[0].lower())
            flag_lookup[key] = fp

    def get_flag(nm):
        if not args.front_flags:
            return None
        key = _re.sub(r"[^a-z0-9]", "", short_label(nm).lower())
        fp = flag_lookup.get(key)
        if fp is None:
            print(f"    ! no flag image for {nm}")
            return None
        if fp not in flag_cache:
            import flag_engrave
            flag_cache[fp] = flag_engrave.flag_to_lines(fp)
        return flag_cache[fp]

    for i, (name, _) in enumerate(regions):
        if subset and name not in subset:
            continue
        g = processed[name]
        if gap_r > 0:
            try:
                others = all_processed.difference(g)
                if not others.is_empty:
                    g = g.difference(others.buffer(gap_r, join_style=2))
            except Exception:
                from shapely.validation import make_valid as _mv
                others = _mv(all_processed).buffer(0).difference(_mv(g).buffer(0))
                if not others.is_empty:
                    g = _mv(g).buffer(0).difference(others.buffer(gap_r, join_style=2).buffer(0))
        if g.is_empty:
            print(f"  ! {name}: empty after gap shrink, skipped")
            continue
        polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
        polys = [p for p in polys if p.area >= args.min_area]
        if not polys:
            print(f"  ! {name}: all parts below min-area, skipped")
            continue
        text = resolve_label(name, polys, args, abbrev_map)
        try:
            if args.front_flags:
                mesh = build_two_sided_tile(polys, text, get_flag(name), args.thickness,
                                            args.label_depth, args.flag_depth,
                                            args.label_fit, args.label_max_h,
                                            args.flag_fit, args.flag_groove)
            else:
                mesh = build_labeled_tile(polys, text, args.thickness,
                                          args.label_depth, args.label_fit, args.label_max_h)
        except Exception as exc:
            print(f"    ! tile build failed for {name} ({exc}); falling back to plain extrusion")
            ms = [trimesh.creation.extrude_polygon(p, height=args.thickness) for p in polys]
            mesh = trimesh.util.concatenate(ms) if len(ms) > 1 else ms[0]
        mesh.visual.face_colors = colors[i]
        safe = name.replace(" ", "_").replace("/", "-")
        mesh.export(os.path.join(args.outdir, "stl", f"{safe}.stl"))
        scene.add_geometry(mesh, geom_name=name)
        summary.append((name, mesh.bounding_box.extents, len(polys)))

    # circle insets: place each far-flung region as a plain disc in empty bed space
    from shapely.geometry import Point as _Point
    obstacle = all_processed
    circle_d_map = {}
    for tok in args.circle_d.split(","):
        if "=" in tok:
            k, v = tok.split("=", 1)
            circle_d_map[k.strip()] = float(v)
    circle_colors = _palette(max(len(circle_regions), 1))
    for ci, (name, g_src) in enumerate(circle_regions):
        max_d = circle_d_map.get(name, args.circle_max_d)
        # sit the disc as close as possible to where the region really is (e.g. a
        # capital district just offshore from its coast), not merely wherever the
        # biggest gap on the bed happens to be
        true_c = to_plate(g_src).centroid
        spot = nearest_empty_circle(obstacle, args.bed, args.bed, max_d / 2.0,
                                    (true_c.x, true_c.y))
        if spot is not None:
            (cx, cy), r = spot, max_d / 2.0
        else:
            found = largest_empty_circle(obstacle, args.bed, args.bed, max_d / 2.0)
            if found is None:
                print(f"  ! no room for circle inset {name}; skipped")
                continue
            (cx, cy), r = found
        if r < 8:
            print(f"  ! circle inset {name} too small (r={r:.1f}); skipped")
            continue
        disc = _Point(cx, cy).buffer(r, resolution=96)
        text = resolve_label(name, [disc], args, abbrev_map)
        try:
            mesh = build_labeled_tile([disc], text, args.thickness,
                                      args.label_depth, args.label_fit, args.label_max_h)
        except Exception as exc:
            print(f"    ! circle build failed for {name} ({exc})")
            mesh = trimesh.creation.extrude_polygon(disc, height=args.thickness)
        mesh.visual.face_colors = circle_colors[ci]
        safe = name.replace(" ", "_").replace("/", "-")
        mesh.export(os.path.join(args.outdir, "stl", f"{safe}.stl"))
        scene.add_geometry(mesh, geom_name=name)
        summary.append((name + " (circle)", mesh.bounding_box.extents, 1))
        # keep next inset from overlapping this one
        obstacle = unary_union([obstacle, disc.buffer(args.gap)])
        print(f"    placed {name} as circle d={2*r:.1f}mm at ({cx:.0f},{cy:.0f}), "
              f"{math.hypot(cx - true_c.x, cy - true_c.y):.0f}mm from its true position")

    # shaped insets: each far-flung region placed off to the side at its real outline,
    # self-projected (own latitude) and scaled to fit an empty spot on the bed.
    inset_colors = _palette(max(len(inset_regions), 1))
    for ii, (name, g_ll) in enumerate(inset_regions):
        # own equirectangular projection so the territory isn't distorted
        b = g_ll.bounds
        lat0i = (b[1] + b[3]) / 2.0
        gp = _affine(g_ll, math.cos(math.radians(lat0i)))
        if not gp.is_valid:
            gp = gp.buffer(0)
        found = largest_empty_circle(obstacle, args.bed, args.bed, args.inset_max_d / 2.0)
        if found is None:
            print(f"  ! no room for inset {name}; skipped")
            continue
        (cx, cy), r = found
        if r < 7:
            print(f"  ! inset {name} too small (r={r:.1f}); skipped")
            continue
        pb = gp.bounds
        s = (2 * r * 0.92) / max(pb[2] - pb[0], pb[3] - pb[1])
        gm = affine_transform(gp, [s, 0, 0, s, 0, 0])
        gb = gm.bounds
        gm = translate(gm, cx - (gb[0] + gb[2]) / 2.0, cy - (gb[1] + gb[3]) / 2.0)
        if smooth_all or name in smooth_set:
            gm = smooth_geom(gm, args.smooth_radius)
            print(f"    smoothed {name} (radius {args.smooth_radius}mm)")
        # keep parts above a small printable area; merge as a multi-part tile
        parts = list(gm.geoms) if gm.geom_type == "MultiPolygon" else [gm]
        parts = [p for p in parts if p.area >= 4.0]
        if not parts:
            print(f"  ! inset {name} has no printable parts; skipped")
            continue
        text = resolve_label(name, parts, args, abbrev_map)
        try:
            mesh = build_labeled_tile(parts, text, args.thickness,
                                      args.label_depth, args.label_fit, args.label_max_h)
        except Exception as exc:
            print(f"    ! inset build failed for {name} ({exc})")
            ms = [trimesh.creation.extrude_polygon(p, height=args.thickness) for p in parts]
            mesh = trimesh.util.concatenate(ms) if len(ms) > 1 else ms[0]
        mesh.visual.face_colors = inset_colors[ii]
        safe = name.replace(" ", "_").replace("/", "-")
        mesh.export(os.path.join(args.outdir, "stl", f"{safe}.stl"))
        scene.add_geometry(mesh, geom_name=name)
        summary.append((name + " (inset)", mesh.bounding_box.extents, len(parts)))
        obstacle = unary_union([obstacle, unary_union(parts).buffer(args.gap)])
        print(f"    placed {name} as shaped inset ~{2*r:.0f}mm at ({cx:.0f},{cy:.0f})")

    base = args.out_name or os.path.splitext(os.path.basename(args.geojson))[0]
    plate_path = os.path.join(args.outdir, f"{base}_plate.3mf")
    scene.export(plate_path)
    print(f"\nexported {plate_path} with {len(summary)} pieces")
    for name, ext, nparts in summary:
        extra = f" ({nparts} parts)" if nparts > 1 else ""
        print(f"  {name:24s} {ext[0]:6.1f} x {ext[1]:6.1f} x {ext[2]:.1f} mm{extra}")

    _preview(scene, os.path.join(args.outdir, f"{base}_preview.png" if args.out_name else "preview.png"),
             args.target, base)


def _palette(n):
    import colorsys
    out = []
    for i in range(n):
        r, g, b = colorsys.hsv_to_rgb(i / max(n, 1), 0.55, 0.85)
        out.append([int(r * 255), int(g * 255), int(b * 255), 255])
    return out


def _preview(scene, path, target, title="map"):
    # top-down orthographic-ish preview via matplotlib (no GL needed)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Polygon as MplPoly
        from matplotlib.collections import PatchCollection
    except Exception as e:
        print("preview skipped (matplotlib missing):", e)
        return
    fig, ax = plt.subplots(figsize=(6, 6))
    for name, geom in scene.geometry.items():
        # project mesh footprint to XY outline
        v = geom.vertices
        base = v[np.isclose(v[:, 2], v[:, 2].min())][:, :2]
        if len(base) >= 3:
            try:
                from scipy.spatial import ConvexHull  # noqa
            except Exception:
                pass
        col = (np.array(geom.visual.face_colors[0][:3]) / 255.0).tolist()
        # draw triangles for accurate shape
        tris = v[geom.faces][:, :, :2]
        patches = [MplPoly(t, closed=True) for t in tris]
        pc = PatchCollection(patches, facecolor=col, edgecolor="none", linewidth=0)
        ax.add_collection(pc)
    ax.set_aspect("equal")
    ax.autoscale_view()
    ax.set_xlim(-5, target + 5)
    ax.set_title(f"{title} — print plate (mm)")
    ax.set_xlabel("mm")
    fig.savefig(path, dpi=130, bbox_inches="tight")
    print("preview ->", path)


if __name__ == "__main__":
    main()
