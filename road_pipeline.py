#!/usr/bin/env python3
"""
OSM Road Network Pipeline — 5-part workflow (prompt_main_new.md)

  Part 1: Preprocess      — reproject, remove links/busway, add class column,
                            detect traffic circles
  Part 2: Merge lines     — intersection-aware iterative segment merging
                            (handles 2-line, T, Y, X/+, and 5+ junction types)
  Part 3: Parallel roads  — collapse parallel/duplicate road segments
  Part 4: Traffic circles — remove circles, extend connecting roads to centroid
  Part 5: Short roads     — remove dead-end stubs < 100 m with exactly 1
                            network connection

Intermediate outputs saved to: intermediate_data/
Final output saved to:         final/  (or --out path)

Usage:
  python road_pipeline.py --shp "example data/roads_OSM_for_example.shp"
  python road_pipeline.py --shp input.shp --crs 32636 --out final/clean.shp
"""

#import libraries
import argparse
import math
import warnings
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points, unary_union, linemerge

warnings.filterwarnings("ignore")

# ── Constants ──────────────────────────────────────────────────────────────────
DEFAULT_CRS = 32636  # UTM Zone 36N

# fclass → class hierarchy
CLASS_MAP = {
    "primary": "Highway",   "motorway": "Highway",   "trunk": "Highway",
    "residential": "Residential", "secondary": "Residential",
    "pedestrian": "Residential",  "tertiary": "Residential",
    "service": "Residential",     "living_street": "Residential",
    "footway": "Paths",  "path": "Paths",  "steps": "Paths",
    "track": "Track",         "track_grade1": "Track", "track_grade2": "Track",
    "track_grade3": "Track",  "track_grade4": "Track", "track_grade5": "Track",
    "bridleway": "Other", "unclassified": "Other", "unknown": "Other",
    "cycleway": "Bike",
}

CLASS_RANK = {
    "Highway": 1, "Residential": 2, "Paths": 3,
    "Track": 4,   "Other": 5,       "Bike": 6,  "traffic circle": 7,
}

TC_CIRCULARITY_THR = 0.90   # isoperimetric quotient threshold for traffic circles
TC_MAX_RADIUS_M    = 50.0   # max bounding-circle radius (m)

MERGE_PREC      = 0.5    # metres — coordinate rounding for vertex matching
MERGE_ANGLE_TOL     = 10.0   # degrees — max deviation from 180° to allow merge
MERGE_ANGLE_TOL_REF = 45.0   # degrees — wider tolerance for same-ref merges

PARALLEL_BEARING_TOL = 20.0  # degrees — bearing similarity for parallel detection
PARALLEL_DETECT_DIST = 15.0  # metres — max lateral distance to examine
PARALLEL_CLOSE_DIST  = 10.0  # metres — lateral threshold for parallel detection
PARALLEL_EXTEND_MAX_M = 50.0  # metres — max snap distance when extending connectors to kept parallel
PARALLEL_SNAP_TO_JN_M = 5.0   # metres — prefer existing junction within this radius of natural snap point

FORK_BEARING_TOL = 30.0  # degrees — arms of a Y-split share bearing within this

TC_MERGE_LOOKAHEAD = 3   # vertices in from the circle centre used to gauge a road's general direction

SHORT_ROAD_M     = 100.0  # metres — dead-end removal threshold
ISOLATED_ROAD_M  = 200.0  # metres — isolated (0-connection) removal threshold
TC_GAP_FRACTION  = 0.125  # max gap / total perimeter for near-complete circle autocompletion
INTERMEDIATE_DIR = "intermediate_data"


# ── Reporting ──────────────────────────────────────────────────────────────────
_REPORT: list = []


def _record(name: str, n_before: int, n_after: int,
            n_removed: int | None = None, n_merged: int = 0) -> None:
    if n_removed is None:
        n_removed = max(0, n_before - n_after)
    pct = (n_after - n_before) / n_before * 100 if n_before else 0.0
    _REPORT.append(dict(name=name, n_before=n_before, n_after=n_after,
                        n_removed=n_removed, n_merged=n_merged, pct=pct))
    sign = "+" if pct > 0 else ""
    print(f"  Remaining: {n_after:,}  |  Removed: {n_removed:,}  |  "
          f"Merged: {n_merged:,}  |  Change: {sign}{pct:.1f}%")


def _print_report(n_initial: int) -> None:
    sep = "=" * 68
    print(f"\n{sep}\n  PIPELINE SUMMARY REPORT\n{sep}")
    n_final = _REPORT[-1]["n_after"] if _REPORT else n_initial
    print(f"  Input features   : {n_initial:,}")
    print(f"  Output features  : {n_final:,}")
    print(f"  Total removed    : {sum(r['n_removed'] for r in _REPORT):,}")
    print(f"  Total merged     : {sum(r['n_merged']  for r in _REPORT):,}\n")
    hdr = f"  {'Part':<44} {'After':>8} {'Removed':>9} {'Merged':>8} {'Chg%':>7}"
    print(hdr)
    print(f"  {'-'*44} {'-'*8} {'-'*9} {'-'*8} {'-'*7}")
    for r in _REPORT:
        s = "+" if r["pct"] > 0 else "-"
        print(f"  {r['name']:<44} {r['n_after']:>8,} "
              f"{r['n_removed']:>9,} {r['n_merged']:>8,} "
              f"{s}{abs(r['pct']):>6.1f}%")
    print(sep)


# ── Bearing helpers ────────────────────────────────────────────────────────────

def _local_bearing(line: LineString, which: str = "end") -> float:
    """Bearing at start or end using the adjacent (second) vertex."""
    c = list(line.coords)
    if len(c) < 2:
        return 0.0
    if which == "end":
        dx, dy = c[-1][0] - c[-2][0], c[-1][1] - c[-2][1]
    else:
        dx, dy = c[1][0] - c[0][0], c[1][1] - c[0][1]
    return math.degrees(math.atan2(dx, dy)) % 360


