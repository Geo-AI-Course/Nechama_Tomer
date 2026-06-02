#!/usr/bin/env python3
"""
OSM Road Network Cleanup and Consolidation Pipeline.

9-step workflow (prompt.md):
  1. Remove unwanted classes      — fclass contains "link", fclass="busway"
  2. Flag major roads             — ref-bearing features marked as protected
  3. Single-direction             — collapse dual carriageways; tunnels separate
  4. Merge touching segments      — linemerge by group+ref+name+tunnel
  5. Connect likely continuations — gap bridging via directional analysis
  6. Junction handling            — prefer straight-through at multi-way junctions
     (implemented together with step 5)
  7. Remove redundant paths       — footway/path/cycleway within 50m of roads
  8. Handle roundabouts           — detect circular loops, create through-roads
  9. Remove short service roads   — service < 200m (skip if ref-protected)

fclass grouping → output fclass:
  highway     : primary, motorway, trunk          → primary
  residential : residential, secondary, pedestrian,
                tertiary, service, living_street  → residential
  paths       : footway, path, steps              → path
  track       : track, track_grade1..5            → track
  other       : bridleway, cycleway, unclassified,
                unknown                           → unclassified

Output always preserves: ref, tunnel.

Usage
-----
  python road_pipeline.py --shp roads_OSM/roads_OSM.shp
  python road_pipeline.py --shp roads_OSM/roads_OSM.shp --out cleaned.shp \\
      --pair-dist 40 --angle-tol 30
"""

import argparse
import math
import warnings
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge, unary_union

warnings.filterwarnings("ignore")

# ── Constants ──────────────────────────────────────────────────────────────────
ITM_EPSG = 2039

# fclass → representative output fclass after group-based merging
_FCLASS_GROUPS = {
    "primary":      ["primary", "motorway", "trunk"],
    "residential":  ["residential", "secondary", "pedestrian",
                     "tertiary", "service", "living_street"],
    "path":         ["footway", "path", "steps"],
    "track":        ["track", "track_grade1", "track_grade2",
                     "track_grade3", "track_grade4", "track_grade5"],
    "unclassified": ["bridleway", "cycleway", "unclassified", "unknown"],
}
FCLASS_OUTPUT = {fc: out
                 for out, members in _FCLASS_GROUPS.items()
                 for fc in members}

# Original fclasses that are "path-like" (for step 7)
PATH_ORIG_FCLASSES = {"footway", "path", "steps", "cycleway", "bridleway"}

# Corridor-collapse applies to these original fclasses
CLUSTER_FCLASSES = {"trunk", "motorway", "primary"}
CLUSTER_DIST     = 50.0    # metres
LANES_KEEP_DUAL_THRESHOLD = 3

# Gap-bridge parameters (steps 5/6)
GAP_BRIDGE_DIST = 15.0    # metres
GAP_ANGLE_TOL   = 20.0    # degrees

# Step 7
REDUNDANT_PATH_DIST = 50.0    # metres

# Step 8
ROUNDABOUT_CIRC_THR  = 0.70   # isoperimetric circularity (1 = perfect circle)
ROUNDABOUT_MAX_PERIM = 600.0  # metres

# Step 9
SHORT_SERVICE_M = 200.0    # metres


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


def print_final_report(n_initial: int) -> None:
    sep = "=" * 68
    print(f"\n{sep}")
    print("  PIPELINE SUMMARY REPORT")
    print(sep)
    n_final = _REPORT[-1]["n_after"] if _REPORT else n_initial
    total_removed = sum(r["n_removed"] for r in _REPORT)
    total_merged  = sum(r["n_merged"]  for r in _REPORT)
    largest = min((r for r in _REPORT), key=lambda r: r["pct"], default=None)
    print(f"  Input features   : {n_initial:,}")
    print(f"  Output features  : {n_final:,}")
    print(f"  Total removed    : {total_removed:,}")
    print(f"  Total merged     : {total_merged:,}")
    if largest:
        print(f"  Largest reduction: '{largest['name']}'  "
              f"({largest['pct']:+.1f}%)")
    print()
    hdr = f"  {'Step':<44} {'After':>8} {'Removed':>9} {'Merged':>8} {'Δ%':>7}"
    print(hdr)
    print(f"  {'-'*44} {'-'*8} {'-'*9} {'-'*8} {'-'*7}")
    for r in _REPORT:
        s = "+" if r["pct"] > 0 else "-"
        print(f"  {r['name']:<44} {r['n_after']:>8,} "
              f"{r['n_removed']:>9,} {r['n_merged']:>8,} "
              f"{s}{abs(r['pct']):>6.1f}%")
    print(sep)


# ── Attribute helpers ──────────────────────────────────────────────────────────

