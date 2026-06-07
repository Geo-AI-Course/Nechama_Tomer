-	Convert the data to WGS84 UTM zone, for this data that is  UTM36 North (EPSG:32636)
-	Only use the fclass, ref, tunnel and class columns  (class will be calulated) keep other fields just don’t use them.
-	After each processing part save the layer in the intermediate_data folder as a shapefile
-	Field Tunnel = T, is a special case and the roads should not be merged
Part 1 – preprocess data
1.	Remove all fclass that include "link" in the name and busway
2.	Add column called class and reclassify the fclass in that column as follows:
Highway = 'primary', 'motorway', 'trunk'
Residential = 'residential', 'secondary', 'pedestrian', 'tertiary', 'service', 'living_street' 
Paths = 'footway', 'path', 'steps'
Track = 'track', 'track_grade1', 'track_grade2', 'track_grade3', 'track_grade4', 'track_grade5' 
Other = 'bridleway', 'unclassified', 'unknown'
Bike = 'cycleway'
This is a hierarchical classification as follows: Highway, Residential, Paths, Track, Other, Bike
3.	Find traffic circles
Traffic circles are very common inside citys and on some small roads between citys. However, for this project we don’t need the traffic circles.  
A traffic circle is when lines create a circle or a shape that is similar to a circle or oval. Most traffic circles are created from 1 to 10 lines (but could be more). The radius of a traffic circle is less than 50m. when a traffic circle is detected change class values to "traffic circle"

Save the current roads layer in the intermediate_data folder as OSM_roads_preprocess
Part2 – merge lines 
Merge all lines that the start or end vertex are touching the start or end  vertex by the fooling rules:
-	When merging keep the data from the longest road and the highest hierarchy. 
-	2 lines are considered touching if the degree between the lines is close to 180 degrees but up to a 10 degree deviation (+-10) from 180 is allowed. 
-	If there is only 2-lines that are touching at one point merge the lines.  
-	If there is a Y intersection do not merge the lines (3 lines with 1 connection point).
-	If there is a T intersection merge the lines that are the closest to 180 degree between the two lines and up to a 10 degree deviation (+-10) from 180, and calculating the degree between the lines should be done using the next vertex from the connection point.  
-	If there are 4 lines that connect at 1 point creating a X or +  (plus) intersection, merge the lines that are the closest to 180 degree between the two lines and up to a 10 degree deviation (+-10) from 180, and calculating the degree between the lines should be done using the next vertex from the connection point.  
-	If there are more than 5 lines connecting at the same point look for lines that are the closest to 180 degree between the two lines and up to a 10 degree deviation (+-10) from 180, and calculating the degree between the lines should be done using the next vertex from the connection point and merge them.
After merging all possible lines recalculate the lines length in m and km in the length_m and length_km fields.
Save the current roads layer in the intermediate_data folder as OSM_roads_merge
Part 3 – deal with "parallel" roads
Many roads have 2 or more lanes, and the roads layer has 2 or more features representing the road. We want to have 1 feature to represent each road and remove roads that are "parallel" and keep only one road. The roads will not be exactly parallel but the general direction of the roads are the same. 
If there are roads that are "parallel" always keep the higher rank for example if there is a Residential and bike or footway, keep the Residential  and delete the rest. 
If the distance between the "parallel" roads is less than 10m for most of the distance create a center line keeping the main data from the lines and delete the original lines, otherwise keep the longer line and delete the shorter line.
When there is a Y intersection where the Y part have the same shape but to the opposite direction indicating a intersection extend the longest road (the base of the Y) to the road that is perpendicular or to the traffic circle.
recalculate the lines length in m and km in the length_m and length_km fields.
Save the current roads layer in the intermediate_data folder as OSM_roads_merge_paralle
Part 4 – fix traffic circle connections to roads
we want to remove the traffic circles and want to connect the roads at the "center" of the circle. To do this find the roads that intersect with the traffic circle  and extend all the roads that get to the circle and have them meet in the middle or in the best optimized spot to connect the roads.

recalculate the lines length in m and km in the length_m and length_km fields.
Save the current roads layer in the intermediate_data folder as OSM_roads_merge_paralle_circle
Part 5 – remove short roads
Any road that has only 1 intersection point and is less than 100m
recalculate the lines length in m and km in the length_m and length_km fields.
Save final output in the final folder as a shapefile
