# GeoAI Mini Project — OSM Israel Road Network Cleaning

A Python pipeline that cleans, consolidates, and simplifies OpenStreetMap road data for Israel.  
The goal is to produce a single, non-redundant, analysis-ready centerline network from raw OSM exports.

---

## Problem

Raw OSM road exports contain several data quality issues that make network analysis difficult:

| Issue | Description |
|-------|-------------|
| **Link roads & busways** | Connector ramps and bus-only ways inflate the network with features irrelevant to general routing |
| **Parallel carriageways** | Divided roads are modelled as two separate one-way lines; most analysis requires a single centerline |
| **Fragmented segments** | A single road is split into dozens of short pieces at every OSM node |
| **Redundant paths** | Footways and cycleways often duplicate the edge of an adjacent road |
| **Roundabouts** | Traffic circles appear as rings of segments; general routing needs straight through-road connections |
| **Short service stubs** | Dead-end service roads shorter than 100 m add noise without contributing to connectivity |

The pipeline resolves all of these in a defined sequence.

---

## Repository layout

```
MiniProject/
├── roads_OSM/
│   └── roads_OSM.shp              # raw OSM export for Israel
├── intermediate_data/
│   ├── OSM_roads_preprocess.shp   # output of Part 1
│   ├── OSM_roads_merge.shp        # output of Part 2
│   ├── OSM_roads_merge_paralle.shp  # output of Part 3
│   └── OSM_roads_merge_paralle_circle.shp  # output of Part 4
├── final/
│   └── <final output>.shp         # output of Part 5
├── road_pipeline.py               # main cleaning pipeline
├── dashboard.py                   # Streamlit interactive dashboard
└── README.md
```

---

## Pipeline overview

All processing is done in **UTM Zone 36 North (EPSG:32636)**.  
Only the `fclass`, `ref`, `tunnel`, and `class` columns are used for processing (other fields are retained but not used).  
Tunnels (`tunnel = T`) are never merged with surrounding roads.

```
Input shapefile
      │
      ▼
[Part 1] Preprocess ─────────── reproject to UTM36N (EPSG:32636);
      │                         remove "link" fclasses and busway;
      │                         add class column (reclassify fclass);
      │                         detect traffic circles (radius < 50 m)
      │                         → intermediate_data/OSM_roads_preprocess.shp
      ▼
[Part 2] Merge lines ────────── merge contiguous segments whose endpoints
      │                         touch at ~180° (±10° tolerance);
      │                         handle 2-line, T, X/+, and multi-way junctions;
      │                         keep attributes from longest / highest-hierarchy road;
      │                         tunnels are never merged;
      │                         recalculate length_m, length_km
      │                         → intermediate_data/OSM_roads_merge.shp
      ▼
[Part 3] Parallel roads ─────── detect and collapse parallel duplicate carriageways;
      │                         keep higher-ranked road, or longer if same rank;
      │                         extend Y-intersection branches to perpendicular road
      │                         or traffic circle;
      │                         recalculate length_m, length_km
      │                         → intermediate_data/OSM_roads_merge_paralle.shp
      ▼
[Part 4] Traffic circles ────── find roads intersecting each traffic circle;
      │                         extend those roads to meet at the optimal
      │                         central connection point; remove circle geometry;
      │                         recalculate length_m, length_km
      │                         → intermediate_data/OSM_roads_merge_paralle_circle.shp
      ▼
[Part 5] Remove short roads ─── Rule 1: drop dead-end stubs (1 connection, < 100 m);
                                Rule 2: drop isolated roads (0 connections, < 200 m);
                                recalculate length_m, length_km
                                → final/<output>.shp
```

---

## Class reclassification (Part 1)

After removing link roads and busways, each feature is assigned a `class` value based on its `fclass`.  
Classification is hierarchical — the order below defines priority when merging or resolving conflicts.

| Priority | Class | Input fclasses |
|----------|-------|----------------|
| 1 (highest) | **Highway** | `primary`, `motorway`, `trunk` |
| 2 | **Residential** | `residential`, `secondary`, `pedestrian`, `tertiary`, `service`, `living_street` |
| 3 | **Paths** | `footway`, `path`, `steps` |
| 4 | **Track** | `track`, `track_grade1`, `track_grade2`, `track_grade3`, `track_grade4`, `track_grade5` |
| 5 | **Other** | `bridleway`, `unclassified`, `unknown` |
| 6 (lowest) | **Bike** | `cycleway` |

