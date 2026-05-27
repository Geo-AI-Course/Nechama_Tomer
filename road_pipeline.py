#!/usr/bin/env python3
"""
OSM Israel road cleaning pipeline.

Workflow
--------
  1. Load & reproject to ITM (EPSG:2039)
  2. Fix / drop bad geometries
  3. Normalize OSM attributes  (fclass/highway, oneway, lanes)
  4. Classify: 'keep_dual' vs 'collapse-to-centerline'
  5a. Corridor collapse  — trunk / motorway / primary:
       cluster ALL parallel segments within 50 m → single centerline
  5b. Carriageway collapse — all other roads:
       pair opposite-direction one-way segments → single centerline
  6. Merge fragmented segments of the same road  (linemerge)
  7. Remove near-duplicate segments              (Hausdorff)
  8. Save output as WGS84 Shapefile

keep_dual rules  (applied to non-corridor roads only)
--------------
  - fclass in {motorway_link}                        → always keep
  - lanes per direction  > LANES_KEEP_DUAL_THRESHOLD → keep
  - explicit tag dual_carriageway=yes or divided=yes → keep

Usage
-----
  python road_pipeline.py --shp roads_OSM/roads_OSM.shp
  python road_pipeline.py --shp roads_OSM/roads_OSM.shp --out cleaned.shp \\
      --pair-dist 40 --angle-tol 30 --no-dedup
"""

import argparse
import math
import warnings
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge

warnings.filterwarnings("ignore")

# ── Global parameters (can be overridden by CLI) ───────────────────────────────
ITM_EPSG               = 2039
ALWAYS_KEEP_DUAL       = {"motorway_link"}   # motorway/trunk/primary → corridor step
LANES_KEEP_DUAL_THRESHOLD = 3                # keep dual if lanes_per_direction > this
CLUSTER_FCLASSES       = {"trunk", "motorway", "primary"}
CLUSTER_DIST           = 50.0               # metres to each side of the central line


# ── Attribute helpers ──────────────────────────────────────────────────────────

def parse_lanes(val):
    """Parse OSM 'lanes' field to int. Returns None if missing or unparseable."""
    if pd.isna(val) or val is None:
        return None
    try:
        return int(float(str(val).strip().split(";")[0].split("|")[0]))
    except (ValueError, TypeError):
        return None


def parse_oneway(val):
    """
    Return True  if oneway=yes/1/true,
           -1    if oneway=-1/reverse,
           False otherwise.
    """
    if pd.isna(val) or val is None:
        return False
    s = str(val).strip().lower()
    if s in ("yes", "1", "true"):
        return True
    if s in ("-1", "reverse"):
        return -1
    return False


def line_bearing(line):
    """Overall bearing of a LineString (first → last coord), degrees [0, 360)."""
    c = list(line.coords)
    return math.degrees(math.atan2(c[-1][0] - c[0][0], c[-1][1] - c[0][1])) % 360


def opposite_bearings(b1, b2, tol):
    """True when two bearings are ~180 ° apart within ±tol degrees."""
    return abs(abs(b1 - b2) % 360 - 180) <= tol


def same_bearings(b1, b2, tol):
    """True when two bearings point in roughly the same direction within ±tol degrees."""
    diff = abs(b1 - b2) % 360
    return diff <= tol or diff >= (360 - tol)


def parallel_bearings(b1, b2, tol):
    """True when two lines are parallel — same OR opposite direction."""
    return same_bearings(b1, b2, tol) or opposite_bearings(b1, b2, tol)


def _connected_components(n_nodes, edges):
    """
    Union-find connected components (no external dependency).
    Returns a list of lists, each containing the positional indices of one cluster.
    """
    parent = list(range(n_nodes))

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for i, j in edges:
        pi, pj = find(i), find(j)
        if pi != pj:
            parent[pi] = pj

    from collections import defaultdict
    comps = defaultdict(list)
    for i in range(n_nodes):
        comps[find(i)].append(i)
    return list(comps.values())