def parse_lanes(val):
    if pd.isna(val) or val is None:
        return None
    try:
        return int(float(str(val).strip().split(";")[0].split("|")[0]))
    except (ValueError, TypeError):
        return None


def parse_oneway(val):
    """
    Return True  if oneway forward  (yes / 1 / true / F in Geofabrik format).
    Return -1    if oneway reverse  (-1 / reverse).
    Return False otherwise           (bidirectional / B in Geofabrik format).
    """
    if pd.isna(val) or val is None:
        return False
    s = str(val).strip().lower()
    if s in ("yes", "1", "true", "f"):   # 'f' = forward in Geofabrik exports
        return True
    if s in ("-1", "reverse"):
        return -1
    return False                          # 'b' = bidirectional


def line_bearing(line: LineString) -> float:
    """Overall bearing: first coord → last coord, degrees [0, 360)."""
    c = list(line.coords)
    return math.degrees(math.atan2(c[-1][0] - c[0][0], c[-1][1] - c[0][1])) % 360


def local_bearing(line: LineString, which: str = "end") -> float:
    """Local bearing at the start or end of a line (last two / first two coords)."""
    c = list(line.coords)
    if which == "end" and len(c) >= 2:
        dx, dy = c[-1][0] - c[-2][0], c[-1][1] - c[-2][1]
    elif len(c) >= 2:
        dx, dy = c[1][0] - c[0][0], c[1][1] - c[0][1]
    else:
        dx, dy = 0.0, 1.0
    return math.degrees(math.atan2(dx, dy)) % 360


def opposite_bearings(b1: float, b2: float, tol: float) -> bool:
    return abs(abs(b1 - b2) % 360 - 180) <= tol


def same_bearings(b1: float, b2: float, tol: float) -> bool:
    diff = abs(b1 - b2) % 360
    return diff <= tol or diff >= (360 - tol)


def parallel_bearings(b1: float, b2: float, tol: float) -> bool:
    return same_bearings(b1, b2, tol) or opposite_bearings(b1, b2, tol)


def _connected_components(n_nodes: int, edges: list) -> list:
    parent = list(range(n_nodes))

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
    for i in range(n_nodes):
        comps[find(i)].append(i)
    return list(comps.values())


# ── Geometry helpers ───────────────────────────────────────────────────────────

def corridor_centerline(lines: list, max_cross_dist: float, n: int = 200) -> LineString:
    if len(lines) == 1:
        return lines[0]
    main   = max(lines, key=lambda l: l.length)
    others = [l for l in lines if l is not main]
    pts_out = []
    for i in range(n):
        pt           = main.interpolate(i / (n - 1), normalized=True)
        contributors = [pt]
        for other in others:
            if pt.distance(other) <= max_cross_dist:
                nearest = other.interpolate(other.project(pt))
                contributors.append(nearest)
        x = sum(p.x for p in contributors) / len(contributors)
        y = sum(p.y for p in contributors) / len(contributors)
        pts_out.append((x, y))
    return LineString(pts_out).simplify(1.0)


def make_centerline(line_a: LineString, line_b: LineString, n: int = 100) -> LineString:
    pts_a = [line_a.interpolate(i / (n - 1), normalized=True) for i in range(n)]
    pts_b = [line_b.interpolate(i / (n - 1), normalized=True) for i in range(n)]
    coords = [((a.x + b.x) / 2, (a.y + b.y) / 2) for a, b in zip(pts_a, pts_b)]
    return LineString(coords).simplify(1.0)


# ── Roundabout loop-tracing helper ─────────────────────────────────────────────

def _trace_loop(start_seg: int, adj: dict, seg_ep: dict,
                max_segs: int = 40) -> list | None:
    """Trace a closed chain of segments beginning at start_seg.
    Returns the ordered list of segment positional indices if a loop is found,
    or None if the chain reaches a dead-end or exceeds max_segs."""
    s_pt, e_pt = seg_ep[start_seg]
    chain   = [start_seg]
    visited = {start_seg}
    cur_pt  = e_pt
    start_pt = s_pt

    for _ in range(max_segs):
        nexts = adj[cur_pt] - visited
        if not nexts:
            return None
        nxt = next(iter(nexts))
        chain.append(nxt)
        visited.add(nxt)
        ns, ne = seg_ep[nxt]
        cur_pt = ne if ns == cur_pt else ns
        if cur_pt == start_pt:
            return chain
    return None


# ── Load & pre-process ─────────────────────────────────────────────────────────

