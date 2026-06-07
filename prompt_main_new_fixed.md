# OSM Road Network Processing Pipeline — Clarified Workflow

## General Rules

- Convert data to the user-specified projected CRS. Default is UTM Zone 36N (EPSG:32636).
  The `--crs` argument accepts any EPSG code.
- Use only the `fclass`, `ref`, `tunnel`, and `class` columns for processing logic.
  Keep all other original fields in the output without modifying them.
- After each processing part, save the layer to the `intermediate_data/` folder as a shapefile.
- `tunnel = T` segments may only merge with other `tunnel = T` segments.
  A tunnel segment never merges with a non-tunnel segment.
- The final output is saved in the `final/` folder as a shapefile.

---

## Class Hierarchy

The `class` column is derived from `fclass` using the following mapping (highest to lowest rank):

| Class | fclass values |
|---|---|
| Highway (rank 1) | primary, motorway, trunk |
| Residential (rank 2) | residential, secondary, pedestrian, tertiary, service, living_street |
| Paths (rank 3) | footway, path, steps |
| Track (rank 4) | track, track_grade1, track_grade2, track_grade3, track_grade4, track_grade5 |
| Other (rank 5) | bridleway, unclassified, unknown |
| Bike (rank 6) | cycleway |
| traffic circle (rank 7) | detected automatically (see Part 1) |

When two roads are merged or one must be chosen over the other, **hierarchy always wins**:
the road with the higher-rank class (lower rank number) provides the attributes.
If both roads have the same class, the longer road's attributes are kept.

---

## Part 1 — Preprocess
**Output:** `intermediate_data/OSM_roads_preprocess.shp`

### Steps

1. Load the input shapefile and reproject to the target CRS (default EPSG:32636).

2. Remove all features where `fclass` contains the substring `"link"`
   (e.g. primary_link, motorway_link) or where `fclass = "busway"`.

3. Add a `class` column by mapping `fclass` through the hierarchy table above.
   Any unmapped fclass value is assigned class `"Other"`.

4. **Detect traffic circles:**
   - Build a graph of line endpoints.
   - Trace connected components to find closed loops of line segments.
   - For each candidate loop compute:
     - Isoperimetric quotient: `Q = 4π × area / perimeter²`
     - Bounding-circle radius: `r = sqrt(area / π)`
   - If `Q ≥ 0.70` AND `r ≤ 50 m`, set `class = "traffic circle"` for all
     segments in that loop.
   - Loops of 1 to 10 segments are typical; any count is allowed.

5. Save to `intermediate_data/OSM_roads_preprocess.shp`.

---

## Part 2 — Merge Lines
**Output:** `intermediate_data/OSM_roads_merge.shp`

### Concept

Iteratively merge pairs of line segments whose endpoints touch and whose
angle is close to 180° (nearly straight-through). Repeat until no new merges
are possible.

### Definitions

- **Touching:** Two lines touch when a start or end vertex of one line coincides
  with a start or end vertex of the other, within a 0.5 m rounding tolerance
  (applied after projection).

- **Angle measurement:** At the shared junction, compute the bearing of each line
  using its **second vertex** (the vertex just inside the line, next to the junction).
  Two lines form a straight-through pair when their toward-junction bearings are
  approximately 180° apart.

- **Merge tolerance:** A pair is eligible for merging when the deviation from 180°
  is ≤ 10°.

### Attribute rule (clarified)

**Hierarchy always wins.** When merging two lines of different classes, the
higher-rank class (and its associated `ref`, `tunnel`, and other attributes)
is kept regardless of segment length. If both lines have the same class, the
longer segment's attributes are kept.

### Tunnel rule (clarified)

`tunnel = T` segments may only merge with other `tunnel = T` segments.
A tunnel segment and a non-tunnel segment at the same junction are never merged.

### Junction cases

| Lines at junction | Rule |
|---|---|
| 2 lines | Merge if the best pair deviation ≤ 10° |
| 3 lines — Y (no pair within 10°) | Do **not** merge any pair |
| 3 lines — T (one pair within 10°) | Merge the pair closest to 180° |
| 4 lines — X or + | Merge the single pair closest to 180° (within 10°) |
| 5 or more lines | Find the pair closest to 180° (within 10°) and merge it |

Traffic circle segments (`class = "traffic circle"`) are **never** merged.