def corridor_centerline(lines, max_cross_dist, n=200):
    """
    Collapse a cluster of parallel LineStrings into a single centerline.

    Algorithm:
      1. Use the longest line as the spine.
      2. Sample n equally-spaced points along the spine.
      3. For each spine point, collect the nearest point from every other line
         that lies within max_cross_dist metres.
      4. Average all collected points → centerline coordinate.
    """
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


def make_centerline(line_a, line_b, n=100):
    """
    Compute centerline between two co-oriented parallel LineStrings by
    averaging n equally-spaced interpolated points.
    Call with both lines oriented in the SAME direction.
    """
    pts_a = [line_a.interpolate(i / (n - 1), normalized=True) for i in range(n)]
    pts_b = [line_b.interpolate(i / (n - 1), normalized=True) for i in range(n)]
    coords = [((a.x + b.x) / 2, (a.y + b.y) / 2) for a, b in zip(pts_a, pts_b)]
    return LineString(coords).simplify(1.0)


# ── Step 1 ─────────────────────────────────────────────────────────────────────

def step1_load(path):
    gdf = gpd.read_file(path)
    print(f"  Loaded {len(gdf):,} features  CRS={gdf.crs}")
    if gdf.crs is None:
        gdf = gdf.set_crs(ITM_EPSG)
        print(f"  No CRS found — assumed EPSG:{ITM_EPSG}")
    elif gdf.crs.to_epsg() != ITM_EPSG:
        gdf = gdf.to_crs(ITM_EPSG)
        print(f"  Reprojected → EPSG:{ITM_EPSG}")
    return gdf


# ── Step 2 ─────────────────────────────────────────────────────────────────────

def step2_clean_geometries(gdf):
    n0 = len(gdf)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    gdf.geometry = gdf.geometry.make_valid()
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)
    gdf = gdf[gdf.geom_type == "LineString"].reset_index(drop=True)
    print(f"  {n0:,} → {len(gdf):,} LineStrings  (dropped null/empty/multi/polygon)")
    return gdf


# ── Step 3 ─────────────────────────────────────────────────────────────────────

def step3_normalize(gdf):
    gdf = gdf.copy()
    gdf["_oneway"] = (gdf["oneway"].apply(parse_oneway)
                      if "oneway" in gdf.columns else False)
    gdf["_lanes"]  = (gdf["lanes"].apply(parse_lanes)
                      if "lanes"  in gdf.columns else None)
    # Geofabrik OSM exports use 'fclass'; fall back to 'highway' for other sources
    if "fclass" in gdf.columns:
        gdf["_hw"] = gdf["fclass"].str.strip().str.lower()
    elif "highway" in gdf.columns:
        gdf["_hw"] = gdf["highway"].str.strip().str.lower()
    else:
        gdf["_hw"] = ""
    n_ow = (gdf["_oneway"] == True).sum()
    print(f"  one-way forward={n_ow:,}  road types={gdf['_hw'].nunique()}  "
          f"(field: {'fclass' if 'fclass' in gdf.columns else 'highway'})")
    return gdf


# ── Step 4 ─────────────────────────────────────────────────────────────────────

def step4_classify(gdf):
    """
    Tag each feature with _keep_dual=True if it belongs to a divided highway
    that should keep separate carriageways; False = candidate for centerline.

    Note: CLUSTER_FCLASSES (trunk/motorway/primary) are always False here —
    they are handled by step5a corridor clustering, not the pair-collapse logic.
    """
    def _keep(row):
        if row["_hw"] in CLUSTER_FCLASSES:
            return False  # corridor step handles these
        if row["_hw"] in ALWAYS_KEEP_DUAL:
            return True
        lanes = row["_lanes"]
        if lanes is not None:
            lpd = lanes if row["_oneway"] else lanes / 2
            if lpd > LANES_KEEP_DUAL_THRESHOLD:
                return True
        if (row.get("dual_carriageway") == "yes"
                or row.get("divided") == "yes"):
            return True
        return False

    gdf = gdf.copy()
    gdf["_keep_dual"] = gdf.apply(_keep, axis=1)
    n_k = int(gdf["_keep_dual"].sum())
    print(f"  keep_dual={n_k:,}   collapse_candidates={len(gdf) - n_k:,}")
    return gdf


