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
| **Short service stubs** | Dead-end service roads shorter than 200 m add noise without contributing to connectivity |

The pipeline resolves all of these in a defined sequence.

---

## Repository layout

```
MiniProject/
├── roads_OSM/
│   └── roads_OSM.shp       # raw OSM export for Israel
├── road_pipeline.py         # Main cleaning pipeline (9 steps)
├── dashboard.py             # Streamlit interactive dashboard
└── README.md
```

---

## Pipeline overview

```
Input shapefile
      │
      ▼
[0] Load & prepare  ────────────── reproject WGS84 → ITM (EPSG:2039),
      │                            fix invalid geometries, normalise attributes
      ▼
[1] Remove unwanted classes  ────── drop fclass containing "link" (motorway_link,
      │                             trunk_link, …) and fclass = "busway"
      ▼
[2] Flag major roads  ───────────── mark ref-bearing features as protected;
      │                             protected features are never removed by later steps
      ▼
[3] Single-direction  ───────────── collapse dual carriageways:
      │                               • trunk/motorway/primary → corridor centerline
      │                                 (cluster all parallel segments within 50 m)
      │                               • other one-way pairs → averaged centerline
      │                             Tunnels processed separately; never merged with surface
      ▼
[4] Merge touching segments  ────── linemerge contiguous pieces sharing the same
      │                             road group + ref + name + tunnel status
      ▼
[5/6] Gap bridging + junctions  ── bridge small gaps (≤ 15 m) between segments
      │                             whose endpoint bearings align;
      │                             at multi-way junctions, prefer straight-through pairs
      ▼
[7] Remove redundant paths  ─────── remove footway / path / cycleway features that
      │                             lie within 50 m of and are parallel to a road;
      │                             ref-protected paths are always kept
      ▼
[8] Handle roundabouts  ─────────── detect circular segment loops (circularity ≥ 0.70),
      │                             replace with straight through-road connections
      │                             between approximately opposite incoming roads
      ▼
[9] Remove short service roads  ─── drop service roads shorter than 200 m
      │                             (ref-protected service roads are kept)
      ▼
[Output] Save WGS84 shapefile  ──── reproject back to EPSG:4326; strip internal
                                    columns; always preserve ref and tunnel
```

---

## fclass grouping

After step 3 the pipeline consolidates the 28+ OSM road types into five groups.  
Merging (step 4) and through-connection creation (step 8) only join segments within the same group.

| Group | Input fclasses | Output fclass |
|-------|----------------|---------------|
| **highway** | primary, motorway, trunk | `primary` |
| **residential** | residential, secondary, pedestrian, tertiary, service, living_street | `residential` |
| **paths** | footway, path, steps | `path` |
| **track** | track, track_grade1 … track_grade5 | `track` |
| **other** | bridleway, cycleway, unclassified, unknown | `unclassified` |

---

## Centerline computation (step 3)

**Corridor collapse** (motorway / trunk / primary):
1. Build a spatial index of all major segments.
2. Link any two segments whose geometries are within `CLUSTER_DIST` (50 m) **and** whose bearings are parallel (within `--angle-tol`).
3. Use union-find to form connected-component clusters.
4. For each cluster: sample 200 equally-spaced points along the longest line; average in the nearest point from every other line within 50 m → single centerline.

**Carriageway collapse** (all other roads):
1. For every one-way segment, search for the nearest opposite-direction one-way segment of the same type within `--pair-dist` metres.
2. Pairing requires: same fclass, bearing difference ≈ 180 °, matching ref/name when present, length ratio in [0.2, 5.0].
3. Orient both lines in the same direction; interpolate 100 points on each; average coordinates → centerline.

---

## Running the pipeline

**Basic (all defaults):**
```bash
python road_pipeline.py --shp roads_OSM/roads_OSM.shp
```
Writes `cleaned_roads.shp` in WGS84 (EPSG:4326) in the current directory.

**Custom output and tuned parameters:**
```bash
python road_pipeline.py \
    --shp         roads_OSM/roads_OSM.shp \
    --out         output/israel_roads_clean.shp \
    --pair-dist   40 \
    --angle-tol   30 \
    --gap-dist    20
```

**Skip optional steps:**
```bash
# skip gap bridging (faster, if connectivity is already good)
python road_pipeline.py --shp roads_OSM/roads_OSM.shp --no-bridge

# skip roundabout handling
python road_pipeline.py --shp roads_OSM/roads_OSM.shp --no-roundabout

# skip both
python road_pipeline.py --shp roads_OSM/roads_OSM.shp --no-bridge --no-roundabout
```

### CLI reference

| Argument | Default | Description |
|----------|---------|-------------|
| `--shp` | *(required)* | Input shapefile (or any OGR-readable format) |
| `--out` | `cleaned_roads.shp` | Output shapefile path |
| `--pair-dist` | `35.0` | Max edge-to-edge distance (m) to consider two one-way segments as opposite carriageways (step 3) |
| `--angle-tol` | `25.0` | Bearing tolerance (°) for parallel / opposite-direction tests (step 3) |
| `--gap-dist` | `15.0` | Max gap (m) to bridge between likely continuation segments (step 5) |
| `--gap-angle` | `20.0` | Max bearing difference (°) for gap-bridge alignment check (step 5) |
| `--roundabout-circ` | `0.70` | Minimum isoperimetric circularity ratio for roundabout detection (step 8) |
| `--short-service-m` | `200.0` | Service roads shorter than this (m) are removed (step 9) |
| `--no-bridge` | off | Skip steps 5/6 (gap bridging and junction handling) |
| `--no-roundabout` | off | Skip step 8 (roundabout handling) |

---

## Processing report

After every step the pipeline prints:

```
  Remaining: 210,430  |  Removed: 8,412  |  Merged: 0  |  Change: -3.8%
```

At the end a full summary table is printed, showing the feature count, removal, merge count, and percentage change for every step — and identifying which step caused the largest reduction.

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

Features: map view (pydeck WebGL), road-type / speed / direction filters, charts (type distribution, speed, length, one-way ratio), and CSV export.

---

## Parameter tuning guide

- **`--pair-dist`** — Israeli divided highways have physical separations of roughly 10–30 m. Default 35 m provides margin for GPS drift. Raise to 50 m if rural highways are missing their pairs; lower to 20 m in dense urban areas to avoid false pairings.
- **`--angle-tol`** — 25 ° handles gently curved roads. Raise to 35–40 ° for sinuous roads; lower to 15 ° for strict urban grids.
- **`--gap-dist`** — 15 m bridges digitisation gaps at intersections. Raise cautiously; too large a value connects roads that should be separate.
- **`--roundabout-circ`** — 0.70 is conservative. Raise toward 0.85 to only detect near-perfect circles; lower to 0.55 to also catch elongated traffic islands.
- **`--short-service-m`** — 200 m removes parking-lot stubs and dead-end driveways. Lower to 100 m to be more conservative; raise to 500 m to aggressively prune service roads.

---

## Output

The output shapefile contains cleaned LineString geometries in **WGS84 (EPSG:4326)**.  
All internal processing columns (prefixed `_`) are stripped.  
The following attributes are always preserved from the original data:

| Attribute | Description |
|-----------|-------------|
| `ref` | Road reference number (route identifier) |
| `tunnel` | Tunnel flag (`T` = tunnel, `F` = surface) |
| `fclass` | Consolidated road type (see fclass grouping table) |

All distance-based processing runs in ITM (EPSG:2039) metres and is reprojected only at save time.