### Iteration

After each pass, rebuild the junction graph and search for new mergeable pairs.
Stop when no new merges are found in a full pass.

### After merging

Recalculate `length_m` (geometry length in metres) and `length_km`.
Save to `intermediate_data/OSM_roads_merge.shp`.

---

## Part 3 — Parallel Roads
**Output:** `intermediate_data/OSM_roads_merge_paralle.shp`

### Problem

Many roads are represented by 2 or more parallel line features (e.g. dual
carriageways, divided roads). This part collapses parallel pairs into a single
representative feature.

### Detection

Two roads are considered parallel when:
- Their overall bearings differ by ≤ 20° (same or opposite direction), AND
- Their lateral separation at the midpoint of the shorter road is ≤ 15 m.

### Collapse rules

**Rule 1 — Different class ranks:**
Keep the higher-rank road unchanged. Delete the lower-rank road.

**Rule 2 — Same class rank:**
Keep the longer road unchanged. Delete the shorter road.

### Y-split (divided highway fork) — clarified

A Y-split is when a road divides into two symmetric branches heading toward
the same intersection or traffic circle — like a divided highway approaching
a junction.

Detection: two segments share a common endpoint (the fork point) AND their
bearings at that point differ by ≤ 30° (they diverge in similar directions).

Action: find the incoming **stem** road (the segment arriving at the fork point
from the other side). Extend the stem to the midpoint between the far ends of
the two fork arms. Delete both fork arms.

### After processing

Recalculate `length_m` and `length_km`.
Save to `intermediate_data/OSM_roads_merge_paralle.shp`.

---

## Part 4 — Fix Traffic Circle Connections
**Output:** `intermediate_data/OSM_roads_merge_paralle_circle.shp`

### Goal

Remove all traffic circles and connect the approaching roads at a single
central point.

### Steps

For each traffic circle (group of connected `class = "traffic circle"` segments):

1. Compute the **centroid** of the circle geometry as the connection point.
2. Find all road segments that intersect or touch the circle geometry
   (using a 1 m buffer to catch near-touches).
3. For each connecting road: extend its endpoint that is nearest to the
   circle geometry to the centroid (add the centroid as the new endpoint vertex).
4. Delete all segments with `class = "traffic circle"`.

### After processing

Recalculate `length_m` and `length_km`.
Save to `intermediate_data/OSM_roads_merge_paralle_circle.shp`.

---

## Part 5 — Remove Short Roads
**Output:** `final/OSM_roads_clean.shp`

### Rule (clarified)

Remove any road segment that meets **both** conditions:

1. Has **exactly 1** connection point with the rest of the network —
   meaning one endpoint touches another road's endpoint or interior,
   but the other endpoint connects to nothing (dead-end stub).
2. Is shorter than **100 m**.

Roads with 0 connections (isolated), or 2+ connections (through-roads),
are kept regardless of length.

### After processing

Recalculate `length_m` and `length_km`.
Save the final result to `final/OSM_roads_clean.shp`
(or the path given by the `--out` argument).

---

## Running the Pipeline

```
python road_pipeline.py --shp "OSM Data/roads_OSM.shp"
python road_pipeline.py --shp input.shp --crs 32636 --out final/output.shp
```

| Argument | Default | Description |
|---|---|---|
| `--shp` | *(required)* | Input shapefile path |
| `--crs` | `32636` | EPSG code for projected CRS (UTM36N) |
| `--out` | `final/OSM_roads_clean.shp` | Final output path |

---

## Clarifications Resolved During Design

| Question | Answer |
|---|---|
| Hierarchy vs. length when merging — which wins? | **Hierarchy always wins.** Length only breaks ties within the same class. |
| Tunnel merge rule | `tunnel = T` merges only with `tunnel = T`. Never with non-tunnel. |
| Parallel roads collapse rule | Higher-rank wins; same rank → keep longer, drop shorter. No centerline averaging. |
| "1 intersection point" in Part 5 | Roads that connect to the network at **exactly 1 endpoint** (dead-end stubs). |
| Input CRS / coordinate system | CLI accepts `--crs` EPSG code; default is 32636 (UTM Zone 36N). |
| Y-split scenario in Part 3 | A divided highway fork: two mirrored branches sharing a common stem endpoint, diverging in similar directions. Extend the stem, delete both arms. |
