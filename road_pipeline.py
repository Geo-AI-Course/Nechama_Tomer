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
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

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

TC_CIRCULARITY_THR = 0.95   # isoperimetric quotient threshold for traffic circles
TC_MAX_RADIUS_M    = 50.0   # max bounding-circle radius (m)

MERGE_PREC      = 0.5    # metres — coordinate rounding for vertex matching
MERGE_ANGLE_TOL = 10.0   # degrees — max deviation from 180° to allow merge

PARALLEL_BEARING_TOL = 20.0  # degrees — bearing similarity for parallel detection
PARALLEL_DETECT_DIST = 15.0  # metres — max lateral distance to examine
PARALLEL_CLOSE_DIST  = 10.0  # metres — threshold for centerline collapse
PARALLEL_CLOSE_FRAC  = 0.50  # fraction of shorter road within close dist

FORK_BEARING_TOL = 30.0  # degrees — arms of a Y-split share bearing within this

SHORT_ROAD_M     = 100.0  # metres — dead-end removal threshold
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
    hdr = f"  {'Part':<44} {'After':>8} {'Removed':>9} {'Merged':>8} {'Δ%':>7}"
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


# ── Geometry helpers ───────────────────────────────────────────────────────────

def _rpt(xy, prec: float = MERGE_PREC):
    return (round(xy[0] / prec) * prec, round(xy[1] / prec) * prec)


def _make_centerline(a: LineString, b: LineString, n: int = 100) -> LineString:
    pts_a = [a.interpolate(i / (n - 1), normalized=True) for i in range(n)]
    pts_b = [b.interpolate(i / (n - 1), normalized=True) for i in range(n)]
    return LineString([((p.x + q.x) / 2, (p.y + q.y) / 2)
                       for p, q in zip(pts_a, pts_b)]).simplify(1.0)


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
        nxt = next(iter(nexts))
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
    gdf = gdf[~remove].copy().reset_index(drop=True)

    # Add class column
    gdf["class"] = gdf["fclass"].map(CLASS_MAP).fillna("Other")

    # Detect traffic circles
    gdf = _detect_traffic_circles(gdf)
    print(f"  class distribution: {dict(gdf['class'].value_counts())}")

    return gdf, n_before


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
    tc_segs: set = set()
    n_circles = 0

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
            for k in loop:
                tc_segs.add(k)
            n_circles += 1

        visited.update(loop)

    if n_circles:
        print(f"  Detected {n_circles:,} traffic circles "
              f"({len(tc_segs):,} segments)")
        gdf = gdf.copy()
        gdf.loc[list(tc_segs), "class"] = "traffic circle"

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


def part2_merge_lines(gdf: gpd.GeoDataFrame):
    df = gdf.reset_index(drop=True).copy()
    total_merged = 0
    passes = 0

    while True:
        df = df.reset_index(drop=True)
        passes += 1

        # Build junction map: rounded_pt → [(seg_idx, "start"|"end"), ...]
        jmap: dict = defaultdict(list)
        for i in range(len(df)):
            c = list(df.iloc[i].geometry.coords)
            jmap[_rpt(c[0])].append((i, "start"))
            jmap[_rpt(c[-1])].append((i, "end"))

        merged_this: set = set()
        new_rows: list   = []

        for pt, endpoints in jmap.items():
            # Unique segments at this junction (dedup, preserve order)
            seen_ids: dict = {}
            for idx, which in endpoints:
                if idx not in seen_ids:
                    seen_ids[idx] = which
            seg_ids = [s for s in seen_ids if s not in merged_this]

            if len(seg_ids) < 2:
                continue

            # Skip junctions involving traffic circles
            if any(df.iloc[s].get("class") == "traffic circle"
                   for s in seg_ids):
                continue

            # Compute toward-junction bearing for each candidate segment
            candidates = [
                (s, seen_ids[s],
                 _bearing_toward_jn(df.iloc[s].geometry, seen_ids[s]))
                for s in seg_ids
            ]

            result = _best_pair(candidates)
            if result is None:
                continue

            i_idx, i_which, j_idx, j_which, diff = result

            # Y intersection: 3 lines, no pair within tolerance → skip
            if diff > MERGE_ANGLE_TOL:
                continue

            # Tunnel compatibility: T-tunnel never merges with non-tunnel
            tun_i = str(df.iloc[i_idx].get("tunnel", "F") or "F").upper() == "T"
            tun_j = str(df.iloc[j_idx].get("tunnel", "F") or "F").upper() == "T"
            if tun_i != tun_j:
                continue

            merged_geom = _merge_geoms(df.iloc[i_idx].geometry, i_which,
                                       df.iloc[j_idx].geometry, j_which)
            if merged_geom is None:
                continue

            # Attribute winner: higher class rank (lower rank number)
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
            print(f"  Converged after {passes} pass(es)")
            break

        survivors = df[~df.index.isin(merged_this)].copy()
        new_gdf   = gpd.GeoDataFrame(new_rows, crs=df.crs, geometry="geometry")
        df        = pd.concat([survivors, new_gdf],
                              ignore_index=True).reset_index(drop=True)
        n_pairs   = len(merged_this) // 2
        total_merged += n_pairs
        print(f"  Pass {passes}: merged {n_pairs:,} pairs "
              f"({len(df):,} features remaining)")

    df = _add_lengths(df)
    return df, total_merged


# ══════════════════════════════════════════════════════════════════════════════
# Part 3 — Parallel roads
# ══════════════════════════════════════════════════════════════════════════════

