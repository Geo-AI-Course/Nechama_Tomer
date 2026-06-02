\# OSM Road Network Cleanup and Consolidation



\## Objective



Process an OSM-derived road network layer and create a cleaned, consolidated output layer suitable for network analysis. The goal is to reduce redundant geometries, merge connected road segments, and preserve important road classifications.



\---



\# Important Attributes to Preserve



Always preserve the following attributes from the original layer:



\* `ref`

\* `tunnel`



When multiple features are merged, preserve the highest-priority `fclass` according to the rules defined below.



\---



\# Step 1 – Remove Unwanted Classes



Remove all features where:



\* `fclass` contains `"link"`

\* `fclass = "busway"`



\---



\# Step 2 – Preserve Major Roads



Use the `ref` field to identify highways and major roads.



Roads containing route numbers in the `ref` field should be treated as important network features and preserved whenever possible.



\---



\# Step 3 – Create Single-Direction Representation



Convert divided roadways into a single logical representation.



Rules:



\* Keep only one direction of travel.

\* Do not force creation of a centerline if opposing carriageways are separated by large distances.

\* Maintain the geometry that best represents the road corridor.



\---



\# Step 4 – Merge Touching Segments



Merge road segments when:



\* The end vertex of one segment touches the start vertex of another segment.

\* The segments belong to the same logical roadway.



\---



\# Step 5 – Connect Likely Continuations



Identify disconnected segments that appear to represent the same road.



\## Direction Analysis



For each segment:



1\. Calculate the direction at the first vertex.

2\. Calculate the direction at the last vertex.



Use these directional measurements to identify likely continuations.



\## Connection Rules



Connect segments when:



\* Their endpoints are near each other.

\* Their directional alignment indicates they are part of the same roadway.

\* They represent the most likely continuation compared to nearby alternatives.



\---



\# Step 6 – Junction Handling



When two or more segments terminate at the same location:



\* Evaluate the geometry and road continuity.

\* Prefer connections that create the most logical road network.

\* If multiple valid continuations exist, connect all segments that reasonably belong to the same roadway.



\---



\# Step 7 – Remove Redundant Paths



For features with:



\* `fclass = footway`

\* `fclass = path`

\* `fclass = cycleway`



Remove the feature if:



\* It follows a similar route to another road category.

\* It is approximately parallel to that road.

\* It lies within 50 meters of that road.



Retain the higher-level road feature.



\---



\# Step 8 – Handle Traffic Circles / Roundabouts



Detect groups of segments that:



\* Form a complete circle.

\* Form a near-circle.

\* Represent a traffic circle or roundabout.



For roads intersecting the roundabout:



\* Identify opposite or near-opposite incoming roads.

\* Create logical through-road connections between them.

\* Preserve overall network connectivity.



\---



\# Step 9 – Remove Short Service Roads



After all merging operations, remove features where:



\* `fclass = service`

\* Length < 200 meters



\---



\# Special Handling: Tunnels



Identify tunnels using:



```text

tunnel = T

```



Tunnel features must:



\* Be processed separately from non-tunnel roads.

\* Never be merged with non-tunnel features.

\* Be merged only with other tunnel segments belonging to the same tunnel.

\* Produce a single-direction representation.



Preserve the `tunnel` attribute in the final output.



\---



\# FClass Grouping



\## Highway



\* primary

\* motorway

\* trunk



\## Residential



\* residential

\* secondary

\* pedestrian

\* tertiary

\* service

\* living\_street



\## Paths



\* footway

\* path

\* steps



\## Other



\* bridleway

\* cycleway

\* unclassified

\* unknown



\## Track



\* track

\* track\_grade1

\* track\_grade2

\* track\_grade3

\* track\_grade4

\* track\_grade5



\---



\# FClass Assignment After Merge



When merging features within a subgroup, assign the subgroup's representative class.



| Subgroup    | Output fclass |

| ----------- | ------------- |

| Highway     | primary       |

| Residential | residential   |

| Paths       | path          |

| Other       | unclassified  |

| Track       | track         |



\---



\# Reporting Requirements



Generate a processing report that tracks the effect of every step.



\## Initial Statistics



\* Number of input features



\## After Each Processing Step



Report:



\* Number of remaining features

\* Number of removed features

\* Number of merged features

\* Percentage change from the previous step



\## Final Statistics



Report:



\* Total input features

\* Total output features

\* Total features removed

\* Total merges performed

\* Largest reduction step



The report should clearly identify which processing stages caused the greatest changes to the network.



\---



\# Output Requirements



The final output layer must:



\* Preserve the `ref` attribute.

\* Preserve the `tunnel` attribute.

\* Contain a simplified, consolidated representation of the road network.

\* Remove redundant and duplicate road representations.

\* Maintain logical connectivity throughout the network.



