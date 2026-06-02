#!/usr/bin/env python3
"""
Israel OSM Road Network Dashboard
Interactive Streamlit app for exploring the cleaned Israel road network.
"""

import streamlit as st
import geopandas as gpd
import pandas as pd
import numpy as np
import pydeck as pdk
import plotly.express as px
import plotly.graph_objects as go
from pathlib import Path

# ── Page configuration ─────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Israel Road Network",
    page_icon="🛣️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .main-title {
        font-size: 2.2rem;
        font-weight: 700;
        color: #1a1a2e;
        margin-bottom: 0;
    }
    .subtitle {
        font-size: 0.95rem;
        color: #666;
        margin-top: 0;
        margin-bottom: 1.5rem;
    }
    .metric-label { font-size: 0.8rem !important; color: #888; }
    div[data-testid="stMetricValue"] { font-size: 1.6rem !important; font-weight: 700; }
    .legend-item { display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 0.85rem; }
    .legend-dot {
        width: 14px; height: 14px; border-radius: 50%;
        display: inline-block; flex-shrink: 0;
    }
    .stAlert { border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

# ── Constants ──────────────────────────────────────────────────────────────────
DATA_PATH = Path(r"D:\תומר\GeoAI\MiniProject\Nechama_Tomer\Exported_data\cleaned_roads.shp")

# Road type → [R, G, B] color mapping
ROAD_COLORS = {
    "motorway":         [220, 20,  60],
    "motorway_link":    [220, 20,  60],
    "trunk":            [255, 100, 0],
    "trunk_link":       [255, 130, 40],
    "primary":          [255, 180, 0],
    "primary_link":     [255, 200, 50],
    "secondary":        [50,  200, 80],
    "secondary_link":   [80,  210, 110],
    "tertiary":         [30,  160, 220],
    "tertiary_link":    [70,  180, 230],
    "residential":      [100, 130, 220],
    "living_street":    [150, 160, 230],
    "service":          [180, 180, 200],
    "unclassified":     [160, 160, 160],
    "track":            [160, 120, 60],
    "track_grade1":     [170, 130, 70],
    "track_grade2":     [175, 135, 75],
    "track_grade3":     [180, 140, 80],
    "track_grade4":     [185, 145, 85],
    "track_grade5":     [190, 150, 90],
    "footway":          [80,  200, 120],
    "path":             [60,  180, 100],
    "cycleway":         [0,   210, 210],
    "steps":            [200, 160, 100],
    "pedestrian":       [100, 200, 140],
    "bridleway":        [140, 100, 60],
}
DEFAULT_COLOR = [120, 120, 120]

# Road type categories for grouping in filters
ROAD_CATEGORIES = {
    "🚗 Major Roads":    ["motorway", "motorway_link", "trunk", "trunk_link",
                          "primary", "primary_link"],
    "🏘️ Urban Roads":    ["secondary", "secondary_link", "tertiary", "tertiary_link",
                          "residential", "living_street", "unclassified"],
    "🔧 Service Roads":  ["service"],
    "🌿 Off-road/Track": ["track", "track_grade1", "track_grade2", "track_grade3",
                           "track_grade4", "track_grade5"],
    "🚶 Pedestrian":     ["footway", "path", "steps", "pedestrian", "bridleway"],
    "🚴 Cycling":        ["cycleway"],
}


# ── Data loading ───────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def load_data() -> gpd.GeoDataFrame:
    gdf = gpd.read_file(DATA_PATH)
    gdf["name"] = gdf["name"].fillna("Unnamed")
    gdf["ref"]  = gdf["ref"].fillna("")
    gdf["maxspeed"] = gdf["maxspeed"].astype(int)
    return gdf


@st.cache_data(show_spinner=False)
def build_map_records(_df: pd.DataFrame) -> list[dict]:
    """Convert GeoDataFrame rows to pydeck PathLayer records."""
    records = []
    for row in _df.itertuples(index=False):
        coords = [[c[0], c[1]] for c in row.geometry.coords]
        records.append({
            "path":      coords,
            "color":     ROAD_COLORS.get(row.fclass, DEFAULT_COLOR),
            "name":      row.name,
            "fclass":    row.fclass,
            "maxspeed":  int(row.maxspeed),
            "length_km": round(float(row.length_km), 3),
            "oneway":    "One-way" if row.oneway == "F" else "Bidirectional",
            "ref":       row.ref if row.ref else "-",
        })
    return records


# ── Sidebar ────────────────────────────────────────────────────────────────────
def render_sidebar(gdf: gpd.GeoDataFrame):
    st.sidebar.markdown("## 🔍 Filters")

    # ── Road type category quick-select ───────────────────────────────────────
    st.sidebar.markdown("**Road Categories**")
    all_types_in_data = set(gdf["fclass"].unique())
    category_states = {}
    for cat_label, cat_types in ROAD_CATEGORIES.items():
        has_any = bool(all_types_in_data & set(cat_types))
        default = cat_label in ("🚗 Major Roads", "🏘️ Urban Roads")
        category_states[cat_label] = st.sidebar.checkbox(
            cat_label, value=(default and has_any), disabled=not has_any
        )

    selected_from_cats = set()
    for cat_label, selected in category_states.items():
        if selected:
            selected_from_cats.update(ROAD_CATEGORIES[cat_label])

    # Fine-grained individual type picker (inside expander to save space)
    with st.sidebar.expander("Fine-tune road types"):
        available_types = sorted(all_types_in_data)
        # Pre-check types that belong to selected categories
        fine_defaults = [t for t in available_types if t in selected_from_cats]
        fine_selected = st.multiselect(
            "Individual road types",
            options=available_types,
            default=fine_defaults,
            label_visibility="collapsed",
        )
    selected_types = set(fine_selected) if fine_selected else selected_from_cats

    st.sidebar.divider()

    # ── Speed limit ───────────────────────────────────────────────────────────
    st.sidebar.markdown("**Speed Limit (km/h)**")
    min_spd, max_spd = int(gdf["maxspeed"].min()), int(gdf["maxspeed"].max())
    speed_range = st.sidebar.slider(
        "Speed range", min_spd, max_spd, (min_spd, max_spd), label_visibility="collapsed"
    )

    # ── Direction ─────────────────────────────────────────────────────────────
    st.sidebar.markdown("**Direction**")
    direction = st.sidebar.radio(
        "Direction filter",
        ["All", "Bidirectional", "One-way"],
        horizontal=True,
        label_visibility="collapsed",
    )

    # ── Name search ───────────────────────────────────────────────────────────
    st.sidebar.divider()
    name_search = st.sidebar.text_input("🔎 Search road name", placeholder="e.g. Herzl, Ibn Gabirol…")

    # ── Map performance ───────────────────────────────────────────────────────
    st.sidebar.divider()
    st.sidebar.markdown("**Map Performance**")
    max_features = st.sidebar.select_slider(
        "Max roads on map",
        options=[2_000, 5_000, 10_000, 20_000, 40_000, 80_000],
        value=10_000,
        help="Higher = richer map but slower rendering",
    )

    # ── Map style ─────────────────────────────────────────────────────────────
    map_style_options = {
        "Light (CARTO)":  "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        "Dark (CARTO)":   "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
        "Voyager (CARTO)":"https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
    }
    map_style_name = st.sidebar.selectbox("Map style", list(map_style_options.keys()))

    return {
        "selected_types": selected_types,
        "speed_range":    speed_range,
        "direction":      direction,
        "name_search":    name_search,
        "max_features":   max_features,
        "map_style":      map_style_options[map_style_name],
    }


# ── Filtering ──────────────────────────────────────────────────────────────────
def apply_filters(gdf: gpd.GeoDataFrame, opts: dict) -> gpd.GeoDataFrame:
    mask = pd.Series(True, index=gdf.index)

    if opts["selected_types"]:
        mask &= gdf["fclass"].isin(opts["selected_types"])

    mask &= gdf["maxspeed"].between(*opts["speed_range"])

    if opts["direction"] == "Bidirectional":
        mask &= gdf["oneway"] == "B"
    elif opts["direction"] == "One-way":
        mask &= gdf["oneway"] == "F"

    if opts["name_search"].strip():
        mask &= gdf["name"].str.contains(opts["name_search"].strip(), case=False, na=False)

    return gdf[mask].copy()


# ── Map ────────────────────────────────────────────────────────────────────────
def render_map(filtered: gpd.GeoDataFrame, max_features: int, map_style: str):
    st.markdown("### 🗺️ Map View")

    total = len(filtered)
    if total == 0:
        st.warning("No roads match the current filters. Adjust the sidebar settings.")
        return

    # Sample for performance
    if total > max_features:
        # Prioritise major roads in the sample
        priority_types = {"motorway", "trunk", "primary", "secondary", "tertiary"}
        major = filtered[filtered["fclass"].isin(priority_types)]
        minor = filtered[~filtered["fclass"].isin(priority_types)]
        n_major = min(len(major), max_features // 2)
        n_minor = min(len(minor), max_features - n_major)
        map_data = pd.concat([
            major.sample(n=n_major, random_state=42) if n_major else major.iloc[:0],
            minor.sample(n=n_minor, random_state=42) if n_minor else minor.iloc[:0],
        ])
        st.info(
            f"Showing **{len(map_data):,}** of **{total:,}** roads on the map "
            f"(major roads prioritised). Increase *Max roads on map* in the sidebar for more detail."
        )
    else:
        map_data = filtered

    # Build layer data
    records = build_map_records(map_data)

    layer = pdk.Layer(
        "PathLayer",
        data=records,
        get_path="path",
        get_color="color",
        width_min_pixels=1,
        width_scale=3,
        pickable=True,
        auto_highlight=True,
    )

    bounds = map_data.total_bounds
    view = pdk.ViewState(
        latitude=(bounds[1] + bounds[3]) / 2,
        longitude=(bounds[0] + bounds[2]) / 2,
        zoom=8,
        pitch=0,
    )

    tooltip = {
        "html": """
        <div style="background:#fff;border-radius:8px;padding:12px;
                    box-shadow:0 2px 8px rgba(0,0,0,.2);font-family:sans-serif;font-size:13px">
          <b style="font-size:14px">{name}</b><br/>
          <span style="color:#666">Type:</span> <b>{fclass}</b><br/>
          <span style="color:#666">Direction:</span> {oneway}<br/>
          <span style="color:#666">Max speed:</span> {maxspeed} km/h<br/>
          <span style="color:#666">Length:</span> {length_km} km<br/>
          <span style="color:#666">Ref:</span> {ref}
        </div>""",
        "style": {"padding": "0"},
    }

    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=view,
        tooltip=tooltip,
        map_style=map_style,
    )
    st.pydeck_chart(deck, use_container_width=True, height=520)

    # Color legend
    with st.expander("🎨 Map legend — road types"):
        shown_types = map_data["fclass"].unique()
        cols = st.columns(3)
        for i, fclass in enumerate(sorted(shown_types)):
            r, g, b = ROAD_COLORS.get(fclass, DEFAULT_COLOR)
            hex_color = f"#{r:02x}{g:02x}{b:02x}"
            with cols[i % 3]:
                st.markdown(
                    f'<div class="legend-item">'
                    f'<span class="legend-dot" style="background:{hex_color}"></span>'
                    f'{fclass}</div>',
                    unsafe_allow_html=True,
                )


# ── Charts ─────────────────────────────────────────────────────────────────────
def render_charts(filtered: gpd.GeoDataFrame):
    st.markdown("### 📊 Statistics")

    if len(filtered) == 0:
        return

    tab1, tab2, tab3, tab4 = st.tabs(
        ["Road Types", "Speed Limits", "Lengths", "Network Summary"]
    )

    # ── Tab 1: Road types ──────────────────────────────────────────────────────
    with tab1:
        col_a, col_b = st.columns([2, 1])
        with col_a:
            type_df = (
                filtered.groupby("fclass")
                .agg(count=("fclass", "size"), total_km=("length_km", "sum"))
                .reset_index()
                .sort_values("count", ascending=True)
                .tail(20)
            )
            type_df["color"] = type_df["fclass"].apply(
                lambda f: "#{:02x}{:02x}{:02x}".format(*ROAD_COLORS.get(f, DEFAULT_COLOR))
            )
            fig = px.bar(
                type_df,
                x="count", y="fclass",
                orientation="h",
                color="fclass",
                color_discrete_map={row.fclass: row.color for row in type_df.itertuples()},
                labels={"count": "Number of segments", "fclass": "Road type"},
                title="Segment count by road type",
            )
            fig.update_layout(showlegend=False, height=420, margin=dict(l=0, r=10, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

        with col_b:
            total_km_df = (
                filtered.groupby("fclass")["length_km"]
                .sum()
                .reset_index()
                .sort_values("length_km", ascending=False)
                .head(10)
            )
            fig2 = px.pie(
                total_km_df,
                values="length_km",
                names="fclass",
                title="Total km by road type (top 10)",
                hole=0.4,
            )
            fig2.update_traces(textposition="inside", textinfo="percent+label")
            fig2.update_layout(height=420, showlegend=False, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig2, use_container_width=True)

    # ── Tab 2: Speed limits ────────────────────────────────────────────────────
    with tab2:
        col_a, col_b = st.columns(2)
        with col_a:
            speed_counts = filtered["maxspeed"].value_counts().sort_index().reset_index()
            speed_counts.columns = ["Speed (km/h)", "Count"]
            fig = px.bar(
                speed_counts,
                x="Speed (km/h)", y="Count",
                title="Speed limit distribution",
                color="Count",
                color_continuous_scale="Blues",
            )
            fig.update_layout(height=380, margin=dict(l=0, r=10, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

        with col_b:
            speed_type = (
                filtered.groupby(["fclass", "maxspeed"])
                .size()
                .reset_index(name="count")
            )
            top_types = filtered["fclass"].value_counts().head(8).index.tolist()
            fig2 = px.box(
                filtered[filtered["fclass"].isin(top_types)],
                x="fclass", y="maxspeed",
                title="Speed limit by road type (top 8)",
                labels={"fclass": "Road type", "maxspeed": "Speed limit (km/h)"},
                color="fclass",
            )
            fig2.update_layout(
                height=380, showlegend=False,
                margin=dict(l=0, r=10, t=40, b=60),
                xaxis_tickangle=-30,
            )
            st.plotly_chart(fig2, use_container_width=True)

    # ── Tab 3: Lengths ─────────────────────────────────────────────────────────
    with tab3:
        col_a, col_b = st.columns(2)
        with col_a:
            clip_km = filtered["length_km"].clip(upper=2)
            fig = px.histogram(
                filtered.assign(length_km_clip=clip_km),
                x="length_km_clip",
                nbins=60,
                title="Road segment length distribution (clipped at 2 km)",
                labels={"length_km_clip": "Length (km)", "count": "Count"},
                color_discrete_sequence=["#4c9be8"],
            )
            fig.update_layout(height=380, margin=dict(l=0, r=10, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

        with col_b:
            len_by_type = (
                filtered[filtered["fclass"].isin(
                    filtered["fclass"].value_counts().head(10).index
                )]
            )
            fig2 = px.violin(
                len_by_type,
                x="fclass", y="length_km",
                title="Length distribution by road type (top 10)",
                labels={"fclass": "Road type", "length_km": "Length (km)"},
                color="fclass",
                box=True,
            )
            fig2.update_layout(
                height=380, showlegend=False,
                margin=dict(l=0, r=10, t=40, b=60),
                xaxis_tickangle=-30,
                yaxis_range=[0, filtered["length_km"].quantile(0.95)],
            )
            st.plotly_chart(fig2, use_container_width=True)

    # ── Tab 4: Network summary ─────────────────────────────────────────────────
    with tab4:
        col_a, col_b = st.columns(2)
        with col_a:
            oneway_map = {"B": "Bidirectional", "F": "One-way"}
            oneway_df = (
                filtered["oneway"]
                .map(oneway_map)
                .value_counts()
                .reset_index()
            )
            oneway_df.columns = ["Direction", "Count"]
            fig = px.pie(
                oneway_df,
                values="Count",
                names="Direction",
                title="Bidirectional vs One-way roads",
                color="Direction",
                color_discrete_map={"Bidirectional": "#4c9be8", "One-way": "#e8654c"},
                hole=0.45,
            )
            fig.update_layout(height=360, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

        with col_b:
            named_count  = (filtered["name"] != "Unnamed").sum()
            unnamed_count = (filtered["name"] == "Unnamed").sum()
            fig2 = px.pie(
                pd.DataFrame({"Status": ["Named", "Unnamed"], "Count": [named_count, unnamed_count]}),
                values="Count",
                names="Status",
                title="Named vs unnamed road segments",
                color="Status",
                color_discrete_map={"Named": "#5cb85c", "Unnamed": "#d9534f"},
                hole=0.45,
            )
            fig2.update_layout(height=360, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig2, use_container_width=True)

        # Top road names
        st.markdown("**Top 10 most-segmented named roads**")
        top_names = (
            filtered[filtered["name"] != "Unnamed"]
            .groupby("name")
            .agg(
                segments=("name", "size"),
                total_km=("length_km", "sum"),
                types=("fclass", lambda x: ", ".join(sorted(x.unique()))),
            )
            .reset_index()
            .sort_values("segments", ascending=False)
            .head(10)
            .rename(columns={"name": "Road name", "segments": "Segments",
                              "total_km": "Total km", "types": "Types"})
        )
        top_names["Total km"] = top_names["Total km"].round(2)
        st.dataframe(top_names, use_container_width=True, hide_index=True)


# ── Data table ─────────────────────────────────────────────────────────────────
def render_table(filtered: gpd.GeoDataFrame):
    st.markdown("### 📋 Data Table")
    display_cols = ["osm_id", "fclass", "name", "ref", "oneway", "maxspeed", "length_km", "layer"]
    rename_map = {
        "osm_id": "OSM ID", "fclass": "Road type", "name": "Name",
        "ref": "Ref", "oneway": "Direction", "maxspeed": "Speed (km/h)",
        "length_km": "Length (km)", "layer": "Layer",
    }

    table_df = (
        filtered[display_cols]
        .assign(
            oneway=filtered["oneway"].map({"B": "Bidirectional", "F": "One-way"}),
            length_km=filtered["length_km"].round(3),
        )
        .rename(columns=rename_map)
    )

    st.dataframe(table_df.head(1000), use_container_width=True, height=320, hide_index=True)

    if len(filtered) > 1000:
        st.caption(f"Showing first 1,000 of {len(filtered):,} rows.")

    csv_bytes = table_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="⬇️ Download filtered data as CSV",
        data=csv_bytes,
        file_name="filtered_israel_roads.csv",
        mime="text/csv",
        type="primary",
    )


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    st.markdown('<p class="main-title">🛣️ Israel Road Network Explorer</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="subtitle">Interactive dashboard for the cleaned Israel OpenStreetMap road network '
        '— 410,030 road segments, 28 road types, ~192,000 km total</p>',
        unsafe_allow_html=True,
    )

    # Load data
    with st.spinner("Loading road network data (one-time, then cached)…"):
        gdf = load_data()

    # Sidebar
    opts = render_sidebar(gdf)

    # Filter
    filtered = apply_filters(gdf, opts)

    # ── KPI metrics ────────────────────────────────────────────────────────────
    total_km   = filtered["length_km"].sum()
    named_pct  = (filtered["name"] != "Unnamed").mean() * 100
    avg_speed  = filtered["maxspeed"].mean()
    n_types    = filtered["fclass"].nunique()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Road segments", f"{len(filtered):,}")
    c2.metric("Total length",  f"{total_km:,.0f} km")
    c3.metric("Road types",    f"{n_types}")
    c4.metric("Named roads",   f"{named_pct:.1f}%")
    c5.metric("Avg speed limit", f"{avg_speed:.0f} km/h")

    st.divider()

    # Tabs for map / charts / table
    map_tab, chart_tab, table_tab = st.tabs(["🗺️ Map", "📊 Charts", "📋 Data"])

    with map_tab:
        render_map(filtered, opts["max_features"], opts["map_style"])

    with chart_tab:
        render_charts(filtered)

    with table_tab:
        render_table(filtered)


if __name__ == "__main__":
    main()
