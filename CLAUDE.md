# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

GeoAI mini-project that cleans and consolidates raw OpenStreetMap road data for Israel (or any OSM export) into an analysis-ready centerline network. The two deliverables are a processing pipeline (`road_pipeline.py`) and an interactive dashboard (`dashboard.py`).

## Commands

**Run the pipeline:**
```
python road_pipeline.py --shp "OSM Data/roads_OSM.shp"
python road_pipeline.py --shp input.shp --crs 32636 --out final/output.shp
```

**Run the dashboard:**
```
streamlit run dashboard.py
```

**Install dependencies:**
```
pip install -r requirements.txt
```

## Architecture

### road_pipeline.py — 5-part sequential pipeline

All geometry work runs in a user-specified projected CRS (default EPSG:32636 UTM36N). The pipeline reads one shapefile, runs five parts in order, saves an intermediate shapefile after each part, and writes the final output to `final/`.

**Part 1 `part1_preprocess`** — Load, reproject, remove `fclass` values containing `"link"` or equal to `"busway"`, add a `class` column mapped from `CLASS_MAP`, then call `_detect_traffic_circles` which traces closed loops via `_trace_loop` and tags qualifying loops (isoperimetric Q ≥ 0.90, radius ≤ 50 m) as `class = "traffic circle"`.

**Part 2 `part2_merge_lines`** — Iterative endpoint-matching merge. Each pass builds a junction map (rounded coordinates → segment endpoints), finds the straightest pair at each junction using `_best_pair`, and merges them with `_merge_geoms`. Continues until no new merges occur. Junction cases: 2-line always merges; 3-line Y (no pair within 10°) skips; T/X/5+ merges the pair closest to 180°. Tunnels only merge with tunnels. Winner attributes come from the higher class rank.

**Part 3 `part3_parallel_roads`** — Scans spatially nearby pairs via `sindex`, checks bearing similarity (≤ 20°) and midpoint lateral distance (≤ 15 m). Rule 1: different class ranks → delete lower rank. Rule 2: same rank + ≥ 50% of shorter road within 10 m → `_make_centerline`. Rule 3: same rank, not close → keep longer. Then `_handle_y_splits` detects fork arms (two segs sharing an endpoint with ≤ 30° bearing difference) and extends the incoming stem to bridge them. When both arms land on a common through-road or a single traffic circle (`_common_y_target`, within `Y_TARGET_SNAP_M` = 5 m), the stem is extended along the fork→midpoint direction until it actually meets that line/circle (`_extend_to_target`); otherwise it bridges to the plain midpoint between the arms' far ends.

**Part 4 `part4_traffic_circles`** — Groups traffic circle segments into circles via `_connected_components`, computes each circle's centroid, extends connecting roads' nearest endpoint to the centroid, then deletes all `class = "traffic circle"` segments. After connecting, `_merge_through_at_points` merges straight through-road pairs (X / + / T) at each centroid: direction is judged by each road's general heading via `_bearing_general` (a vertex `TC_MERGE_LOOKAHEAD` ≈ 3 in from the centre, to ignore the roundabout-entry kink), then the standard `MERGE_ANGLE_TOL` (10°) straight-through rule applies, reusing `_best_pair`/`_merge_geoms` and the Part 2 tunnel/class-rank winner rules.

**Part 5 `part5_short_roads`** — Removes segments shorter than 100 m that have exactly 1 network connection (dead-end stubs, one endpoint free). Connectivity is geometric: an endpoint counts as connected when it **touches** any other road's endpoint *or* interior (a T-bone), detected via the spatial index within `TOUCH_TOL_M` (0.5 m) — not by exact endpoint matching.

### Key shared utilities (in road_pipeline.py)

| Function | Purpose |
|---|---|
| `CLASS_MAP` / `CLASS_RANK` | fclass → class string, class → priority integer |
| `_rpt(xy, prec)` | Round coordinate to grid for vertex-matching |
| `_bearing_toward_jn(line, which_end)` | Bearing pointing INTO the junction — both start and end return comparable values; straight-through pairs are ~180° apart |
| `_best_pair(candidates)` | O(n²) scan returning the pair with minimum deviation from 180° |
| `_make_centerline(a, b, n=100)` | 100-point interpolation average of two lines |
| `_add_lengths(gdf)` | Recalculates `length_m` and `length_km` in-place |
| `_trace_loop` / `_connected_components` | Graph utilities for loop detection and component grouping |
| `_record` / `_print_report` | Per-part statistics tracking |

### dashboard.py

Streamlit app that loads the final shapefile from `final/`. Uses pydeck for WebGL map rendering and plotly for charts. Reads the `class`, `length_m`, `ref`, and `tunnel` columns. No processing logic — display only.

## Data

- Input: `OSM Data/roads_OSM.shp` — raw OSM export for Israel (~500 k features, EPSG:4326)
- Intermediate outputs: `intermediate_data/` (created at runtime)
- Final output: `final/OSM_roads_clean.shp` (created at runtime)

## Processing columns

Only `fclass`, `ref`, `tunnel`, and `class` drive processing logic. All other original OSM columns are preserved unchanged throughout.

## Maintenance rule

Whenever `road_pipeline.py` or `prompt_main_new_fixed.md` is updated, `README.md` must be updated to stay in sync. Specifically:
- Changes to pipeline logic, parameters, or part behaviour → update the matching section in README.md
- Changes to the class hierarchy or fclass mappings → update the class reclassification table in README.md
- Changes to CLI arguments or output paths → update the "Running the pipeline" section in README.md
- New or removed processing steps → update the pipeline flowchart in README.md

## Clarified design decisions

- **Hierarchy vs length:** class rank always wins when choosing attributes during a merge; length only breaks ties within the same class.
- **Tunnel rule:** `tunnel = T` segments merge only with other `tunnel = T` segments.
- **Parallel threshold:** 50% of the shorter road's sampled points must be within 10 m for centerline collapse.
- **Short road rule:** removes roads with exactly 1 network connection (not 0, not 2+). A connection means an endpoint **touches** another road's endpoint or interior (T-bone included), tested geometrically within `TOUCH_TOL_M` (0.5 m).
- The full clarified spec is in `prompt_main_new_fixed.md`.