def load_and_prep(path: str) -> gpd.GeoDataFrame:
    print(f"  Loading: {path}")
    gdf = gpd.read_file(path)
    print(f"  Loaded {len(gdf):,} features  CRS={gdf.crs}")

    if gdf.crs is None:
        gdf = gdf.set_crs(ITM_EPSG)
        print(f"  No CRS — assumed EPSG:{ITM_EPSG}")
    elif gdf.crs.to_epsg() != ITM_EPSG:
        gdf = gdf.to_crs(ITM_EPSG)
        print(f"  Reprojected → EPSG:{ITM_EPSG}")

    # Fix geometries
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    gdf.geometry = gdf.geometry.make_valid()
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)
    gdf = gdf[gdf.geom_type == "LineString"].reset_index(drop=True)
    print(f"  After geometry fix: {len(gdf):,} LineStrings")

    # Normalise fclass / highway
    fc_col = "fclass" if "fclass" in gdf.columns else "highway"
    gdf["_hw"]      = gdf[fc_col].astype(str).str.strip().str.lower().fillna("unclassified")
    gdf["_orig_hw"] = gdf["_hw"].copy()   # preserved for steps 7, 9

    # Normalise oneway
    gdf["_oneway"] = (gdf["oneway"].apply(parse_oneway)
                      if "oneway" in gdf.columns else False)
    gdf["_lanes"]  = (gdf["lanes"].apply(parse_lanes)
                      if "lanes"  in gdf.columns else None)

    # Tunnel flag
    if "tunnel" not in gdf.columns:
        gdf["tunnel"] = "F"
    gdf["_is_tunnel"] = gdf["tunnel"].astype(str).str.strip().str.upper().eq("T")

    # Ref attribute
    if "ref" not in gdf.columns:
        gdf["ref"] = ""
    gdf["ref"] = gdf["ref"].fillna("").astype(str).str.strip()

    # Protected flag (set in step 2)
    gdf["_protected"] = False

    print(f"  one-way fwd={int((gdf['_oneway'] == True).sum()):,}  "
          f"tunnel={gdf['_is_tunnel'].sum():,}  "
          f"types={gdf['_hw'].nunique()}")
    return gdf


# ── Internal collapse helpers (used in step 3) ─────────────────────────────────