Features identified as traffic circles are assigned `class = "traffic circle"`.

---

## Traffic circle detection (Part 1, step 3)

A set of line segments is classified as a traffic circle when:

- The segments form a closed loop (circle, oval, or near-circular shape).
- The isoperimetric quotient **Q = 4π × area / perimeter² ≥ 0.90**.
- The bounding-circle radius **r ≤ 50 m**.
- Typically 1–10 segments form the loop (more are allowed).

Detection runs in three passes:

1. **Self-closing single segments** (start == end) — detected directly from the enclosed polygon area.
2. **Multi-segment closed loops** — traced via a "head-home" heuristic that follows connected segments back to the chain's origin.
3. **Near-complete open arcs** — chains whose remaining gap is less than **12.5 % of the full circumference** (`gap / (arc_length + gap) < 0.125`). The gap is bridged with a **fitted circular arc** (circle centre from the convex-hull centroid, radius interpolated between the two free ends) so the closure continues the circular curvature rather than cutting a straight chord across the gap.

Each detected circle is emitted as a **single merged line feature** (the arc segments — plus the fitted closing arc for near-complete circles — are stitched into one line via `linemerge`, with a rounded-endpoint fallback). The merged feature is tagged `class = "traffic circle"` and handled in Part 4.

---

## Line merging rules (Part 2)

Merging runs in two sequential phases, each iterated until convergence:

**Phase 1 — ref-based pass (runs first):**  
Segments that share the same non-empty `ref` road number and whose endpoints touch
are merged with a **±45° tolerance**. This joins numbered-road segments preferentially
before the generic angle pass.

Segments with a compound ref (e.g. `ref = "1:6"`) are first **duplicated** into one
copy per road number (`ref = "1"` and `ref = "6"`). Each copy then participates in
Phase 1 with its respective road number.

**Phase 2 — angle-based pass:**  
All remaining segments are merged using the standard angle rules below.

| Junction type | Merge behaviour |
|---------------|-----------------|
| **2 lines at 1 point** | Always merge |
| **Y intersection (3 lines)** | Do not merge |
| **T intersection** | Merge the pair closest to 180° (within ±10°) |
| **X / + intersection (4 lines)** | Merge the pair closest to 180° (within ±10°) |
| **5 + lines at 1 point** | Find and merge any pair closest to 180° (within ±10°) |

In both phases, **hierarchy always wins** when choosing attributes: the higher-rank
class road provides the attributes. Length breaks ties within the same class.  
Tunnels (`tunnel = T`) are never merged with non-tunnel segments in either phase.

---

## Parallel road handling (Part 3)

| Condition | Action |
|-----------|--------|
| Two roads with different class ranks | Keep the higher-rank road; delete the lower-rank road |
| Two roads with the same class rank | Keep the longer road; delete the shorter road |
| Y-shaped intersection where both branches mirror each other | Extend the longest (base) road to the perpendicular road or traffic circle |

---

## Running the pipeline

```bash
python road_pipeline.py --shp roads_OSM/roads_OSM.shp
```

Intermediate shapefiles are written automatically to `intermediate_data/`.  
The final cleaned shapefile is written to the `final/` folder.

---

## Dependencies

```bash
pip install geopandas shapely pandas
```

| Package | Purpose |
|---------|---------|
| `geopandas` | spatial I/O, CRS reprojection, spatial indexing |
| `shapely` | geometry operations (`linemerge`, `make_valid`, `unary_union`) |
| `pandas` | attribute table manipulation |

---

## Dashboard

An interactive Streamlit dashboard is included for exploring the output:

```bash
streamlit run dashboard.py
```

Features: map view (pydeck WebGL), road-type / direction filters, charts (type distribution, length, one-way ratio), and CSV export.

---

## Output attributes

All distance-based processing runs in UTM Zone 36 North (EPSG:32636) and intermediate files are saved in that CRS.

| Attribute | Description |
|-----------|-------------|
| `fclass` | Original OSM road type |
| `class` | Consolidated road class (see reclassification table) |
| `ref` | Road reference number (route identifier) |
| `tunnel` | Tunnel flag (`T` = tunnel) |
| `length_m` | Segment length in metres (recalculated after each part) |
| `length_km` | Segment length in kilometres (recalculated after each part) |