# ── Step 5a ────────────────────────────────────────────────────────────────────

def step5a_cluster_corridors(gdf, cluster_dist=CLUSTER_DIST, angle_tol=25.0):
    """
    For trunk / motorway / primary roads: group all parallel segments within
    cluster_dist metres into clusters using connected components, then collapse
    each cluster to a single centerline (regardless of oneway status).

    Two segments join the same cluster when BOTH:
      - their bearings are parallel (same or opposite, within angle_tol °)
      - their geometries are within cluster_dist metres of each other
    """
    is_major = gdf["_hw"].isin(CLUSTER_FCLASSES)
    major    = gdf[is_major].reset_index(drop=True)
    rest     = gdf[~is_major].copy()

    if len(major) == 0:
        print("  No trunk/motorway/primary segments — step skipped")
        return gdf

    print(f"  Clustering {len(major):,} trunk/motorway/primary segments "
          f"(corridor width ±{cluster_dist} m) ...")

    sindex = major.sindex
    edges  = []

    for i in range(len(major)):
        geom = major.iloc[i].geometry
        b    = line_bearing(geom)
        for j in list(sindex.query(geom.buffer(cluster_dist))):
            if j <= i:
                continue
            if parallel_bearings(b, line_bearing(major.iloc[j].geometry), angle_tol):
                edges.append((i, j))

    components = _connected_components(len(major), edges)
    n_solo     = sum(1 for c in components if len(c) == 1)
    n_merged   = len(components) - n_solo
    print(f"  {len(components):,} clusters  "
          f"({n_merged:,} multi-segment, {n_solo:,} isolated)")

    new_rows = []
    for comp in components:
        lines = [major.iloc[i].geometry for i in comp]
        try:
            cl = corridor_centerline(lines, cluster_dist)
        except Exception:
            cl = lines[0]
        rep             = major.iloc[comp[0]].copy()
        rep["geometry"] = cl
        rep["_oneway"]  = False
        new_rows.append(rep)

    result_major = gpd.GeoDataFrame(
        new_rows, crs=gdf.crs, geometry="geometry"
    ).reset_index(drop=True)
    print(f"  → {len(result_major):,} corridor centerlines")

    return pd.concat([rest, result_major], ignore_index=True)


# ── Step 5b ────────────────────────────────────────────────────────────────────