def _classify_dual(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Tag _keep_dual for non-corridor roads."""
    def _keep(row):
        if row["_hw"] in CLUSTER_FCLASSES:
            return False
        lanes = row["_lanes"]
        if lanes is not None:
            lpd = lanes if row["_oneway"] else lanes / 2
            if lpd > LANES_KEEP_DUAL_THRESHOLD:
                return True
        if (row.get("dual_carriageway") == "yes" or row.get("divided") == "yes"):
            return True
        return False

    gdf = gdf.copy()
    gdf["_keep_dual"] = gdf.apply(_keep, axis=1)
    return gdf


def _corridor_collapse(gdf: gpd.GeoDataFrame,
                       cluster_dist: float, angle_tol: float) -> gpd.GeoDataFrame:
    """Cluster & centerline trunk/motorway/primary segments."""
    is_major = gdf["_hw"].isin(CLUSTER_FCLASSES)
    major    = gdf[is_major].reset_index(drop=True)
    rest     = gdf[~is_major].copy()

    if len(major) == 0:
        return gdf

    print(f"    Clustering {len(major):,} major segments (±{cluster_dist} m) ...")
    sindex = major.sindex
    edges  = []
    for i in range(len(major)):
        b = line_bearing(major.iloc[i].geometry)
        for j in list(sindex.query(major.iloc[i].geometry.buffer(cluster_dist))):
            if j <= i:
                continue
            if parallel_bearings(b, line_bearing(major.iloc[j].geometry), angle_tol):
                edges.append((i, j))

    components = _connected_components(len(major), edges)
    new_rows = []
    for comp in components:
        lines = [major.iloc[k].geometry for k in comp]
        try:
            cl = corridor_centerline(lines, cluster_dist)
        except Exception:
            cl = lines[0]
        rep = major.iloc[comp[0]].copy()
        rep["geometry"] = cl
        rep["_oneway"]  = False
        new_rows.append(rep)

    result_major = gpd.GeoDataFrame(new_rows, crs=gdf.crs,
                                    geometry="geometry").reset_index(drop=True)
    n_collapsed = len(major) - len(result_major)
    print(f"    → {len(result_major):,} corridor centerlines  "
          f"(collapsed {n_collapsed:,})")
    return pd.concat([rest, result_major], ignore_index=True)


def _carriageway_collapse(gdf: gpd.GeoDataFrame,
                           pair_dist: float, angle_tol: float) -> gpd.GeoDataFrame:
    """Pair opposite-direction one-way segments into centerlines."""
    mask = (~gdf["_keep_dual"]
            & (gdf["_oneway"] == True)
            & ~gdf["_hw"].isin(CLUSTER_FCLASSES))
    coll = gdf[mask].reset_index(drop=True)
    rest = gdf[~mask].copy()

    if len(coll) == 0:
        return gdf

    print(f"    Pairing {len(coll):,} one-way collapse candidates ...")
    has_ref  = "ref"  in coll.columns
    has_name = "name" in coll.columns
    sindex   = coll.sindex
    used     = set()
    pairs    = []

    for i in range(len(coll)):
        if i in used:
            continue
        row  = coll.iloc[i]
        geom = row.geometry
        b    = line_bearing(geom)
        best_j, best_d = None, float("inf")
        for j in list(sindex.query(geom.buffer(pair_dist))):
            if j == i or j in used:
                continue
            other = coll.iloc[j]
            if other["_hw"] != row["_hw"]:
                continue
            if not opposite_bearings(b, line_bearing(other.geometry), angle_tol):
                continue
            if has_ref:
                ra = str(row.get("ref") or "").strip()
                rb = str(other.get("ref") or "").strip()
                if ra and rb and ra != rb:
                    continue
            if has_name:
                na = str(row.get("name") or "").strip()
                nb = str(other.get("name") or "").strip()
                if na and nb and na != nb:
                    continue
            ratio = geom.length / max(other.geometry.length, 1e-9)
            if not (0.2 < ratio < 5.0):
                continue
            d = geom.distance(other.geometry)
            if d < best_d:
                best_d, best_j = d, j

        if best_j is not None:
            pairs.append((i, best_j))
            used.add(i)
            used.add(best_j)

    print(f"    Paired {len(pairs):,} opposite-direction pairs")
    new_rows = []
    for i, j in pairs:
        row_a, row_b = coll.iloc[i], coll.iloc[j]
        line_b = row_b.geometry
        if opposite_bearings(line_bearing(row_a.geometry), line_bearing(line_b), angle_tol):
            line_b = LineString(list(line_b.coords)[::-1])
        try:
            cl = make_centerline(row_a.geometry, line_b)
        except Exception:
            cl = row_a.geometry
        new = row_a.copy()
        new["geometry"] = cl
        new["_oneway"]  = False
        new_rows.append(new)

    unpaired = coll.iloc[[k for k in range(len(coll)) if k not in used]]
    parts = [rest]
    if new_rows:
        parts.append(gpd.GeoDataFrame(new_rows, crs=gdf.crs,
                                       geometry="geometry").reset_index(drop=True))
    if len(unpaired):
        parts.append(unpaired)
    return pd.concat(parts, ignore_index=True)


# ── Step 1 ─────────────────────────────────────────────────────────────────────

def step1_remove_unwanted(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Remove features where fclass contains 'link' or fclass = 'busway'."""
    mask_link   = gdf["_hw"].str.contains("link", na=False)
    mask_busway = gdf["_hw"] == "busway"
    remove      = mask_link | mask_busway
    n_link, n_bus = int(mask_link.sum()), int(mask_busway.sum())
    print(f"  Removed {n_link:,} link-type  +  {n_bus:,} busway features")
    return gdf[~remove].copy().reset_index(drop=True)


# ── Step 2 ─────────────────────────────────────────────────────────────────────

def step2_flag_major(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Flag ref-bearing features as protected (not removed by later steps)."""
    has_ref = gdf["ref"].ne("") & gdf["ref"].notna()
    gdf = gdf.copy()
    gdf["_protected"] = has_ref
    print(f"  {int(has_ref.sum()):,} features carry a ref tag → protected")
    return gdf


# ── Step 3 ─────────────────────────────────────────────────────────────────────

def step3_single_direction(gdf: gpd.GeoDataFrame,
                           cluster_dist: float = CLUSTER_DIST,
                           pair_dist:    float = 35.0,
                           angle_tol:   float = 25.0) -> gpd.GeoDataFrame:
    """Collapse dual carriageways.  Tunnels processed separately."""
    tunnels = gdf[gdf["_is_tunnel"]].copy().reset_index(drop=True)
    surface = gdf[~gdf["_is_tunnel"]].copy().reset_index(drop=True)
    print(f"  Surface: {len(surface):,}   Tunnels: {len(tunnels):,}")

    def _collapse(sub: gpd.GeoDataFrame, label: str) -> gpd.GeoDataFrame:
        if len(sub) == 0:
            return sub
        sub = _classify_dual(sub)
        print(f"  [{label}] corridor collapse ...")
        sub = _corridor_collapse(sub, cluster_dist, angle_tol)
        print(f"  [{label}] carriageway collapse ...")
        sub = _carriageway_collapse(sub, pair_dist, angle_tol)
        return sub

    surface_out = _collapse(surface, "surface")
    tunnel_out  = _collapse(tunnels, "tunnel")

    result = pd.concat([surface_out, tunnel_out], ignore_index=True)

    # Apply fclass group remapping now that collapse is done
    result["_hw"] = result["_orig_hw"].map(FCLASS_OUTPUT).fillna(result["_orig_hw"])

    return result


# ── Step 4 ─────────────────────────────────────────────────────────────────────

def step4_merge_touching(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Merge touching segments sharing the same road identity.
    Tunnels are never merged with surface roads (tunnel in key)."""
    gdf = gdf.copy()

    def _key(row):
        hw  = row["_hw"]
        ref = str(row.get("ref")  or "")  if "ref"  in gdf.columns else ""
        nm  = str(row.get("name") or "")  if "name" in gdf.columns else ""
        tun = "T" if row.get("_is_tunnel") else "F"
        return f"{hw}\x00{ref}\x00{nm}\x00{tun}"

    gdf["__grp"] = gdf.apply(_key, axis=1)
    n_before     = len(gdf)
    rows = []
    for _, grp in gdf.groupby("__grp", sort=False):
        merged = linemerge(list(grp.geometry))
        geoms_out: list = ([merged]
                           if isinstance(merged, LineString)
                           else list(merged.geoms)
                           if isinstance(merged, MultiLineString)
                           else [merged])
        rep = grp.iloc[0].copy()
        for g in geoms_out:
            r = rep.copy()
            r["geometry"] = g
            rows.append(r)

    result = gpd.GeoDataFrame(rows, crs=gdf.crs,
                              geometry="geometry").reset_index(drop=True)
    result = result.drop(columns=["__grp"], errors="ignore")
    n_merged = n_before - len(result)
    print(f"  {n_before:,} → {len(result):,}  (merged {n_merged:,} segments)")
    return result, n_merged


# ── Steps 5 + 6: Gap bridging and junction handling ────────────────────────────

def step5_6_connect_and_junction(gdf: gpd.GeoDataFrame,
                                  gap_dist:   float = GAP_BRIDGE_DIST,
                                  angle_tol:  float = GAP_ANGLE_TOL) -> gpd.GeoDataFrame:
    """
    Step 5 — Bridge small gaps between segments that appear to continue the
              same road: for each segment endpoint, find nearby (≤ gap_dist)
              endpoints of other segments; connect if bearing alignment passes.

    Step 6 — At junctions where 3+ segments meet, prefer the pair with bearing
              closest to 180° (straight-through) when multiple candidates exist.
    """
    gdf = gdf.reset_index(drop=True)

    # Build endpoint table: (seg_pos, "start"|"end", rounded_pt, local_bearing)
    PREC = 0.5  # metres — rounding precision for junction detection

    def _round(xy):
        return (round(xy[0] / PREC) * PREC, round(xy[1] / PREC) * PREC)

    # endpoint_records[k] = (seg_pos, which, pt_coords, bearing)
    ep_records = []
    for i in range(len(gdf)):
        geom = gdf.iloc[i].geometry
        coords = list(geom.coords)
        if len(coords) < 2:
            continue
        ep_records.append((i, "end",   _round(coords[-1]), local_bearing(geom, "end")))
        ep_records.append((i, "start", _round(coords[0]),  local_bearing(geom, "start")))

    if not ep_records:
        return gdf

    # Junction map: rounded_pt → {seg_pos, ...}
    junction_map: dict = defaultdict(set)
    for seg_pos, _, rpt, _ in ep_records:
        junction_map[rpt].add(seg_pos)

    # Spatial index on endpoint coordinates
    ep_points = [Point(x, y) for _, _, (x, y), _ in ep_records]
    ep_gdf    = gpd.GeoDataFrame(geometry=ep_points, crs=gdf.crs)
    ep_sindex = ep_gdf.sindex

    used_ep: set = set()   # (seg_pos, which) pairs already committed to a bridge
    bridges: list = []

    for seg_i, which_i, rpt_i, b_i in ep_records:
        if which_i != "end":
            continue                       # only bridge from END points
        if (seg_i, "end") in used_ep:
            continue

        row_i   = gdf.iloc[seg_i]
        tun_i   = row_i["_is_tunnel"]
        grp_i   = row_i["_hw"]

        pt_i = Point(rpt_i)
        nearby_k = list(ep_sindex.query(pt_i.buffer(gap_dist)))

        best_score = float("inf")
        best_m     = None   # index into ep_records

        for m in nearby_k:
            seg_j, which_j, rpt_j, b_j = ep_records[m]
            if seg_j == seg_i or which_j != "start":
                continue
            if (seg_j, "start") in used_ep:
                continue

            # Don't bridge surface to tunnel or vice-versa
            if gdf.iloc[seg_j]["_is_tunnel"] != tun_i:
                continue
            # Same group only
            if gdf.iloc[seg_j]["_hw"] != grp_i:
                continue

            d = pt_i.distance(Point(rpt_j))
            if d < 0.1 or d > gap_dist:
                continue

            # Bearing alignment: departure bearing b_i should match b_j (start bearing of j)
            b_align = abs(b_i - b_j) % 360
            if b_align > 180:
                b_align = 360 - b_align
            if b_align > angle_tol:
                continue

            # Step 6: junction bonus — prefer straight-through at multi-way junctions
            junc_size = len(junction_map.get(rpt_j, set()))
            junc_bonus = -5.0 if junc_size >= 3 and b_align < 15.0 else 0.0

            score = d + b_align * 0.5 + junc_bonus
            if score < best_score:
                best_score = score
                best_m     = m

        if best_m is not None:
            seg_j, which_j, rpt_j, _ = ep_records[best_m]
            bridge = LineString([rpt_i, rpt_j])
            rep = gdf.iloc[seg_i].copy()
            rep["geometry"] = bridge
            bridges.append(rep)
            used_ep.add((seg_i, "end"))
            used_ep.add((seg_j, "start"))

    if not bridges:
        print(f"  No gap-bridge connections found")
        return gdf

    bridge_gdf = gpd.GeoDataFrame(bridges, crs=gdf.crs,
                                   geometry="geometry").reset_index(drop=True)
    result = pd.concat([gdf, bridge_gdf], ignore_index=True)
    print(f"  Added {len(bridges):,} bridge segments")

    # Re-run linemerge to absorb bridges into parent segments
    result, _ = step4_merge_touching(result)
    return result


# ── Step 7 ─────────────────────────────────────────────────────────────────────

def step7_remove_redundant_paths(gdf: gpd.GeoDataFrame,
                                  proximity: float = REDUNDANT_PATH_DIST,
                                  angle_tol: float = 30.0) -> gpd.GeoDataFrame:
    """Remove footway/path/cycleway features that are parallel and close to
    a non-path road.  Protected (ref-bearing) features are always kept."""
    is_path = gdf["_orig_hw"].isin(PATH_ORIG_FCLASSES) & ~gdf["_protected"]
    paths   = gdf[is_path].reset_index(drop=True)
    others  = gdf[~gdf["_orig_hw"].isin(PATH_ORIG_FCLASSES)].reset_index(drop=True)
    protected_paths = gdf[gdf["_orig_hw"].isin(PATH_ORIG_FCLASSES) & gdf["_protected"]]

    if len(paths) == 0:
        print("  No unprotected path/footway/cycleway features")
        return gdf

    if len(others) == 0:
        return gdf

    road_sindex = others.sindex
    drop_pos: set = set()

    for i in range(len(paths)):
        row  = paths.iloc[i]
        geom = row.geometry
        pb   = line_bearing(geom)
        nearby = list(road_sindex.query(geom.buffer(proximity)))
        for j in nearby:
            road = others.iloc[j]
            if geom.distance(road.geometry) > proximity:
                continue
            if parallel_bearings(pb, line_bearing(road.geometry), angle_tol):
                drop_pos.add(i)
                break

    survivors = paths.iloc[[k for k in range(len(paths)) if k not in drop_pos]]
    print(f"  Removed {len(drop_pos):,} redundant path/footway/cycleway features")
    return pd.concat([others, protected_paths, survivors],
                     ignore_index=True).reset_index(drop=True)


# ── Step 8 ─────────────────────────────────────────────────────────────────────

def step8_handle_roundabouts(gdf: gpd.GeoDataFrame,
                              circ_thr:    float = ROUNDABOUT_CIRC_THR,
                              max_perim:   float = ROUNDABOUT_MAX_PERIM) -> gpd.GeoDataFrame:
    """Detect circular groups of segments (roundabouts), remove them, and
    replace with straight through-road connections between opposite roads."""
    gdf = gdf.reset_index(drop=True)

    # Round endpoints for junction matching
    PREC = 0.5

    def _round(xy):
        return (round(xy[0] / PREC) * PREC, round(xy[1] / PREC) * PREC)

    adj:    dict = defaultdict(set)
    seg_ep: dict = {}

    for i in range(len(gdf)):
        coords = list(gdf.iloc[i].geometry.coords)
        s = _round(coords[0])
        e = _round(coords[-1])
        adj[s].add(i)
        adj[e].add(i)
        seg_ep[i] = (s, e)

    # Trace closed loops
    visited: set = set()
    loops:   list = []
    for i in range(len(gdf)):
        if i in visited:
            continue
        s_pt, e_pt = seg_ep[i]
        if len(adj[s_pt]) < 2 or len(adj[e_pt]) < 2:
            continue
        loop = _trace_loop(i, adj, seg_ep, max_segs=40)
        if loop is not None and len(loop) >= 3:
            # Check we haven't already captured these segments
            if not visited.intersection(loop):
                loops.append(loop)
                visited.update(loop)

    # Filter to true roundabouts via circularity
    roundabout_loops: list = []
    for loop in loops:
        geoms = [gdf.iloc[k].geometry for k in loop]
        perim = sum(g.length for g in geoms)
        if perim > max_perim:
            continue
        merged_geom = unary_union(geoms)
        hull        = merged_geom.convex_hull
        if hull.area == 0:
            continue
        circularity = 4 * math.pi * hull.area / (perim ** 2)
        if circularity >= circ_thr:
            roundabout_loops.append((loop, hull.centroid))

    if not roundabout_loops:
        print("  No roundabouts detected")
        return gdf

    print(f"  Detected {len(roundabout_loops):,} roundabouts")
    all_ra_segs: set = set(k for loop, _ in roundabout_loops for k in loop)
    new_connections: list = []

    for loop, _ in roundabout_loops:
        loop_set = set(loop)

        # Find all junction points on the roundabout perimeter that link to external roads
        seen_pts: set = set()
        junctions: list = []  # (rounded_pt, list[external_seg_pos])
        for seg_k in loop:
            for pt in seg_ep[seg_k]:
                if pt in seen_pts:
                    continue
                seen_pts.add(pt)
                external = [s for s in adj[pt] if s not in loop_set]
                if external:
                    junctions.append((pt, external))

        if not junctions:
            continue

        # For each external segment, compute the bearing AWAY from the junction
        candidates: list = []  # (pt, seg_pos, outward_bearing)
        for pt, ext_segs in junctions:
            for es in ext_segs:
                coords = list(gdf.iloc[es].geometry.coords)
                s_pt, e_pt = seg_ep[es]
                b = (local_bearing(gdf.iloc[es].geometry, "start")
                     if s_pt == pt
                     else (local_bearing(gdf.iloc[es].geometry, "end") + 180) % 360)
                candidates.append((pt, es, b))

        # Pair opposite candidates (bearing diff closest to 180°) from different junctions
        used_cand: set = set()
        for ci, (pt_i, es_i, b_i) in enumerate(candidates):
            if ci in used_cand:
                continue
            best_cj, best_diff = None, float("inf")
            for cj, (pt_j, _, b_j) in enumerate(candidates):
                if cj == ci or cj in used_cand or pt_j == pt_i:
                    continue
                diff = abs(abs(b_i - b_j) % 360 - 180)
                if diff < best_diff:
                    best_diff = diff
                    best_cj   = cj
            if best_cj is not None and best_diff <= 45.0:
                pt_j, _, _ = candidates[best_cj]
                through_line = LineString([pt_i, pt_j])
                rep = gdf.iloc[es_i].copy()
                rep["geometry"] = through_line
                new_connections.append(rep)
                used_cand.add(ci)
                used_cand.add(best_cj)

    # Remove roundabout segments; add through-connections
    keep_mask = ~gdf.index.isin(all_ra_segs)
    result    = gdf[keep_mask].copy()
    if new_connections:
        conn_gdf = gpd.GeoDataFrame(new_connections, crs=gdf.crs,
                                     geometry="geometry").reset_index(drop=True)
        result = pd.concat([result, conn_gdf], ignore_index=True)

    n_removed = len(all_ra_segs)
    n_added   = len(new_connections)
    print(f"  Removed {n_removed:,} roundabout segments, "
          f"added {n_added:,} through-road connections")
    return result.reset_index(drop=True)


# ── Step 9 ─────────────────────────────────────────────────────────────────────

def step9_remove_short_service(gdf: gpd.GeoDataFrame,
                                min_length: float = SHORT_SERVICE_M) -> gpd.GeoDataFrame:
    """Remove service road segments shorter than min_length.
    Protected (ref-bearing) service roads are kept."""
    is_service   = gdf["_orig_hw"] == "service"
    is_short     = gdf.geometry.length < min_length
    is_protected = gdf["_protected"]
    drop_mask    = is_service & is_short & ~is_protected
    n_dropped    = int(drop_mask.sum())
    print(f"  Removed {n_dropped:,} short service roads (< {min_length:.0f} m)")
    return gdf[~drop_mask].reset_index(drop=True)


# ── Save ───────────────────────────────────────────────────────────────────────

def save_output(gdf: gpd.GeoDataFrame, out_path: str) -> None:
    # Drop internal helper columns (all prefixed with '_')
    clean = gdf.drop(columns=[c for c in gdf.columns if c.startswith("_")])

    # Assign group output fclass — the _hw column has already been remapped in step 3
    # but we serialise it as 'fclass' to preserve schema compatibility
    if "fclass" in clean.columns:
        clean["fclass"] = gdf["_hw"]

    clean = clean.to_crs(4326)
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean.to_file(p)
    print(f"  Saved {len(clean):,} features (WGS84) → {p.resolve()}")
    print(f"  Attributes preserved: ref, tunnel")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="OSM Road Network Cleanup and Consolidation Pipeline",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--shp",            required=True,
                    help="Input shapefile (or any OGR-readable format)")
    ap.add_argument("--out",            default="cleaned_roads.shp",
                    help="Output shapefile path")
    ap.add_argument("--pair-dist",      type=float, default=35.0,
                    help="Max distance (m) between opposite one-way carriageways")
    ap.add_argument("--angle-tol",      type=float, default=25.0,
                    help="Bearing tolerance (°) for parallel/opposite tests")
    ap.add_argument("--gap-dist",       type=float, default=GAP_BRIDGE_DIST,
                    help="Max gap (m) to bridge in step 5")
    ap.add_argument("--gap-angle",      type=float, default=GAP_ANGLE_TOL,
                    help="Max bearing difference (°) for gap bridging")
    ap.add_argument("--roundabout-circ",type=float, default=ROUNDABOUT_CIRC_THR,
                    help="Minimum circularity ratio for roundabout detection")
    ap.add_argument("--short-service-m",type=float, default=SHORT_SERVICE_M,
                    help="Service road length (m) below which they are removed")
    ap.add_argument("--no-bridge",      action="store_true",
                    help="Skip step 5/6 (gap bridging and junction handling)")
    ap.add_argument("--no-roundabout",  action="store_true",
                    help="Skip step 8 (roundabout handling)")
    args = ap.parse_args()

    sep = "=" * 68
    print(f"\n{sep}")
    print("  OSM Road Network Cleanup and Consolidation Pipeline")
    print(sep)

    # ── Load & pre-process ─────────────────────────────────────────────────────
    print("\n[0] Load and prepare data")
    gdf = load_and_prep(args.shp)
    n_initial = len(gdf)
    print(f"  Initial features: {n_initial:,}")

    # ── Step 1 ─────────────────────────────────────────────────────────────────
    print("\n[1] Remove unwanted classes (links, busway)")
    n0 = len(gdf)
    gdf = step1_remove_unwanted(gdf)
    _record("1. Remove links & busway", n0, len(gdf))

    # ── Step 2 ─────────────────────────────────────────────────────────────────
    print("\n[2] Flag major roads (ref-protected)")
    n0 = len(gdf)
    gdf = step2_flag_major(gdf)
    _record("2. Flag major roads (ref)", n0, len(gdf), n_removed=0)

    # ── Step 3 ─────────────────────────────────────────────────────────────────
    print("\n[3] Single-direction representation")
    n0 = len(gdf)
    gdf = step3_single_direction(gdf, angle_tol=args.angle_tol,
                                 pair_dist=args.pair_dist)
    _record("3. Single-direction (collapse)", n0, len(gdf),
            n_merged=max(0, n0 - len(gdf)))

    # ── Step 4 ─────────────────────────────────────────────────────────────────
    print("\n[4] Merge touching segments")
    n0 = len(gdf)
    gdf, n_merged_4 = step4_merge_touching(gdf)
    _record("4. Merge touching segments", n0, len(gdf),
            n_removed=0, n_merged=n_merged_4)

    # ── Steps 5 + 6 ────────────────────────────────────────────────────────────
    if not args.no_bridge:
        print("\n[5/6] Connect continuations + junction handling")
        n0 = len(gdf)
        gdf = step5_6_connect_and_junction(gdf,
                                            gap_dist=args.gap_dist,
                                            angle_tol=args.gap_angle)
        _record("5/6. Gap bridge + junction", n0, len(gdf),
                n_removed=max(0, n0 - len(gdf)))
    else:
        print("\n[5/6] Skipped (--no-bridge)")

    # ── Step 7 ─────────────────────────────────────────────────────────────────
    print("\n[7] Remove redundant paths")
    n0 = len(gdf)
    gdf = step7_remove_redundant_paths(gdf)
    _record("7. Remove redundant paths", n0, len(gdf))

    # ── Step 8 ─────────────────────────────────────────────────────────────────
    if not args.no_roundabout:
        print("\n[8] Handle roundabouts")
        n0 = len(gdf)
        gdf = step8_handle_roundabouts(gdf,
                                        circ_thr=args.roundabout_circ,
                                        max_perim=ROUNDABOUT_MAX_PERIM)
        _record("8. Roundabout handling", n0, len(gdf))
    else:
        print("\n[8] Skipped (--no-roundabout)")

    # ── Step 9 ─────────────────────────────────────────────────────────────────
    print("\n[9] Remove short service roads")
    n0 = len(gdf)
    gdf = step9_remove_short_service(gdf, min_length=args.short_service_m)
    _record("9. Remove short service roads", n0, len(gdf))

    # ── Save ───────────────────────────────────────────────────────────────────
    print("\n[Output] Save WGS84 shapefile")
    save_output(gdf, args.out)

    # ── Final report ───────────────────────────────────────────────────────────
    print_final_report(n_initial)
    print("  Pipeline complete.\n")


if __name__ == "__main__":
    main()
