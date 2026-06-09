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
   - A candidate qualifies as a traffic circle when both:
     - Isoperimetric quotient: `Q = 4π × area / perimeter² ≥ 0.90`
     - Bounding-circle radius: `r = sqrt(area / π) ≤ 50 m`
   - Detection runs in three passes:
     - **Pass 1 — Single-segment closed rings** (segments where start == end):
       compute Q and r directly from the enclosed polygon. (OSM often stores a
       complete roundabout as one self-closing way.)
     - **Pass 2 — Multi-segment closed loops:** for the remaining open-arc
       segments, trace connected components to find closed loops. At each
       junction, the next segment chosen is the one whose far endpoint is closest
       to the loop origin — this "heads home" and traces the tightest (most
       circular) path deterministically. Test Q and r on the loop's convex hull.
     - **Pass 3 — Near-complete open arcs:** chains whose remaining gap is less
       than **12.5 % of the full circumference** (`gap / (arc_length + gap) < 0.125`).
       The gap is bridged with a **fitted circular arc** (centre = convex-hull
       centroid, radius interpolated between the two free ends) so the closure
       continues the circular curvature rather than cutting a straight chord
       across the gap. Test Q and r on the completed shape.
   - **Output as a single feature:** each detected circle is emitted as **one
     merged line feature** — its arc segments (plus the fitted closing arc for
     near-complete circles) are stitched into a single line, tagged
     `class = "traffic circle"`, and the source segments are removed.
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

- **Merge tolerance (angle pass):** A pair is eligible for merging when the deviation
  from 180° is ≤ 10°.

- **Merge tolerance (ref pass):** A pair sharing the same non-empty `ref` value is
  eligible for merging when the deviation from 180° is ≤ 45°.

### Compound `ref` values

Some segments carry two road numbers separated by `":"` (e.g. `ref = "1:6"`).
Before merging begins, split each such segment into **one copy per road number**
(e.g. one copy with `ref = "1"` and one copy with `ref = "6"`).
Both copies have the same geometry and all other attributes.
Each copy then participates independently in the ref-based pass with its own
single road number.

### Two-phase merge order

Merging runs in two sequential phases, each iterated until convergence:

**Phase 1 — ref-based pass:**
At each junction, collect candidates that share the same non-empty `ref` value.
Pick the straightest such pair. Merge if deviation from 180° ≤ 45°.
Tunnel rule applies. Traffic circle segments are never merged.
Repeat until no new ref-based merges occur.

**Phase 2 — angle-based pass:**
Runs on all remaining segments after Phase 1.
Uses the standard ≤ 10° tolerance with no ref filter.
All existing junction rules apply (2-line, T, Y, X, 5+).
Repeat until convergence.

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
from the other side), then:

- If both fork arms terminate on a **common through-road** or on a **single
  traffic circle** (each arm's far end lies within 5 m of it), extend the stem
  from the fork point along the fork→midpoint direction **until it meets that
  line/circle**, and delete both fork arms. (For a circle the connector stops at
  the ring boundary; Part 4 later carries it on to the centroid.)
- Otherwise, extend the stem to the midpoint between the far ends of the two fork
  arms and delete both arms (fallback).

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

For each traffic circle (a single merged `class = "traffic circle"` line feature
from Part 1; any group of connected such segments is still tolerated):

1. Compute the **centroid** of the circle geometry as the connection point.
2. Find all road segments that intersect or touch the circle geometry
   (using a 1 m buffer to catch near-touches).
3. For each connecting road: extend its endpoint that is nearest to the
   circle geometry to the centroid (add the centroid as the new endpoint vertex).
4. Delete all segments with `class = "traffic circle"`.

### Merge through-roads at the centre (clarified)

Once the approaching roads all meet at the centroid they form a crossroads, so
merge the straight **through-road** pairs there using the same junction logic as
Part 2, with two differences:

- **Direction is judged by each road's general heading, not its first segment.**
  Measure the toward-junction bearing from a vertex about **3 vertices in from the
  centre** (rather than the immediately adjacent vertex). This ignores the short
  kink where a road bends into the roundabout, so two genuine through-arms read as
  ~180° apart.
- The standard **≤ 10° straight-through tolerance** then decides each merge.

Apply this to every centre point, treating each like an ordinary junction:

| Arms at the centre | Rule |
|---|---|
| 2 | Merge if deviation from 180° ≤ 10° |
| 3 — T (one pair within 10°) | Merge the straightest pair; the third arm stays as a branch |
| 3 — Y (no pair within 10°) | Do **not** merge any pair |
| 4 — X or + | Merge both opposite pairs, producing a proper crossroads |
| 5 or more | Repeatedly merge any straight-through pair until none remain |

The **hierarchy** and **tunnel** rules from Part 2 apply unchanged: the higher-rank
class supplies the merged attributes, and `tunnel = T` segments never merge with
non-tunnel segments.

### After processing

Recalculate `length_m` and `length_km`.
Save to `intermediate_data/OSM_roads_merge_paralle_circle.shp`.

---

## Part 5 — Remove Short Roads
**Output:** `final/OSM_roads_clean.shp`

### Rules (clarified)

**Rule 1 — Dead-end stubs:** Remove any road segment that meets **both** conditions:

1. Has **exactly 1** connection point with the rest of the network —
   meaning one endpoint touches another road's endpoint or interior,
   but the other endpoint connects to nothing (dead-end stub).
2. Is shorter than **100 m**.

**Rule 2 — Isolated segments:** Remove any road segment that meets **both** conditions:

1. Has **0** connections — neither endpoint touches any other road.
2. Is shorter than **200 m**.

Roads with 2+ connections (through-roads) are kept regardless of length.
Isolated roads ≥ 200 m and dead-end stubs ≥ 100 m are also kept.

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
| Traffic circle geometry output | Each detected circle is merged into **one** line feature; near-complete circles are closed with a **fitted circular arc** (continuing the curve), not a straight chord. |
| Merging at the traffic-circle centre (Part 4) | After connecting roads to the centroid, merge straight through-road pairs (X / + / T) there. Direction is judged by each road's **general heading** (~3 vertices in from the centre, to ignore the roundabout-entry kink), then the standard **≤ 10°** straight-through rule applies. |