def _line_bearing(line: LineString) -> float:
    """Overall bearing first → last coord."""
    c = list(line.coords)
    return math.degrees(math.atan2(c[-1][0] - c[0][0], c[-1][1] - c[0][1])) % 360


def _angle_diff(b1: float, b2: float) -> float:
    """Absolute angular difference in [0, 180]."""
    d = abs(b1 - b2) % 360
    return d if d <= 180 else 360 - d


def _parallel(b1: float, b2: float, tol: float) -> bool:
    d = _angle_diff(b1, b2)
    return d <= tol or d >= (180 - tol)


def _bearing_toward_jn(line: LineString, which_end: str) -> float:
    """Bearing of the line as it APPROACHES its junction endpoint.

    Both start and end return a bearing pointing INTO the junction,
    so two lines form a straight-through when their values are ~180° apart.
    """
    if which_end == "end":
        return _local_bearing(line, "end")
    return (_local_bearing(line, "start") + 180) % 360


def _bearing_general(line: LineString, which_end: str,
                     lookahead: int = TC_MERGE_LOOKAHEAD) -> float:
    """Bearing pointing INTO the junction, judged by the road's GENERAL heading.

    Like `_bearing_toward_jn`, but measured from a vertex `lookahead` steps in from
    the junction endpoint (clamped for short lines) rather than the immediately
    adjacent one.  This smooths out the kink where a road bends into a roundabout,
    so two opposite through-road arms read as ~180° apart.
    """
    c = list(line.coords)
    if len(c) < 2:
        return 0.0
    if which_end == "end":
        j = c[-1]
        k = c[max(0, len(c) - 1 - lookahead)]
    else:
        j = c[0]
        k = c[min(len(c) - 1, lookahead)]
    return math.degrees(math.atan2(j[0] - k[0], j[1] - k[1])) % 360


# ── Geometry helpers ───────────────────────────────────────────────────────────

def _rpt(xy, prec: float = MERGE_PREC):
    return (round(xy[0] / prec) * prec, round(xy[1] / prec) * prec)


def _make_centerline(a: LineString, b: LineString, n: int = 100) -> LineString:
    pts_a = [a.interpolate(i / (n - 1), normalized=True) for i in range(n)]
    pts_b = [b.interpolate(i / (n - 1), normalized=True) for i in range(n)]
    return LineString([((p.x + q.x) / 2, (p.y + q.y) / 2)
                       for p, q in zip(pts_a, pts_b)]).simplify(1.0)


def _stitch_chain(geoms: list) -> LineString:
    """Concatenate LineStrings into one, matching rounded endpoints.

    Fallback for when shapely's linemerge cannot join coincident-but-not-exact
    vertices.  Uses the same orientation logic as `_merge_geoms`.
    """
    remaining = list(geoms)
    coords = list(remaining.pop(0).coords)
    changed = True
    while remaining and changed:
        changed = False
        for idx, g in enumerate(remaining):
            gc = list(g.coords)
            if _rpt(coords[-1]) == _rpt(gc[0]):
                coords += gc[1:]
            elif _rpt(coords[-1]) == _rpt(gc[-1]):
                coords += gc[-2::-1]
            elif _rpt(coords[0]) == _rpt(gc[-1]):
                coords = gc[:-1] + coords
            elif _rpt(coords[0]) == _rpt(gc[0]):
                coords = gc[:0:-1] + coords
            else:
                continue
            remaining.pop(idx)
            changed = True
            break
    return LineString(coords)


def _merge_circle(geoms: list) -> LineString:
    """Merge the arc segments of one traffic circle into a single line.

    Prefers shapely's linemerge (handles ordering/direction and yields a
    closed ring for a full loop); falls back to `_stitch_chain` when endpoints
    are coincident only after rounding.
    """
    if len(geoms) == 1:
        return geoms[0]
    merged = linemerge(geoms)
    if merged.geom_type == "LineString":
        return merged
    parts = list(merged.geoms) if merged.geom_type == "MultiLineString" else geoms
    return _stitch_chain(parts)


def _arc_between(center, p_start, p_end, n: int = 24) -> LineString:
    """Circular arc from p_start to p_end around center.

    Sweeps the shortest signed angle (the small remaining gap) and interpolates
    the radius linearly between the two endpoints so the arc meets both free
    ends exactly while continuing the circular curvature.
    """
    cx, cy = center
    a0 = math.atan2(p_start[1] - cy, p_start[0] - cx)
    a1 = math.atan2(p_end[1] - cy, p_end[0] - cx)
    d  = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    r0 = math.hypot(p_start[0] - cx, p_start[1] - cy)
    r1 = math.hypot(p_end[0] - cx, p_end[1] - cy)
    pts = []
    for k in range(n + 1):
        t   = k / n
        ang = a0 + d * t
        r   = r0 + (r1 - r0) * t
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return LineString(pts)