def step5_collapse(gdf, pair_dist=35.0, angle_tol=25.0):
    """
    Among non-keep_dual, non-major roads with oneway=yes, find pairs of segments
    that travel in opposite directions and are close & parallel.  Replace each
    pair with a single bidirectional centerline segment.
    (trunk/motorway/primary are already handled by step5a and are skipped here.)

    Pairing criteria (all must hold):
      - same road type, NOT in CLUSTER_FCLASSES
      - opposite bearing  (within angle_tol degrees of 180 ° difference)
      - distance between geometries < pair_dist metres
      - if both carry a 'ref'  tag → must match
      - if both carry a 'name' tag → must match
      - length ratio in [0.2, 5.0]  (avoids pairing very short with very long)
    """
    # Exclude major fclasses (handled by step5a) and keep_dual roads
    mask = (~gdf["_keep_dual"]
            & (gdf["_oneway"] == True)
            & ~gdf["_hw"].isin(CLUSTER_FCLASSES))
    coll = gdf[mask].reset_index(drop=True)
    rest = gdf[~mask].copy()

    if len(coll) == 0:
        print("  No collapse candidates — step skipped")
        return gdf

    print(f"  Searching {len(coll):,} candidates for opposite-direction pairs ...")

    has_ref  = "ref"  in coll.columns
    has_name = "name" in coll.columns

    sindex = coll.sindex
    used   = set()   # positional indices already assigned to a pair
    pairs  = []      # list of (i, j) positional-index pairs

    for i in range(len(coll)):
        if i in used:
            continue
        row  = coll.iloc[i]
        geom = row.geometry
        b    = line_bearing(geom)

        nearby = list(sindex.query(geom.buffer(pair_dist)))

        best_j    = None
        best_dist = float("inf")

        for j in nearby:
            if j == i or j in used:
                continue
            other = coll.iloc[j]

            # highway type must match
            if other["_hw"] != row["_hw"]:
                continue

            # must be travelling in opposite directions
            if not opposite_bearings(b, line_bearing(other.geometry), angle_tol):
                continue

            # ref must match when present on both
            if has_ref:
                ra = str(row.get("ref")  or "").strip()
                rb = str(other.get("ref") or "").strip()
                if ra and rb and ra != rb:
                    continue

            # name must match when present on both
            if has_name:
                na = str(row.get("name")  or "").strip()
                nb = str(other.get("name") or "").strip()
                if na and nb and na != nb:
                    continue

            # length ratio sanity check
            ratio = geom.length / max(other.geometry.length, 1e-9)
            if not (0.2 < ratio < 5.0):
                continue

            d = geom.distance(other.geometry)
            if d < best_dist:
                best_dist = d
                best_j    = j

        if best_j is not None:
            pairs.append((i, best_j))
            used.add(i)
            used.add(best_j)

    print(f"  Paired {len(pairs):,} opposite-direction pairs → {len(pairs):,} centerlines")

    new_rows = []
    for i, j in pairs:
        row_a  = coll.iloc[i]
        row_b  = coll.iloc[j]
        line_a = row_a.geometry
        line_b = row_b.geometry

        # Orient line_b so it points the same way as line_a before averaging
        if opposite_bearings(line_bearing(line_a), line_bearing(line_b), angle_tol):
            line_b = LineString(list(line_b.coords)[::-1])

        try:
            cl = make_centerline(line_a, line_b)
        except Exception:
            cl = line_a  # fallback: keep first carriageway

        new = row_a.copy()
        new["geometry"] = cl
        new["_oneway"]  = False
        new_rows.append(new)

    unpaired = coll.iloc[[i for i in range(len(coll)) if i not in used]]
    print(f"  Unpaired one-way segments (kept as-is): {len(unpaired):,}")

    parts = [rest]
    if new_rows:
        paired_gdf = gpd.GeoDataFrame(
            new_rows, crs=gdf.crs, geometry="geometry"
        ).reset_index(drop=True)
        parts.append(paired_gdf)
    if len(unpaired):
        parts.append(unpaired)

    return pd.concat(parts, ignore_index=True)


# ── Step 6 ─────────────────────────────────────────────────────────────────────

def step6_merge_segments(gdf):
    """
    Merge touching / collinear segments that share the same road identity
    (highway + ref + name) using Shapely's linemerge.  Segments that do NOT
    share an endpoint are left separate (road network topology is preserved).
    """
    print(f"  Merging {len(gdf):,} segments ...")

    gdf = gdf.copy()

    def _key(row):
        hw  = row["_hw"]
        ref = str(row.get("ref")  or "") if "ref"  in gdf.columns else ""
        nm  = str(row.get("name") or "") if "name" in gdf.columns else ""
        return f"{hw}\x00{ref}\x00{nm}"

    gdf["__grp"] = gdf.apply(_key, axis=1)

    rows = []
    for _, grp in gdf.groupby("__grp"):
        merged = linemerge(list(grp.geometry))
        if isinstance(merged, LineString):
            geoms_out = [merged]
        elif isinstance(merged, MultiLineString):
            geoms_out = list(merged.geoms)
        else:
            geoms_out = [merged]

        rep = grp.iloc[0].copy()
        for g in geoms_out:
            r = rep.copy()
            r["geometry"] = g
            rows.append(r)

    result = gpd.GeoDataFrame(
        rows, crs=gdf.crs, geometry="geometry"
    ).reset_index(drop=True)
    result = result.drop(columns=["__grp"], errors="ignore")
    print(f"  After merge: {len(result):,} segments")
    return result


# ── Step 7 ─────────────────────────────────────────────────────────────────────