def _close_fraction(short: LineString, long_: LineString,
                    dist: float, n: int = 80) -> float:
    """Fraction of evenly-spaced sample points on `short` within `dist` of `long_`."""
    count = sum(
        1 for i in range(n)
        if short.interpolate(i / max(n - 1, 1), normalized=True).distance(long_) <= dist
    )
    return count / n


def part3_parallel_roads(gdf: gpd.GeoDataFrame):
    df      = gdf.reset_index(drop=True).copy()
    dropped: set  = set()
    replace: dict = {}       # orig_idx → new row (centerline)

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

            # Rule 1: different ranks — delete lower rank
            if sri != lri:
                dropped.add(si if sri > lri else li)
                continue

            # Rule 2 & 3: same rank — check close fraction
            # Align directions before centerline
            gl_rev = (LineString(list(gl.coords)[::-1])
                      if _angle_diff(_line_bearing(gi), _line_bearing(gl)) > 90
                      else gl)

            frac = _close_fraction(gi, gl, PARALLEL_CLOSE_DIST)
            if frac >= PARALLEL_CLOSE_FRAC:
                # Create centerline; winner attrs from longer road
                try:
                    cl = _make_centerline(gl_rev, gi)
                except Exception:
                    cl = gl
                winner = df.iloc[li].copy()
                winner["geometry"] = cl
                replace[li] = winner
                dropped.add(si)
                dropped.add(li)   # will be replaced, not truly dropped
            else:
                dropped.add(si)   # keep longer, drop shorter

    # Build result
    rows = []
    for i in range(len(df)):
        if i in dropped and i not in replace:
            continue
        rows.append(replace[i] if i in replace else df.iloc[i])

    result = gpd.GeoDataFrame(rows, crs=df.crs,
                              geometry="geometry").reset_index(drop=True)

    # Y-split handling
    result, n_fork = _handle_y_splits(result)
    if n_fork:
        print(f"  Y-split: removed {n_fork:,} fork arms")

    n_removed = len(df) - (len(result) - (len(replace) if replace else 0))
    result = _add_lengths(result)
    return result, max(0, n_removed)


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
        return gdf, 0

    rows = []
    for i in range(len(df)):
        if i in dropped and i not in replaced:
            continue
        rows.append(replaced[i] if i in replaced else df.iloc[i])

    result = gpd.GeoDataFrame(rows, crs=df.crs,
                              geometry="geometry").reset_index(drop=True)
    return result, len(dropped)


# ══════════════════════════════════════════════════════════════════════════════
# Part 4 — Traffic circle connections
# ══════════════════════════════════════════════════════════════════════════════

def part4_traffic_circles(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    df    = gdf.reset_index(drop=True).copy()
    is_tc = df["class"] == "traffic circle"
    tc    = df[is_tc].reset_index(drop=True)
    roads = df[~is_tc].copy().reset_index(drop=True)

    if len(tc) == 0:
        print("  No traffic circles found")
        return df

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

    for comp in components:
        circle_geoms = [tc.iloc[k].geometry for k in comp]
        circle_union = unary_union(circle_geoms)
        centroid     = circle_union.centroid
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
    roads = _add_lengths(roads)
    return roads.reset_index(drop=True)


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

    drop: set = set()
    for i in range(len(df)):
        if df.iloc[i].geometry.length >= SHORT_ROAD_M:
            continue
        c = list(df.iloc[i].geometry.coords)
        s, e = _rpt(c[0]), _rpt(c[-1])
        conn_s = len(ep_segs[s] - {i})
        conn_e = len(ep_segs[e] - {i})
        # Exactly 1 connection point (dead-end on one side)
        if (conn_s > 0) + (conn_e > 0) == 1:
            drop.add(i)

    result = df[~df.index.isin(drop)].reset_index(drop=True)
    result = _add_lengths(result)
    print(f"  Removed {len(drop):,} short dead-end roads (< {SHORT_ROAD_M:.0f} m)")
    return result, len(drop)


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
    gdf, n_initial = part1_preprocess(args.shp, args.crs)
    _record("1. Preprocess (links/busway removed)", n_initial, len(gdf))
    _save_intermediate(gdf, "OSM_roads_preprocess")

    print("\n[Part 2] Merge lines")
    n0 = len(gdf)
    gdf, n_merged = part2_merge_lines(gdf)
    _record("2. Merge lines", n0, len(gdf), n_removed=0, n_merged=n_merged)
    _save_intermediate(gdf, "OSM_roads_merge")

    print("\n[Part 3] Parallel roads")
    n0 = len(gdf)
    gdf, n_removed3 = part3_parallel_roads(gdf)
    _record("3. Parallel roads", n0, len(gdf), n_removed=n_removed3)
    _save_intermediate(gdf, "OSM_roads_merge_paralle")

    print("\n[Part 4] Traffic circle connections")
    n0 = len(gdf)
    gdf = part4_traffic_circles(gdf)
    _record("4. Traffic circles removed", n0, len(gdf))
    _save_intermediate(gdf, "OSM_roads_merge_paralle_circle")

    print("\n[Part 5] Remove short roads")
    n0 = len(gdf)
    gdf, n_removed5 = part5_short_roads(gdf)
    _record("5. Short dead-end roads removed", n0, len(gdf), n_removed=n_removed5)

    print(f"\n[Output] Save -> {args.out}")
    _save_final(gdf, args.out)

    _print_report(n_initial)
    print("  Pipeline complete.\n")


if __name__ == "__main__":
    main()