def _add_lengths(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gdf = gdf.copy()
    gdf["length_m"]  = gdf.geometry.length
    gdf["length_km"] = gdf["length_m"] / 1000.0
    return gdf


def _connected_components(n: int, edges: list) -> list:
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in edges:
        pi, pj = find(i), find(j)
        if pi != pj:
            parent[pi] = pj

    comps: dict = defaultdict(list)
    for i in range(n):
        comps[find(i)].append(i)
    return list(comps.values())


def _trace_loop(start: int, adj: dict, ep: dict, max_segs: int = 50):
    s_pt, e_pt = ep[start]
    chain, visited = [start], {start}
    cur, origin = e_pt, s_pt
    for _ in range(max_segs):
        nexts = adj[cur] - visited
        if not nexts:
            return None
        # Among candidates, prefer the one whose far endpoint is closest to
        # origin — this "heads home" and traces the tightest circular path.
        def _far_dist(k):
            ns, ne = ep[k]
            far = ne if ns == cur else ns
            return math.hypot(far[0] - origin[0], far[1] - origin[1])
        nxt = min(nexts, key=_far_dist)
        chain.append(nxt)
        visited.add(nxt)
        ns, ne = ep[nxt]
        cur = ne if ns == cur else ns
        if cur == origin:
            return chain
    return None


# ── Save helpers ───────────────────────────────────────────────────────────────

def _save_intermediate(gdf: gpd.GeoDataFrame, name: str) -> None:
    p = Path(INTERMEDIATE_DIR) / f"{name}.shp"
    p.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(p)
    print(f"  Saved {len(gdf):,} features -> {p}")


def _save_final(gdf: gpd.GeoDataFrame, out_path: str) -> None:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(p)
    print(f"  Saved {len(gdf):,} features -> {p.resolve()}")


def _save_deleted(gdf: gpd.GeoDataFrame, name: str) -> None:
    if gdf is None or len(gdf) == 0:
        print(f"  No features deleted -> {name}.shp skipped")
        return
    p = Path(INTERMEDIATE_DIR) / f"{name}.shp"
    p.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(p)
    print(f"  Saved {len(gdf):,} deleted features -> {p}")


# ══════════════════════════════════════════════════════════════════════════════
# Part 1 — Preprocess
# ══════════════════════════════════════════════════════════════════════════════

def part1_preprocess(path: str, crs: int):
    print(f"  Loading: {path}")
    gdf = gpd.read_file(path)
    print(f"  Loaded {len(gdf):,} features  CRS={gdf.crs}")

    # Reproject
    if gdf.crs is None:
        gdf = gdf.set_crs(crs)
        print(f"  No CRS — set to EPSG:{crs}")
    elif gdf.crs.to_epsg() != crs:
        gdf = gdf.to_crs(crs)
        print(f"  Reprojected -> EPSG:{crs}")

    # Fix geometries
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    gdf.geometry = gdf.geometry.make_valid()
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)
    gdf = gdf[gdf.geom_type == "LineString"].reset_index(drop=True)
    print(f"  After geometry fix: {len(gdf):,} LineStrings")

    # Normalise fclass
    fc_col = "fclass" if "fclass" in gdf.columns else "highway"
    gdf["fclass"] = (gdf[fc_col].astype(str).str.strip().str.lower()
                     .fillna("unclassified"))

    # Ensure ref and tunnel columns exist
    if "ref" not in gdf.columns:
        gdf["ref"] = ""
    gdf["ref"] = gdf["ref"].fillna("").astype(str).str.strip()
    if "tunnel" not in gdf.columns:
        gdf["tunnel"] = "F"
    gdf["tunnel"] = gdf["tunnel"].fillna("F").astype(str).str.strip()

    n_before = len(gdf)

    # Remove fclass containing "link" or equal to "busway"
    mask_link   = gdf["fclass"].str.contains("link", na=False)
    mask_busway = gdf["fclass"] == "busway"
    remove      = mask_link | mask_busway
    print(f"  Removing {int(mask_link.sum()):,} link-type "
          f"+ {int(mask_busway.sum()):,} busway")
    deleted = gdf[remove].copy()
    gdf = gdf[~remove].copy().reset_index(drop=True)

    # Add class column
    gdf["class"] = gdf["fclass"].map(CLASS_MAP).fillna("Other")

    # Detect traffic circles
    gdf = _detect_traffic_circles(gdf)
    print(f"  class distribution: {dict(gdf['class'].value_counts())}")

    return gdf, n_before, deleted