def step7_dedup(gdf, threshold=3.0):
    """
    Drop near-exact duplicate LineStrings: same highway type and
    Hausdorff distance < threshold metres.  When a pair is found the
    shorter segment is removed.
    """
    print(f"  Deduplicating {len(gdf):,} features (Hausdorff threshold={threshold} m) ...")
    gdf = gdf.reset_index(drop=True)
    sindex = gdf.sindex
    drop   = set()

    for idx in range(len(gdf)):
        if idx in drop:
            continue
        row  = gdf.iloc[idx]
        geom = row.geometry
        nearby_pos = list(sindex.query(geom.buffer(threshold + 1)))

        for pos in nearby_pos:
            if pos <= idx or pos in drop:
                continue
            other = gdf.iloc[pos]
            if row["_hw"] != other["_hw"]:
                continue
            try:
                if geom.hausdorff_distance(other.geometry) <= threshold:
                    # keep the longer one
                    if geom.length >= other.geometry.length:
                        drop.add(pos)
                    else:
                        drop.add(idx)
                        break  # this segment is now scheduled for removal
            except Exception:
                continue

    result = gdf.iloc[[i for i in range(len(gdf)) if i not in drop]].reset_index(drop=True)
    print(f"  Removed {len(drop):,} duplicates → {len(result):,} features")
    return result


# ── Step 8 ─────────────────────────────────────────────────────────────────────

def step8_save(gdf, out_path):
    clean = gdf.drop(columns=[c for c in gdf.columns if c.startswith("_")])
    clean = clean.to_crs(4326)   # reproject to WGS84 geographic for output
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean.to_file(p)             # default driver = ESRI Shapefile
    print(f"  Saved {len(clean):,} features (WGS84) → {p.resolve()}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="OSM Israel road cleaning pipeline",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--shp",       required=True,
                    help="Input shapefile (or any OGR-readable format)")
    ap.add_argument("--out",       default="cleaned_roads.shp",
                    help="Output shapefile path (.shp)")
    ap.add_argument("--pair-dist", type=float, default=35.0,
                    help="Max edge-to-edge distance (m) to consider two "
                         "one-way segments as opposite carriageways")
    ap.add_argument("--angle-tol", type=float, default=25.0,
                    help="Bearing tolerance (°) for 'opposite direction' test")
    ap.add_argument("--dedup-thr", type=float, default=3.0,
                    help="Hausdorff distance (m) threshold for duplicate removal")
    ap.add_argument("--no-merge",  action="store_true",
                    help="Skip step 6 (segment merging)")
    ap.add_argument("--no-dedup",  action="store_true",
                    help="Skip step 7 (near-duplicate removal)")
    args = ap.parse_args()

    sep = "=" * 62
    print(f"\n{sep}")
    print("  OSM Road Cleaning Pipeline — Israel")
    print(sep)

    print("\n[1] Load & reproject")
    gdf = step1_load(args.shp)

    print("\n[2] Clean geometries")
    gdf = step2_clean_geometries(gdf)

    print("\n[3] Normalize attributes")
    gdf = step3_normalize(gdf)

    print("\n[4] Classify roads")
    gdf = step4_classify(gdf)

    print("\n[5a] Corridor collapse — trunk / motorway / primary")
    gdf = step5a_cluster_corridors(gdf, angle_tol=args.angle_tol)

    print("\n[5b] Carriageway collapse — remaining one-way pairs")
    gdf = step5_collapse(gdf, pair_dist=args.pair_dist, angle_tol=args.angle_tol)

    if not args.no_merge:
        print("\n[6] Merge fragmented segments")
        gdf = step6_merge_segments(gdf)

    if not args.no_dedup:
        print("\n[7] Remove near-duplicates")
        gdf = step7_dedup(gdf, threshold=args.dedup_thr)

    print("\n[8] Save output")
    step8_save(gdf, args.out)

    print(f"\n{sep}")
    print("  Pipeline complete.")
    print(f"{sep}\n")


if __name__ == "__main__":
    main()
