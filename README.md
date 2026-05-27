# GeoAI Mini Project — OSM Israel Road Network Cleaning

A Python pipeline that cleans and simplifies OpenStreetMap road data for Israel.  
The goal is to produce a single, non-redundant centerline network from raw OSM exports, while preserving the dual-carriageway structure of major divided highways.

---

## Problem

Raw OSM road exports contain several data quality issues that make network analysis difficult:

- **Fragmented segments** — a single road is split into dozens of short pieces at every OSM node
- **Duplicate geometries** — the same road drawn twice from different import sources
- **Parallel carriageways** — divided roads are modelled as two separate one-way lines; most analysis requires a single centerline
- **Mixed coordinate systems** — OSM exports in WGS84 (degrees), unsuitable for distance and buffer operations

The pipeline resolves all four issues in sequence.

---

## Repository layout

```
MiniProject/
├── roads_OSM/
│   └── roads_OSM.shp       # raw OSM export for Israel
├── MiniProject.py           # Step 1 — exploratory inspection of the shapefile
├── road_pipeline.py         # Main cleaning pipeline (steps 1–8)
└── README.md
```

---

## Pipeline overview

```
Input shapefile
      │
      ▼
[1] Load & reproject  ──────────────────── WGS84 / any CRS → ITM (EPSG:2039)
      │
      ▼
[2] Clean geometries  ──────────────────── drop null / empty, fix invalid,
      │                                    explode multi-parts, keep LineStrings only
      ▼
[3] Normalize attributes  ──────────────── parse oneway, lanes, highway to
      │                                    typed internal columns
      ▼
[4] Classify roads  ────────────────────── tag each segment:
      │                                      keep_dual  = True  → dual carriageway kept
      │                                      keep_dual  = False → centerline candidate
      ▼
[5] Collapse parallel carriageways  ─────── pair opposite-direction one-way segments
      │                                     → replace each pair with one centerline
      ▼
[6] Merge fragmented segments  ──────────── linemerge touching pieces of the same road
      │
      ▼
[7] Remove near-duplicates  ─────────────── Hausdorff-distance deduplication
      │
      ▼
[8] Save  ───────────────────────────────── .gpkg (default) or .shp
```

### keep_dual classification rules

A road segment keeps its separate carriageways when **any** of these hold:

| Condition | OSM evidence |
|-----------|-------------|
| `highway` ∈ `{motorway, motorway_link}` | always a physical divider |
| lanes per direction **> 3** | large multi-lane road |
| `dual_carriageway = yes` or `divided = yes` | explicit tag |

Everything else that is mapped as a one-way pair gets collapsed to a single centerline.

### Centerline computation (step 5)

For each matched pair of opposite-direction one-way segments:
1. Orient both lines in the same direction (reverse the second if needed)
2. Interpolate **100 equally-spaced points** along each line
3. Average corresponding point coordinates → midpoint sequence
4. Simplify with tolerance 1 m (removes interpolation noise)

Pairing criteria (all must hold):
- Same `highway` type
- Bearing difference ≈ 180 ° (within `--angle-tol` degrees)
- Edge-to-edge distance < `--pair-dist` metres
- `ref` tag matches when present on both
- `name` tag matches when present on both
- Length ratio in \[0.2 , 5.0\]

---

## Exploration script

[MiniProject.py](MiniProject.py) prints a full profile of any shapefile:

```bash
python MiniProject.py --shp roads_OSM/roads_OSM.shp
```

Output includes: shape, CRS, geometry health, column null-rates, highway type breakdown, oneway / lanes distributions, and CRS advice.  Run this first to understand the data before tuning pipeline parameters.

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
    --shp      roads_OSM/roads_OSM.shp \
    --out      output/israel_roads_clean.shp \
    --pair-dist 40 \
    --angle-tol 30 \
    --dedup-thr 3
```

**Skip optional steps:**
```bash
# inspect collapse result before deduplication
python road_pipeline.py --shp roads_OSM/roads_OSM.shp --no-dedup

# skip both merge and dedup (fastest, for debugging)
python road_pipeline.py --shp roads_OSM/roads_OSM.shp --no-merge --no-dedup
```

### CLI reference

| Argument | Default | Description |
|----------|---------|-------------|
| `--shp` | *(required)* | Input shapefile (or any OGR-readable format) |
| `--out` | `cleaned_roads.shp` | Output shapefile path |
| `--pair-dist` | `35.0` | Max edge-to-edge distance (m) to consider two one-way segments as opposite carriageways |
| `--angle-tol` | `25.0` | Bearing tolerance (°) for the "opposite direction" test |
| `--dedup-thr` | `3.0` | Hausdorff distance threshold (m) for duplicate removal |
| `--no-merge` | off | Skip step 6 (segment merging) |
| `--no-dedup` | off | Skip step 7 (near-duplicate removal) |

---

## Dependencies

```bash
pip install geopandas shapely pandas
```

| Package | Purpose |
|---------|---------|
| `geopandas` | spatial I/O, CRS reprojection, spatial indexing |
| `shapely` | geometry operations (`linemerge`, `make_valid`, `hausdorff_distance`) |
| `pandas` | attribute table manipulation |

---

## Parameter tuning guide

- **`--pair-dist`** — Israeli divided highways have physical separations of roughly 10–30 m. Default 35 m provides margin for GPS drift in OSM edits. Raise to 50 m if rural highways are missing their pairs; lower to 20 m in dense urban areas to avoid false pairings.
- **`--angle-tol`** — 25 ° handles gently curved roads. Raise to 35–40 ° for highly sinuous roads; lower to 15 ° for strictly straight urban grids where false pairings are a risk.
- **`--dedup-thr`** — 3 m catches re-digitised duplicates. Raise with caution: parallel city streets can be as close as 10 m.

---

## Output

The output shapefile contains cleaned LineString geometries with all original OSM attributes preserved (internal processing columns prefixed `_` are stripped).  The CRS is **WGS84 geographic (EPSG:4326)** — coordinates in decimal degrees, compatible with web maps and GIS tools out of the box.  All distance-based processing runs internally in ITM (EPSG:2039) metres and is reprojected only at save time.