def _detect_traffic_circles(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gdf = gdf.reset_index(drop=True)
    adj: dict = defaultdict(set)
    ep:  dict = {}

    for i in range(len(gdf)):
        c = list(gdf.iloc[i].geometry.coords)
        s, e = _rpt(c[0]), _rpt(c[-1])
        adj[s].add(i)
        adj[e].add(i)
        ep[i] = (s, e)

    visited: set = set()
    circles: list = []   # each: {"members": [seg idx, ...], "close_geom": LineString | None}

    # Pass 1: self-closed single-segment rings (start == end).
    # OSM often stores a complete roundabout as one closed way.
    for i in range(len(gdf)):
        s_pt, e_pt = ep[i]
        if s_pt != e_pt:
            continue
        geom = gdf.iloc[i].geometry
        coords = list(geom.coords)
        if len(coords) < 3:
            continue
        try:
            poly = Polygon(coords)
        except Exception:
            continue
        if poly.area == 0:
            continue
        perim = geom.length
        if perim == 0:
            continue
        Q      = 4 * math.pi * poly.area / (perim ** 2)
        radius = math.sqrt(poly.area / math.pi)
        if Q >= TC_CIRCULARITY_THR and radius <= TC_MAX_RADIUS_M:
            circles.append({"members": [i], "close_geom": None})
            visited.add(i)

    # Pass 2: multi-segment closed loops traced from open-arc endpoints.
    for i in range(len(gdf)):
        if i in visited:
            continue
        s_pt, e_pt = ep[i]
        if len(adj[s_pt]) < 2 or len(adj[e_pt]) < 2:
            continue
        loop = _trace_loop(i, adj, ep)
        if loop is None or len(loop) < 2:
            continue
        if visited.intersection(loop):
            continue

        geoms = [gdf.iloc[k].geometry for k in loop]
        perim = sum(g.length for g in geoms)
        if perim == 0:
            continue

        hull = unary_union(geoms).convex_hull
        if not hasattr(hull, "area") or hull.area == 0:
            continue

        Q      = 4 * math.pi * hull.area / (perim ** 2)
        radius = math.sqrt(hull.area / math.pi)

        if Q >= TC_CIRCULARITY_THR and radius <= TC_MAX_RADIUS_M:
            circles.append({"members": list(loop), "close_geom": None})

        visited.update(loop)

    # Pass 3: Near-complete open arcs missing < TC_GAP_FRACTION of circumference.
    # For each unvisited segment, trace a chain toward the start point using the
    # same "head-home" heuristic as _trace_loop.  When the remaining gap is small
    # enough, check Q and radius on the synthetically completed polygon; if both
    # pass, tag the chain and bridge the gap with a fitted circular arc.
    n_autocompleted = 0
    started: set = set()

    for i in range(len(gdf)):
        if i in visited or i in started:
            continue
        s_pt, e_pt = ep[i]

        chain: list = [i]
        chain_set: set = {i}
        origin = s_pt
        cur = e_pt
        found = False

        for _ in range(50):
            arc_length = sum(gdf.iloc[k].geometry.length for k in chain)
            gap = math.hypot(cur[0] - origin[0], cur[1] - origin[1])
            total = arc_length + gap

            if total > 0 and gap / total < TC_GAP_FRACTION:
                geoms = [gdf.iloc[k].geometry for k in chain]
                close_geom = LineString([cur, origin])
                hull = unary_union(geoms + [close_geom]).convex_hull
                if hasattr(hull, "area") and hull.area > 0:
                    Q = 4 * math.pi * hull.area / (total ** 2)
                    radius = math.sqrt(hull.area / math.pi)
                    if Q >= TC_CIRCULARITY_THR and radius <= TC_MAX_RADIUS_M:
                        found = True
                break  # gap threshold met — no point extending further

            nexts = adj[cur] - chain_set - visited
            if not nexts:
                break

            def _far_dist_arc(k):
                ns, ne = ep[k]
                far = ne if ns == cur else ns
                return math.hypot(far[0] - origin[0], far[1] - origin[1])

            nxt = min(nexts, key=_far_dist_arc)
            chain.append(nxt)
            chain_set.add(nxt)
            ns, ne = ep[nxt]
            cur = ne if ns == cur else ns

        started.update(chain)

        if found:
            visited.update(chain)
            # Fit the circle and bridge the gap with an arc continuing the curve,
            # from the chain's free tip (cur) back to its free start (origin).
            geoms      = [gdf.iloc[k].geometry for k in chain]
            center     = unary_union(geoms).convex_hull.centroid
            origin_xy  = list(gdf.iloc[chain[0]].geometry.coords)[0]
            last_coords = list(gdf.iloc[chain[-1]].geometry.coords)
            cur_xy     = (last_coords[0] if _rpt(last_coords[0]) == cur
                          else last_coords[-1])
            arc = _arc_between((center.x, center.y), cur_xy, origin_xy)
            circles.append({"members": list(chain), "close_geom": arc})
            n_autocompleted += 1

    if circles:
        drop_idx: set = set()
        circle_rows: list = []
        for circ in circles:
            members = circ["members"]
            geoms   = [gdf.iloc[k].geometry for k in members]
            if circ["close_geom"] is not None:
                geoms = geoms + [circ["close_geom"]]
            merged_geom = _merge_circle(geoms)
            row = gdf.iloc[members[0]].copy()
            row["class"]    = "traffic circle"
            row["geometry"] = merged_geom
            circle_rows.append(row)
            drop_idx.update(members)

        print(f"  Detected {len(circles):,} traffic circles "
              f"({len(drop_idx):,} source segments merged, "
              f"{n_autocompleted:,} arc-closed)")

        kept       = gdf[~gdf.index.isin(drop_idx)].copy()
        circle_gdf = gpd.GeoDataFrame(circle_rows, crs=gdf.crs,
                                      geometry="geometry")
        gdf = pd.concat([kept, circle_gdf], ignore_index=True)

    return gdf


# ══════════════════════════════════════════════════════════════════════════════
# Part 2 — Merge lines
# ══════════════════════════════════════════════════════════════════════════════

def _merge_geoms(geom_a: LineString, which_a: str,
                 geom_b: LineString, which_b: str) -> LineString | None:
    """Concatenate two lines sharing a junction endpoint.

    Orient so A ends at junction, B starts at junction.
    """
    ca = list(geom_a.coords)
    cb = list(geom_b.coords)
    if which_a == "start":
        ca = ca[::-1]
    if which_b == "end":
        cb = cb[::-1]
    merged = ca + cb[1:]
    return LineString(merged) if len(merged) >= 2 else None


def _best_pair(candidates: list):
    """Return (idx_i, which_i, idx_j, which_j, diff) for the straightest pair.

    candidates: [(seg_idx, which_end, toward_jn_bearing), ...]
    diff = deviation from 180° (0 = perfectly straight).
    """
    best = None
    best_diff = float("inf")
    n = len(candidates)
    for i in range(n):
        for j in range(i + 1, n):
            diff = abs(_angle_diff(candidates[i][2], candidates[j][2]) - 180)
            if diff < best_diff:
                best_diff = diff
                best = (candidates[i][0], candidates[i][1],
                        candidates[j][0], candidates[j][1], diff)
    return best


def _expand_compound_refs(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Duplicate segments whose ref contains ':' (e.g. '1:6') into one copy
    per road number so each copy can participate in ref-based merging."""
    gdf = gdf.copy().reset_index(drop=True)
    extra = []
    for i in range(len(gdf)):
        r = str(gdf.iloc[i].get("ref", "") or "").strip()
        if ":" not in r:
            continue
        parts = [p.strip() for p in r.split(":") if p.strip()]
        if len(parts) < 2:
            continue
        gdf.at[i, "ref"] = parts[0]
        for part in parts[1:]:
            new_row = gdf.iloc[i].copy()
            new_row["ref"] = part
            extra.append(new_row)
    if extra:
        gdf = pd.concat([gdf, gpd.GeoDataFrame(extra, crs=gdf.crs)],
                        ignore_index=True)
    return gdf


def _merge_pass(gdf: gpd.GeoDataFrame, ref_only: bool = False):
    """Run iterative merge passes until convergence.

    ref_only=True  — Phase 1: only pairs sharing the same non-empty ref,
                     angle tolerance MERGE_ANGLE_TOL_REF (45°).
    ref_only=False — Phase 2: all pairs, angle tolerance MERGE_ANGLE_TOL (10°).
    """
    df = gdf.reset_index(drop=True).copy()
    total_merged = 0
    passes = 0
    deleted_rows: list = []
    angle_tol   = MERGE_ANGLE_TOL_REF if ref_only else MERGE_ANGLE_TOL
    phase_label = "ref" if ref_only else "angle"

    while True:
        df = df.reset_index(drop=True)
        passes += 1

        jmap: dict = defaultdict(list)
        for i in range(len(df)):
            c = list(df.iloc[i].geometry.coords)
            jmap[_rpt(c[0])].append((i, "start"))
            jmap[_rpt(c[-1])].append((i, "end"))

        merged_this: set = set()
        new_rows: list   = []

        for pt, endpoints in jmap.items():
            seen_ids: dict = {}
            for idx, which in endpoints:
                if idx not in seen_ids:
                    seen_ids[idx] = which
            seg_ids = [s for s in seen_ids if s not in merged_this]

            if len(seg_ids) < 2:
                continue

            if any(df.iloc[s].get("class") == "traffic circle"
                   for s in seg_ids):
                continue

            candidates = [
                (s, seen_ids[s],
                 _bearing_toward_jn(df.iloc[s].geometry, seen_ids[s]))
                for s in seg_ids
            ]

            if ref_only:
                # Keep only the largest group of same-ref candidates
                ref_groups: dict = defaultdict(list)
                for cand in candidates:
                    r = str(df.iloc[cand[0]].get("ref", "") or "").strip()
                    if r:
                        ref_groups[r].append(cand)
                same_ref = max(ref_groups.values(), key=len, default=[])
                if len(same_ref) < 2:
                    continue
                candidates = same_ref

            result = _best_pair(candidates)
            if result is None:
                continue

            i_idx, i_which, j_idx, j_which, diff = result

            if diff > angle_tol:
                continue

            tun_i = str(df.iloc[i_idx].get("tunnel", "F") or "F").upper() == "T"
            tun_j = str(df.iloc[j_idx].get("tunnel", "F") or "F").upper() == "T"
            if tun_i != tun_j:
                continue

            merged_geom = _merge_geoms(df.iloc[i_idx].geometry, i_which,
                                       df.iloc[j_idx].geometry, j_which)
            if merged_geom is None:
                continue

            row_i = df.iloc[i_idx]
            row_j = df.iloc[j_idx]
            ri = CLASS_RANK.get(row_i.get("class", "Other"), 5)
            rj = CLASS_RANK.get(row_j.get("class", "Other"), 5)
            winner = row_i.copy() if ri <= rj else row_j.copy()
            winner["geometry"] = merged_geom
            new_rows.append(winner)

            merged_this.add(i_idx)
            merged_this.add(j_idx)

        if not merged_this:
            print(f"  Phase {phase_label}: converged after {passes} pass(es)")
            break

        deleted_rows.append(df[df.index.isin(merged_this)].copy())
        survivors = df[~df.index.isin(merged_this)].copy()
        new_gdf   = gpd.GeoDataFrame(new_rows, crs=df.crs, geometry="geometry")
        df        = pd.concat([survivors, new_gdf],
                              ignore_index=True).reset_index(drop=True)
        n_pairs   = len(merged_this) // 2
        total_merged += n_pairs
        print(f"  Phase {phase_label} pass {passes}: merged {n_pairs:,} pairs "
              f"({len(df):,} features remaining)")

    deleted_gdf = (pd.concat(deleted_rows, ignore_index=True)
                   if deleted_rows else gpd.GeoDataFrame(columns=df.columns, crs=df.crs))
    return df, total_merged, deleted_gdf


def part2_merge_lines(gdf: gpd.GeoDataFrame):
    df = _expand_compound_refs(gdf)
    n_expanded = len(df) - len(gdf)
    if n_expanded:
        print(f"  Expanded {n_expanded:,} compound-ref segment(s) into copies")

    df, merged_ref,   deleted_ref   = _merge_pass(df, ref_only=True)
    df, merged_angle, deleted_angle = _merge_pass(df, ref_only=False)

    total_merged = merged_ref + merged_angle
    all_deleted  = [d for d in (deleted_ref, deleted_angle) if len(d)]
    deleted_gdf  = (pd.concat(all_deleted, ignore_index=True)
                    if all_deleted
                    else gpd.GeoDataFrame(columns=df.columns, crs=df.crs))
    df = _add_lengths(df)
    return df, total_merged, deleted_gdf


# ══════════════════════════════════════════════════════════════════════════════
# Part 3 — Parallel roads
# ══════════════════════════════════════════════════════════════════════════════


def _extend_to_parallel(df: gpd.GeoDataFrame, dropped: set,
                        drop_to_kept: dict) -> None:
    """Extend connectors of dropped roads to the nearest point on their kept parallel.

    Covers all vertices of the dropped road (not just its two endpoints), so
    T-intersections along the dropped road are also re-attached.  For each
    connector endpoint the snap target is the nearest point on the kept road
    geometry; if an existing junction lies within PARALLEL_SNAP_TO_JN_M of
    that point, the connector snaps to the junction instead (X-intersection).
    Extensions beyond PARALLEL_EXTEND_MAX_M are skipped.
    Modifies df geometry in-place.
    """
    # Resolve multi-hop chains (A→B→C where B is also dropped)
    resolved: dict = {}
    for d_idx in drop_to_kept:
        seen: set = set()
        cur = drop_to_kept[d_idx]
        while cur in drop_to_kept and cur not in seen:
            seen.add(cur)
            cur = drop_to_kept[cur]
        resolved[d_idx] = cur

    # Endpoint map: rounded_coord → [(row_idx, "start"|"end"), ...]
    ep_map: dict = defaultdict(list)
    for i in range(len(df)):
        c = list(df.iloc[i].geometry.coords)
        ep_map[_rpt(c[0])].append((i, "start"))
        ep_map[_rpt(c[-1])].append((i, "end"))

    # Spatial index over all unique endpoint locations for junction snapping
    ep_keys = list(ep_map.keys())
    ep_pts  = gpd.GeoDataFrame(
        {"key_idx": range(len(ep_keys))},
        geometry=[Point(k) for k in ep_keys],
        crs=df.crs,
    )
    ep_si = ep_pts.sindex

    for d_idx, k_idx in resolved.items():
        k_geom   = df.iloc[k_idx].geometry
        d_coords = list(df.iloc[d_idx].geometry.coords)

        for raw_pt in d_coords:           # all vertices, including T-junction ones
            ep_key = _rpt(raw_pt)
            if ep_key not in ep_map:
                continue
            pt = Point(raw_pt)

            # Nearest point anywhere along kept road geometry
            _, snap_shapely = nearest_points(pt, k_geom)
            snap_xy   = (snap_shapely.x, snap_shapely.y)
            snap_dist = pt.distance(snap_shapely)

            # Prefer an existing junction within PARALLEL_SNAP_TO_JN_M
            near = list(ep_si.query(snap_shapely.buffer(PARALLEL_SNAP_TO_JN_M)))
            if near:
                best_i = min(near,
                             key=lambda i: snap_shapely.distance(ep_pts.iloc[i].geometry))
                best_pt = ep_pts.iloc[best_i].geometry
                if snap_shapely.distance(best_pt) <= PARALLEL_SNAP_TO_JN_M:
                    snap_xy   = (best_pt.x, best_pt.y)
                    snap_dist = pt.distance(best_pt)

            if snap_dist > PARALLEL_EXTEND_MAX_M:
                continue

            for conn_idx, conn_end in ep_map[ep_key]:
                if conn_idx == d_idx or conn_idx in dropped:
                    continue
                c = list(df.iloc[conn_idx].geometry.coords)
                new_coords = ([snap_xy] + c[1:] if conn_end == "start"
                              else c[:-1] + [snap_xy])
                if len(new_coords) >= 2:
                    df.at[conn_idx, "geometry"] = LineString(new_coords)


def part3_parallel_roads(gdf: gpd.GeoDataFrame):
    df           = gdf.reset_index(drop=True).copy()
    dropped: set = set()
    drop_to_kept: dict = {}

    sindex = df.sindex

    for i in range(len(df)):
        if i in dropped:
            continue
        row_i  = df.iloc[i]
        if row_i.get("class") == "traffic circle":
            continue
        geom_i = row_i.geometry
        b_i    = _line_bearing(geom_i)
        rank_i = CLASS_RANK.get(row_i.get("class", "Other"), 5)

        for j in sindex.query(geom_i.buffer(PARALLEL_DETECT_DIST)):
            if j <= i or j in dropped:
                continue
            row_j  = df.iloc[j]
            if row_j.get("class") == "traffic circle":
                continue
            geom_j = row_j.geometry
            b_j    = _line_bearing(geom_j)
            rank_j = CLASS_RANK.get(row_j.get("class", "Other"), 5)

            # Must be parallel in bearing
            if not _parallel(b_i, b_j, PARALLEL_BEARING_TOL):
                continue

            # Lateral separation at midpoint
            if geom_i.interpolate(0.5, normalized=True).distance(geom_j) \
                    > PARALLEL_DETECT_DIST:
                continue

            # Identify shorter vs longer
            if geom_i.length <= geom_j.length:
                si, sri, gi = i, rank_i, geom_i
                li, lri, gl = j, rank_j, geom_j
            else:
                si, sri, gi = j, rank_j, geom_j
                li, lri, gl = i, rank_i, geom_i

            # Keep the higher-rank road; if same rank, keep the longer one
            if sri < lri:
                dropped.add(li)
                drop_to_kept[li] = si
            else:
                dropped.add(si)
                drop_to_kept[si] = li

    # Extend connectors of dropped roads to their kept parallel
    _extend_to_parallel(df, dropped, drop_to_kept)

    # Build result
    deleted_parallel = df[df.index.isin(dropped)].copy()
    rows = [df.iloc[i] for i in range(len(df)) if i not in dropped]
    result = gpd.GeoDataFrame(rows, crs=df.crs,
                              geometry="geometry").reset_index(drop=True)

    # Y-split handling
    result, n_fork, deleted_forks = _handle_y_splits(result)
    if n_fork:
        print(f"  Y-split: removed {n_fork:,} fork arms")

    # Re-merge segments made collinear by the connector extensions
    result, n_remerge, _ = _merge_pass(result, ref_only=False)
    if n_remerge:
        print(f"  Post-extension merge: {n_remerge:,} additional pair(s) merged")

    deleted_gdf = pd.concat([deleted_parallel, deleted_forks], ignore_index=True)
    n_removed = len(df) - len(result)
    result = _add_lengths(result)
    return result, max(0, n_removed), n_remerge, deleted_gdf


def _handle_y_splits(gdf: gpd.GeoDataFrame):
    """Find fork arms (two segs sharing an endpoint with similar bearings)
    and extend the incoming stem to bridge both arms.
    """
    df = gdf.reset_index(drop=True).copy()

    start_map: dict = defaultdict(list)
    end_map:   dict = defaultdict(list)

    for i in range(len(df)):
        if df.iloc[i].get("class") == "traffic circle":
            continue
        c = list(df.iloc[i].geometry.coords)
        start_map[_rpt(c[0])].append(i)
        end_map[_rpt(c[-1])].append(i)

    dropped:  set  = set()
    replaced: dict = {}

    def _process(ep_map, which_end):
        for pt, segs in ep_map.items():
            if len(segs) < 2:
                continue
            for ii in range(len(segs)):
                for jj in range(ii + 1, len(segs)):
                    ai, aj = segs[ii], segs[jj]
                    if ai in dropped or aj in dropped:
                        continue
                    row_a = df.iloc[ai]
                    row_b = df.iloc[aj]
                    if (row_a.get("class") == "traffic circle" or
                            row_b.get("class") == "traffic circle"):
                        continue

                    # Both arms must leave the fork in similar directions
                    b_a = _bearing_toward_jn(row_a.geometry, which_end)
                    b_b = _bearing_toward_jn(row_b.geometry, which_end)
                    if _angle_diff(b_a, b_b) > FORK_BEARING_TOL:
                        continue

                    # Find an incoming stem from the other side of this fork
                    stem_pool = (end_map.get(pt, []) if which_end == "start"
                                 else start_map.get(pt, []))
                    stems = [s for s in stem_pool
                             if s not in {ai, aj} and s not in dropped]
                    if not stems:
                        continue

                    stem_idx  = max(stems, key=lambda s: df.iloc[s].geometry.length)
                    stem_row  = df.iloc[stem_idx]
                    stem_geom = stem_row.geometry

                    # Target: midpoint between the far ends of the two arms
                    def far_pt(idx):
                        c = list(df.iloc[idx].geometry.coords)
                        return _rpt(c[-1] if which_end == "start" else c[0])

                    fp_a, fp_b = far_pt(ai), far_pt(aj)
                    target = ((fp_a[0] + fp_b[0]) / 2, (fp_a[1] + fp_b[1]) / 2)

                    sc = list(stem_geom.coords)
                    new_coords = (sc + [target] if which_end == "start"
                                  else [target] + sc)
                    new_stem = stem_row.copy()
                    new_stem["geometry"] = LineString(new_coords)
                    replaced[stem_idx] = new_stem
                    dropped.add(ai)
                    dropped.add(aj)

    _process(start_map, "start")
    _process(end_map,   "end")

    if not dropped and not replaced:
        return gdf, 0, gpd.GeoDataFrame(columns=df.columns, crs=df.crs)

    true_removed = dropped - set(replaced.keys())
    deleted_forks = df[df.index.isin(true_removed)].copy()

    rows = []
    for i in range(len(df)):
        if i in dropped and i not in replaced:
            continue
        rows.append(replaced[i] if i in replaced else df.iloc[i])

    result = gpd.GeoDataFrame(rows, crs=df.crs,
                              geometry="geometry").reset_index(drop=True)
    return result, len(dropped), deleted_forks


# ══════════════════════════════════════════════════════════════════════════════
# Part 4 — Traffic circle connections
# ══════════════════════════════════════════════════════════════════════════════

def _merge_through_at_points(df: gpd.GeoDataFrame, centroids: list):
    """Merge straight-through pairs (X / + / T) at the given junction points.

    Used after Part 4 extends connecting roads to a traffic circle's centre: the
    roads now converge on one point like a crossroads.  At each centre point, the
    straightest opposite pair — judged by each road's GENERAL heading
    (`_bearing_general`, ~3 vertices in from the centre) and the 10° rule
    (`MERGE_ANGLE_TOL`) — is merged into one through-road, using the same pairing
    (`_best_pair`), concatenation (`_merge_geoms`), tunnel and class-rank winner
    rules as the Part 2 merge.

    Iterates until no further pair merges, so a 4-way `+` collapses both opposite
    pairs (after the first merge the centre becomes an interior vertex, leaving the
    other two arms to merge next pass) and 5+-arm centres collapse repeatedly.
    """
    df = df.reset_index(drop=True).copy()
    centre_keys = {_rpt(c) for c in centroids}
    total_merged = 0

    while True:
        df = df.reset_index(drop=True)

        jmap: dict = defaultdict(list)
        for i in range(len(df)):
            c = list(df.iloc[i].geometry.coords)
            jmap[_rpt(c[0])].append((i, "start"))
            jmap[_rpt(c[-1])].append((i, "end"))

        merged_this: set = set()
        new_rows: list   = []

        for pt in centre_keys:
            endpoints = jmap.get(pt, [])
            seen_ids: dict = {}
            for idx, which in endpoints:
                if idx not in seen_ids:
                    seen_ids[idx] = which
            seg_ids = [s for s in seen_ids if s not in merged_this]
            if len(seg_ids) < 2:
                continue

            candidates = [
                (s, seen_ids[s],
                 _bearing_general(df.iloc[s].geometry, seen_ids[s]))
                for s in seg_ids
            ]

            result = _best_pair(candidates)
            if result is None:
                continue

            i_idx, i_which, j_idx, j_which, diff = result
            if diff > MERGE_ANGLE_TOL:
                continue

            tun_i = str(df.iloc[i_idx].get("tunnel", "F") or "F").upper() == "T"
            tun_j = str(df.iloc[j_idx].get("tunnel", "F") or "F").upper() == "T"
            if tun_i != tun_j:
                continue

            merged_geom = _merge_geoms(df.iloc[i_idx].geometry, i_which,
                                       df.iloc[j_idx].geometry, j_which)
            if merged_geom is None:
                continue

            row_i = df.iloc[i_idx]
            row_j = df.iloc[j_idx]
            ri = CLASS_RANK.get(row_i.get("class", "Other"), 5)
            rj = CLASS_RANK.get(row_j.get("class", "Other"), 5)
            winner = row_i.copy() if ri <= rj else row_j.copy()
            winner["geometry"] = merged_geom
            new_rows.append(winner)

            merged_this.add(i_idx)
            merged_this.add(j_idx)

        if not merged_this:
            break

        survivors = df[~df.index.isin(merged_this)].copy()
        new_gdf   = gpd.GeoDataFrame(new_rows, crs=df.crs, geometry="geometry")
        df        = pd.concat([survivors, new_gdf],
                              ignore_index=True).reset_index(drop=True)
        total_merged += len(merged_this) // 2

    return df, total_merged


def part4_traffic_circles(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    df    = gdf.reset_index(drop=True).copy()
    is_tc = df["class"] == "traffic circle"
    tc    = df[is_tc].reset_index(drop=True)
    roads = df[~is_tc].copy().reset_index(drop=True)

    if len(tc) == 0:
        print("  No traffic circles found")
        return df, 0, gpd.GeoDataFrame(columns=df.columns, crs=df.crs)

    # Group TC segments into individual circles by connectivity
    adj_tc: dict = defaultdict(set)
    ep_tc:  dict = {}
    for i in range(len(tc)):
        c = list(tc.iloc[i].geometry.coords)
        s, e = _rpt(c[0]), _rpt(c[-1])
        ep_tc[i] = (s, e)
        adj_tc[s].add(i)
        adj_tc[e].add(i)

    edges = [
        (a, b)
        for pts in adj_tc.values()
        for a in pts
        for b in pts
        if a < b
    ]
    components = _connected_components(len(tc), edges)
    print(f"  Processing {len(components):,} traffic circle(s) "
          f"({len(tc):,} segments)")

    roads_sindex = roads.sindex
    centroids: list = []

    for comp in components:
        circle_geoms = [tc.iloc[k].geometry for k in comp]
        circle_union = unary_union(circle_geoms)
        centroid     = circle_union.centroid
        centroids.append((centroid.x, centroid.y))
        buf          = circle_union.buffer(1.0)

        for road_idx in roads_sindex.query(buf):
            road_geom = roads.iloc[road_idx].geometry
            if not road_geom.intersects(buf):
                continue

            c = list(road_geom.coords)
            d_start = Point(c[0]).distance(circle_union)
            d_end   = Point(c[-1]).distance(circle_union)

            if d_start <= d_end:
                new_coords = [(centroid.x, centroid.y)] + c
            else:
                new_coords = c + [(centroid.x, centroid.y)]

            roads.at[road_idx, "geometry"] = LineString(new_coords)

    n_tc = len(tc)
    print(f"  Removed {n_tc:,} traffic circle segments")

    # Merge straight-through pairs (X / + / T) at the circle centres
    roads, n_merged = _merge_through_at_points(roads, centroids)
    if n_merged:
        print(f"  Centre X/+ merge: {n_merged:,} through-road pair(s) merged")

    roads = _add_lengths(roads)
    return roads.reset_index(drop=True), n_merged, tc


# ══════════════════════════════════════════════════════════════════════════════
# Part 5 — Remove short roads
# ══════════════════════════════════════════════════════════════════════════════

def part5_short_roads(gdf: gpd.GeoDataFrame):
    df = gdf.reset_index(drop=True).copy()

    # For each endpoint, count how many OTHER segments share it
    ep_segs: dict = defaultdict(set)
    for i in range(len(df)):
        c = list(df.iloc[i].geometry.coords)
        ep_segs[_rpt(c[0])].add(i)
        ep_segs[_rpt(c[-1])].add(i)

    drop_stub: set = set()
    drop_isolated: set = set()
    for i in range(len(df)):
        length = df.iloc[i].geometry.length
        c = list(df.iloc[i].geometry.coords)
        s, e = _rpt(c[0]), _rpt(c[-1])
        conn_s = len(ep_segs[s] - {i})
        conn_e = len(ep_segs[e] - {i})
        connections = (conn_s > 0) + (conn_e > 0)
        # Rule 1: exactly 1 connection (dead-end stub) AND length < 100 m
        if connections == 1 and length < SHORT_ROAD_M:
            drop_stub.add(i)
        # Rule 2: 0 connections (isolated) AND length < 200 m
        elif connections == 0 and length < ISOLATED_ROAD_M:
            drop_isolated.add(i)

    drop = drop_stub | drop_isolated
    deleted = df[df.index.isin(drop)].copy()
    result = df[~df.index.isin(drop)].reset_index(drop=True)
    result = _add_lengths(result)
    print(f"  Removed {len(drop_stub):,} short dead-end roads (< {SHORT_ROAD_M:.0f} m)")
    print(f"  Removed {len(drop_isolated):,} isolated roads (< {ISOLATED_ROAD_M:.0f} m)")
    return result, len(drop), deleted


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="OSM Road Network Pipeline — 5-part workflow",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--shp", required=True,
                    help="Input shapefile (or any OGR-readable format)")
    ap.add_argument("--crs", type=int, default=DEFAULT_CRS,
                    help="EPSG code for projected CRS")
    ap.add_argument("--out", default="final/OSM_roads_clean.shp",
                    help="Output shapefile path")
    args = ap.parse_args()

    sep = "=" * 68
    print(f"\n{sep}")
    print("  OSM Road Network Pipeline — 5-part workflow")
    print(sep)

    print("\n[Part 1] Preprocess")
    gdf, n_initial, deleted1 = part1_preprocess(args.shp, args.crs)
    _record("1. Preprocess (links/busway removed)", n_initial, len(gdf))
    _save_intermediate(gdf, "OSM_roads_preprocess")
    _save_deleted(deleted1, "deleted_part1")

    print("\n[Part 2] Merge lines")
    n0 = len(gdf)
    gdf, n_merged, deleted2 = part2_merge_lines(gdf)
    _record("2. Merge lines", n0, len(gdf), n_removed=0, n_merged=n_merged)
    _save_intermediate(gdf, "OSM_roads_merge")
    _save_deleted(deleted2, "deleted_part2")

    print("\n[Part 3] Parallel roads")
    n0 = len(gdf)
    gdf, n_removed3, n_remerge3, deleted3 = part3_parallel_roads(gdf)
    _record("3. Parallel roads", n0, len(gdf), n_removed=n_removed3, n_merged=n_remerge3)
    _save_intermediate(gdf, "OSM_roads_merge_paralle")
    _save_deleted(deleted3, "deleted_part3")

    print("\n[Part 4] Traffic circle connections")
    n0 = len(gdf)
    gdf, n_merged4, deleted4 = part4_traffic_circles(gdf)
    _record("4. Traffic circles removed", n0, len(gdf),
            n_removed=max(0, n0 - len(gdf) - n_merged4), n_merged=n_merged4)
    _save_intermediate(gdf, "OSM_roads_merge_paralle_circle")
    _save_deleted(deleted4, "deleted_part4")

    print("\n[Part 5] Remove short roads")
    n0 = len(gdf)
    gdf, n_removed5, deleted5 = part5_short_roads(gdf)
    _record("5. Short dead-end roads removed", n0, len(gdf), n_removed=n_removed5)
    _save_deleted(deleted5, "deleted_part5")

    print(f"\n[Output] Save -> {args.out}")
    _save_final(gdf, args.out)

    _print_report(n_initial)
    print("  Pipeline complete.\n")


if __name__ == "__main__":
    main()
