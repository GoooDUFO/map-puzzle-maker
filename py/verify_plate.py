#!/usr/bin/env python3
"""Check a built puzzle plate: engraved glyph height, tile waist, extra pieces, and a
mirrored back-face render.

Uses the measurement methods documented in CLAUDE.md:
  * glyphs  -- union the BACK-face (z=min) triangles into an exact polygon; the engraved
               letters are its interior rings. Median ring height = glyph height.
  * waist   -- union the FRONT-face (z=max) triangles (never the back: the letters would
               read as necks), buffer(-w/2) until it splits, and only count a split whose
               second lobe is > 20mm2 (smaller ones are harmless edge spikes).

usage: verify_plate.py out_dir [--plate NAME.3mf] [--fig W H]
"""
import argparse
import glob
import os

import matplotlib
import numpy as np
import trimesh
from shapely.geometry import Polygon
from shapely.ops import unary_union

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

FAINT = 0.70   # mm -- Cambodia/Kep, the smallest glyphs that have printed cleanly
LOBE = 20.0    # mm2 -- a split only counts if the severed lobe is at least this big


def face_parts(mesh, z, tol=1e-4):
    v, f = mesh.vertices, mesh.faces
    tris = [Polygon(v[t][:, :2]) for t in f if np.all(np.abs(v[t][:, 2] - z) < tol)]
    u = unary_union([p for p in tris if p.is_valid and p.area > 0]).buffer(0)
    return sorted(u.geoms if u.geom_type == "MultiPolygon" else [u], key=lambda p: -p.area)


def glyph_height(mesh):
    rings = [Polygon(r) for p in face_parts(mesh, mesh.vertices[:, 2].min()) for r in p.interiors]
    if not rings:
        return 0.0, 0.0
    hs = []
    for r in rings:
        xy = np.array(r.minimum_rotated_rectangle.exterior.coords)
        hs.append(max(np.linalg.norm(np.diff(xy, axis=0), axis=1)[:2]))
    return float(np.median(hs)), sum(r.area for r in rings)


def waist(poly, max_w=6.0):
    for w in np.arange(0.2, max_w + 0.01, 0.1):
        e = poly.buffer(-w / 2)
        if e.is_empty:
            return w
        lobes = sorted((q.buffer(w / 2) for q in (e.geoms if e.geom_type == "MultiPolygon" else [e])),
                       key=lambda p: -p.area)
        if len(lobes) > 1 and lobes[1].area > LOBE:
            return w
    return max_w


def fit_pairs(tiles):
    """(gap, a, b, overlap) for every pair of tiles closer than 1mm. Per-tile simplification
    turns the nominal 0.6mm gap into ~0.30-0.60mm on every plate so far; overlaps are the
    real defect."""
    names, out = list(tiles), []
    for i, a in enumerate(names):
        ax0, ay0, ax1, ay1 = tiles[a].bounds
        for b in names[i + 1:]:
            bx0, by0, bx1, by1 = tiles[b].bounds
            if ax0 > bx1 + 1 or bx0 > ax1 + 1 or ay0 > by1 + 1 or by0 > ay1 + 1:
                continue
            d = tiles[a].distance(tiles[b])
            if d < 1.0:
                out.append((d, a, b, tiles[a].intersection(tiles[b]).area))
    return out


def render_back(plate_path, out_png, figsize):
    scene = trimesh.load(plate_path)
    fig, ax = plt.subplots(figsize=figsize)
    for g in (scene.geometry.values() if hasattr(scene, "geometry") else [scene]):
        v, f = g.vertices, g.faces
        zmin = v[:, 2].min()
        keep = [t for t in f if np.all(v[t][:, 2] < zmin + 0.05)]
        if keep:
            ax.add_collection(matplotlib.collections.PolyCollection(
                [v[t][:, :2] for t in keep], facecolor="#2d7", edgecolor="none"))
    ax.autoscale()
    ax.set_aspect("equal")
    ax.invert_xaxis()          # back face: mirror so the letters read correctly
    ax.axis("off")
    plt.tight_layout()
    plt.savefig(out_png, dpi=150)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir")
    ap.add_argument("--plate", help="plate .3mf inside outdir (default: the only one there)")
    ap.add_argument("--fig", nargs=2, type=float, default=(12, 12), metavar=("W", "H"))
    args = ap.parse_args()

    rows = []
    for path in sorted(glob.glob(os.path.join(args.outdir, "stl", "*.stl"))):
        m = trimesh.load(path)
        g, ink = glyph_height(m)
        parts = face_parts(m, m.vertices[:, 2].max())
        rows.append((os.path.basename(path)[:-4], g, ink, waist(parts[0]), parts))

    w = max(len(r[0]) for r in rows) + 2
    print(f"{'tile':{w}s} {'glyph':>6s} {'ink':>7s} {'waist':>6s} {'area':>8s}")
    for n, g, ink, wst, parts in sorted(rows, key=lambda r: r[1]):
        flag = "  <-- FAINT" if g < FAINT else ""
        print(f"{n:{w}s} {g:6.2f} {ink:7.1f} {wst:6.1f} {parts[0].area:8.1f}{flag}")

    print(f"\nunlabelled: {[r[0] for r in rows if r[1] == 0] or 'none'}")
    lo_g, lo_w = min(rows, key=lambda r: r[1]), min(rows, key=lambda r: r[3])
    print(f"min glyph {lo_g[1]:.2f}mm ({lo_g[0]})   min waist {lo_w[3]:.1f}mm ({lo_w[0]})   "
          f"faint (<{FAINT}mm): {sum(r[1] < FAINT for r in rows)}")
    extras = [(n, p) for n, *_, parts in rows for p in parts[1:]]
    print("extra pieces:" if extras else "extra pieces: none -- every tile is one solid piece")
    for n, p in extras:
        x0, y0, x1, y1 = p.bounds
        print(f"  {n:{w}s} {p.area:6.1f}mm2  {x1 - x0:4.1f} x {y1 - y0:4.1f} mm")

    pairs = fit_pairs({n: unary_union(parts) for n, *_, parts in rows})
    if pairs:
        over = [f"{a}/{b} {ov:.2f}mm2" for _, a, b, ov in pairs if ov > 1e-6]
        print(f"fit: {len(pairs)} neighbour pairs, gap {min(p[0] for p in pairs):.2f}-"
              f"{max(p[0] for p in pairs):.2f}mm, overlaps: {over or 'none'}")

    plate = args.plate or next(iter(glob.glob(os.path.join(args.outdir, "*.3mf"))), None)
    if plate:
        if not os.path.isabs(plate) and not os.path.exists(plate):
            plate = os.path.join(args.outdir, plate)
        out_png = os.path.join(args.outdir, "back_preview.png")
        render_back(plate, out_png, tuple(args.fig))
        print(f"back render -> {out_png}")


if __name__ == "__main__":
    main()
