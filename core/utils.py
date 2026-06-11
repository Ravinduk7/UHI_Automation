import pystac_client
import planetary_computer
import geopandas as gpd
from shapely.geometry import shape
import io
import base64
import matplotlib.pyplot as plt
import contextily as cx
from docxtpl import DocxTemplate, InlineImage
from docx.shared import Cm
import requests
import pandas as pd
from datetime import datetime, timedelta
import numpy as np
import folium
import zipfile
from shapely.validation import make_valid
import re

def export_to_tiff_bytes(array_2d, spatial_ref_da):

    da = spatial_ref_da.copy(
        data=np.expand_dims(array_2d, axis=0).astype("float32")
    )

    buf = io.BytesIO()
    da.rio.to_raster(buf, driver="GTiff")

    return buf.getvalue()

def process_user_polygon(user_polygon):

    if not user_polygon or not isinstance(user_polygon, dict):
        raise ValueError("Invalid AOI data.")

    geom_type = user_polygon.get("type")

    if geom_type not in ["Polygon", "MultiPolygon"]:
        raise ValueError("Only Polygon/MultiPolygon allowed.")

    try:
        geom = shape(user_polygon)
    except Exception:
        raise ValueError("Invalid geometry structure.")

    # Fix invalid geometry early (VERY IMPORTANT)
    if not geom.is_valid:
        try:
            geom = make_valid(geom)
        except:
            geom = geom.buffer(0)

    # bbox from geometry (single source of truth)
    minx, miny, maxx, maxy = geom.bounds
    bbox = [minx, miny, maxx, maxy]

    # metric conversion
    gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326").to_crs(3857)

    area_sq_km = float(gdf.geometry.area.iloc[0]) / 1e6

    bounds = gdf.geometry.bounds.iloc[0]
    width_km = (bounds.maxx - bounds.minx) / 1000
    height_km = (bounds.maxy - bounds.miny) / 1000

    return bbox, area_sq_km, max(width_km, height_km)

def init_master_map(center_lat, center_lon, zoom_start=12, selected_basemap="Dark Mode Map"):
    """
    Initializes a Folium map with premium basemaps, forcing the selected_basemap
    to remain active across Streamlit component reruns.
    """
    # Initialize with tiles=None so we can manually control activation states
    m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom_start, tiles=None)

    basemaps = {
        "Satellite View": {
            "tiles": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            "attr": "Esri World Imagery"
        },
        "Dark Mode Map": {
            "tiles": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
            "attr": "CartoDB Dark Matter"
        },
        "Light Minimal": {
            "tiles": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
            "attr": "CartoDB Positron"
        },
        "OpenStreetMap": {
            "tiles": "OpenStreetMap",
            "attr": "OpenStreetMap"
        }
    }

    # Loop and set show=True ONLY for the user's selected base map
    for name, cfg in basemaps.items():
        folium.TileLayer(
            tiles=cfg["tiles"],
            attr=cfg["attr"],
            name=name,
            overlay=False,
            show=(name == selected_basemap)
        ).add_to(m)

    return m

def fetch_stac_data(bbox, year, collections=["landsat-c2-l2"]):
    """Queries Planetary Computer for cloud-free (<10%) satellite passes."""
    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    search = catalog.search(
        collections=collections,
        bbox=bbox,
        datetime=f"{year}-01-01/{year}-12-31",
        query={"eo:cloud_cover": {"lt": 10}}
    )
    return list(search.items())

def fig_to_base64(fig):
    """Converts a matplotlib figure to a base64 string to save RAM."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches='tight', pad_inches=0, transparent=True)
    plt.close(fig) 
    return base64.b64encode(buf.getvalue()).decode('utf-8')

def generate_basemap_b64(user_polygon):
    geom = shape(user_polygon)
    gdf = gpd.GeoDataFrame({'geometry': [geom]}, crs="EPSG:4326").to_crs(3857)

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.set_axis_off()
    fig.patch.set_alpha(0)

    gdf.boundary.plot(ax=ax, color="red", linewidth=2)
    cx.add_basemap(ax, crs=gdf.crs.to_string(), source=cx.providers.Esri.WorldImagery)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0, transparent=True)

    plt.close(fig)  # 🔥 IMPORTANT: put it HERE, not before save

    return base64.b64encode(buf.getvalue()).decode('utf-8')


def create_word_report(template_path, context):
    ctx = {**context}  # <-- Shielding the original context from mutation
    doc = DocxTemplate(template_path)

    def inject(img_source, width=15):
        if isinstance(img_source, str): # Handle Base64 strings
            img = io.BytesIO(base64.b64decode(img_source))
        else: # Handle active BytesIO buffers
            img = img_source
            img.seek(0)
        return InlineImage(doc, img, width=Cm(width))

    image_keys = [
        "heatmap_image", "rainfall_chart", "tier_chart", 
        "landcover_chart", "dem_map", "wc_map", "jrc_map"
    ]

    # Safely convert image placeholders ONLY if data exists
    for key in image_keys:
        if ctx.get(key):
            ctx[key] = inject(ctx[key])
        else:
            # 🛑 The Fix: Don't force a fake image. 
            # Just pass an empty string so Word renders nothing instead of crashing.
            ctx[key] = "⚠️ This image has not been generated. Contact the author for more information."

    doc.render(ctx)  # <-- Using the isolated context here

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

def get_dynamic_rain_events(lat, lon, years_back=3, num_tiers=5, backups_per_tier=3):
    """
    Scouts historical IMERG data and returns a list of Tiers. 
    Each Tier contains backup dates of similar severity to ensure satellite matches.
    """
    end_date = datetime.today() - timedelta(days=7)
    start_date = end_date - timedelta(days=365 * years_back)
    
    url = f"https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
        "daily": "precipitation_sum",
        "timezone": "auto"
    }
    
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()


    data = response.json()
    if "error" in data:
        raise ValueError(f"API Error: {data.get('reason')}")
        
    df = pd.DataFrame({
        "date": pd.to_datetime(data["daily"]["time"]),
        "precip_mm": data["daily"]["precipitation_sum"]
    })
    
    df['3_day_sum'] = df['precip_mm'].rolling(window=3).sum()
    df = df.dropna()
    
    # --- GET WET EVENTS (THE TIERED UPGRADE - MUTUALLY EXCLUSIVE BUCKETS) ---
    worst_storms = df.sort_values(by="3_day_sum", ascending=False)
    all_independent_peaks = []
    
    for index, row in worst_storms.iterrows():
        # Enforce the storm threshold here so our distribution is purely heavy rain
        if row['3_day_sum'] < 15.0:
            continue
            
        is_independent = all(abs((row['date'] - p['date']).days) > 14 for p in all_independent_peaks)
        if is_independent:
            all_independent_peaks.append(row)

    tiered_candidates = []
    storm_metadata = {} # NEW: The HUD stats dictionary!
    total_peaks = len(all_independent_peaks)
    
    if total_peaks >= num_tiers:
        # 1. Calculate dynamic severity boundaries (avoiding 0/100 edge cases)
        peak_values = [p['3_day_sum'] for p in all_independent_peaks]
        
        # For 5 tiers, this creates 4 internal boundaries: [80.0, 60.0, 40.0, 20.0]
        percentiles = [100 - (100 / num_tiers) * i for i in range(1, num_tiers)] 
        boundaries = [np.percentile(peak_values, q) for q in percentiles]
        
        remaining = all_independent_peaks.copy()
        original_peaks = all_independent_peaks.copy() # NEW: The un-depleted master list
        
        # Loop through the internal boundaries (Tiers 1 to num_tiers-1)
        for threshold in boundaries:
            # NEW: Count against the original full set for an honest return period
            historical_count = sum(1 for p in original_peaks if p['3_day_sum'] >= threshold)
            
            # <-- Fixed formula to return years, not days
            return_period = round(years_back / max(historical_count, 1), 1) if historical_count > 0 else years_back
            
            bucket = [p for p in remaining if p['3_day_sum'] >= threshold]
            selected = bucket[:backups_per_tier]
            tier_dates = [p['date'].strftime('%Y-%m-%d') for p in selected]\
            
            if tier_dates:
                tiered_candidates.append(tier_dates)
                
                # Save the rainfall and return period for the UI!
                for p in selected:
                    date_str = p['date'].strftime('%Y-%m-%d')
                    storm_metadata[date_str] = {
                        "rainfall_mm": round(p['3_day_sum'], 1),
                        "return_period_years": return_period 
                    }
                    
            remaining = [r for r in remaining if r['date'] not in [s['date'] for s in selected]]
            
        # The Final Tier (e.g., Tier 5): Whatever is left!
        if remaining:
            selected = remaining[:backups_per_tier]
            tier_dates = [p['date'].strftime('%Y-%m-%d') for p in selected]
            
            if tier_dates:
                tiered_candidates.append(tier_dates)
                
                # For the bottom tier, ALL original storms meet this baseline
                historical_count = len(original_peaks) 
                
                # <-- Fixed formula here too
                return_period = round(years_back / max(historical_count, 1), 1) if historical_count > 0 else years_back
                
                for p in selected:
                    date_str = p['date'].strftime('%Y-%m-%d')
                    storm_metadata[date_str] = {
                        "rainfall_mm": round(p['3_day_sum'], 1),
                        "return_period_years": return_period 
                    }
    else:
        # --- THE UPGRADED FALLBACK ---
        print(f"  [!] Fallback triggered: Generating synthetic 3-day backup clusters.")
        tiered_candidates = []
        for p in all_independent_peaks[:num_tiers]:
            peak_dt = p['date']
            # Create a 3-day backup cluster (Day Before, Peak Day, Day After)
            backup_cluster = [
                (peak_dt - timedelta(days=1)).strftime('%Y-%m-%d'),
                peak_dt.strftime('%Y-%m-%d'),
                (peak_dt + timedelta(days=1)).strftime('%Y-%m-%d')
            ]
            tiered_candidates.append(backup_cluster)
            
            # NEW: Fixed key and accurate fallback return period
            for d in backup_cluster:
                storm_metadata[d] = {
                    "rainfall_mm": round(p['3_day_sum'], 1),
                    "return_period_years": years_back 
                }
            
    # --- GET DRY CANDIDATES ---
    driest_periods = df.sort_values(by="3_day_sum", ascending=True)
    dry_candidates = []
    
    for index, row in driest_periods.iterrows():
        if row['3_day_sum'] > 10.0:
            continue
        is_independent = all(abs((row['date'] - d['date']).days) > 14 for d in dry_candidates)
        if is_independent:
            dry_candidates.append(row)
        if len(dry_candidates) >= 5: 
            break

    baseline_strs = [d['date'].strftime('%Y-%m-%d') for d in dry_candidates] 
    
    # Returning tiered lists and the new metadata payload!
    return tiered_candidates, baseline_strs, storm_metadata

def build_flood_report_context(result):
    master = result.get("master_stats", {})
    
    # 1. COORDINATE FALLBACK TRACKER
    center_lat = result.get("center_lat") or master.get("center_lat") or master.get("latitude")
    center_lon = result.get("center_lon") or master.get("center_lon") or master.get("longitude")
    
    # Calculate from bounding box if coordinates are missing from the root
    if (not center_lat or not center_lon) and "bbox" in result:
        bbox = result["bbox"]
        center_lat = (bbox[1] + bbox[3]) / 2
        center_lon = (bbox[0] + bbox[2]) / 2
    
    # Final sanity check string conversion
    center_lat = round(center_lat, 4) if isinstance(center_lat, (int, float)) else "N/A"
    center_lon = round(center_lon, 4) if isinstance(center_lon, (int, float)) else "N/A"

    # 2. AUTO-GENERATE TIER CHART
    tier_chart_buf = create_tier_chart(result) if "individual_layers" in result else None
    rainfall_chart_buf = create_rainfall_chart(result) if "individual_layers" in result else None
    landcover_chart_buf = create_landcover_chart(result)

    context = {
        "region_name": result.get("region_name", "AOI Flood Analysis"),
        "center_lat": center_lat,
        "center_lon": center_lon,

        "area_sqkm": master.get("selected_area_sqkm") or master.get("area_sqkm") or "N/A",
        "baseline_date": master.get("baseline_date", "N/A"),
        "mean_elevation": master.get("mean_elevation_m") or master.get("mean_elevation") or "N/A",
        "confidence_score": master.get("confidence_score", "N/A"),
        "max_flood_sqkm": master.get("high_susceptibility_sqkm") or master.get("max_flood_sqkm") or "N/A",

        "tier_table": [
            {
                # Extract digits only (e.g., "Tier_1" -> "1") to fit the template's "Tier {{ t.tier }}"
                "tier": "".join(filter(str.isdigit, str(k))) or str(k),
                "date": v.get("date", "N/A"),
                "rainfall": v.get("rainfall_mm", 0),
                "label": v.get("tier_label") or v.get("label") or "Standard Event",
                "area": v.get("flooded_sqkm", 0)
            }
            for k, v in sorted(
                result.get("individual_layers", {}).items(), 
                key=lambda x: int("".join(filter(str.isdigit, str(x[0])))) if any(c.isdigit() for c in str(x[0])) else x[0]
            )
        ],

        "heatmap_image": result.get("heatmap_b64") or "",

        # 3. ANTI-NONE STRING PROTECTION (Now using our local buffers!)
        "rainfall_chart": rainfall_chart_buf or "", 
        "tier_chart": tier_chart_buf or "",
        "landcover_chart": landcover_chart_buf or "",

        # 4. CROSS-REFERENCE LAND COVER FIELDS
        "cropland_total": master.get("cropland_total_sqkm") or 0,
        "urban_total": master.get("urban_total_sqkm") or 0,
        "forest_total": master.get("forest_total_sqkm") or 0,

        "threshold_table": result.get("threshold_table") or "", 
        "risk_summary": result.get("risk_summary") or "Generated from SAR-based flood susceptibility model."
    }

    return context

def create_tier_chart(result):

    import matplotlib.pyplot as plt
    import io

    tiers = []
    areas = []

    for k in sorted(result["individual_layers"].keys(), key=lambda x: int(x.split("_")[1])):
        tiers.append(k)
        areas.append(result["individual_layers"][k]["flooded_sqkm"])

    fig, ax = plt.subplots()
    ax.bar(tiers, areas)
    ax.set_title("Flooded Area per Tier")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)

    buf.seek(0)
    return buf

def create_rainfall_chart(result):
    import matplotlib.pyplot as plt
    import io

    rain = []
    flood = []

    # Sorted so the trend line connects properly
    for k, v in sorted(
        result.get("individual_layers", {}).items(), 
        key=lambda x: int("".join(filter(str.isdigit, str(x[0])))) if any(c.isdigit() for c in str(x[0])) else x[0]
    ):
        rain.append(v.get("rainfall_mm", 0))
        flood.append(v.get("flooded_sqkm", 0))

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.scatter(rain, flood, color='#1f77b4', s=100, zorder=5)
    ax.plot(rain, flood, color='#aec7e8', linestyle='--', linewidth=2, zorder=4) 
    
    ax.set_xlabel("Rainfall (mm)")
    ax.set_ylabel("Flooded Area (sq km)")
    ax.set_title("Rainfall vs Flood Response", fontweight='bold')
    ax.grid(True, linestyle=':', alpha=0.6)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)

    buf.seek(0)
    return buf  # Returning the actual buffer, NOT .getvalue()

def create_landcover_chart(result):
    import matplotlib.pyplot as plt
    import io

    master = result.get("master_stats", {})
    
    labels = ['Cropland', 'Urban', 'Forest']
    sizes = [
        master.get("cropland_total_sqkm", 0),
        master.get("urban_total_sqkm", 0),
        master.get("forest_total_sqkm", 0)
    ]

    filtered_data = [(l, s) for l, s in zip(labels, sizes) if s > 0]
    
    fig, ax = plt.subplots(figsize=(5, 5))
    
    if filtered_data:
        f_labels, f_sizes = zip(*filtered_data)
        colors = ['#ff9999', '#66b3ff', '#99ff99']
        ax.pie(f_sizes, labels=f_labels, autopct='%1.1f%%', startangle=90, colors=colors, wedgeprops={'edgecolor': 'white'})
        ax.axis('equal') 
    else:
        ax.text(0.5, 0.5, 'No Land Cover Data', ha='center', va='center', color='gray')
        ax.set_axis_off()

    ax.set_title("Land Cover Impact", fontweight='bold')

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf

def img_from_buf(doc, buf):
    return InlineImage(doc, buf, width=Cm(15))


def generate_insights(result):
    """
    Generates dynamic text insights based on historical storm tiers and land cover impacts.
    """
    layers = result.get('individual_layers', {})
    
    # 1. Critical Rainfall Threshold
    significant_tiers = [
        data for key, data in layers.items() 
        if data.get('flooded_sqkm', 0) > 5.0
    ]
    
    if significant_tiers:
        significant_tiers.sort(key=lambda x: x['rainfall_mm'])
        threshold = significant_tiers[0]
        threshold_text = (
            f"Non-linear flood expansion begins near {threshold['rainfall_mm']} mm of rainfall. "
            f"Events exceeding this threshold show a sharp increase in inundated area (>{threshold['flooded_sqkm']} sq km)."
        )
    else:
        threshold_text = "No severe non-linear flood expansion detected in the current historical tiers."

    # 2. High Risk Zones
    lc_totals = {"Cropland": 0, "Urban": 0, "Forest": 0}
    for layer in layers.values():
        lc = layer.get("land_cover", {})
        if lc.get("cropland_sqkm") != "N/A":
            lc_totals["Cropland"] += lc.get("cropland_sqkm", 0)
            lc_totals["Urban"] += lc.get("urban_sqkm", 0)
            lc_totals["Forest"] += lc.get("forest_sqkm", 0)

    if sum(lc_totals.values()) > 0:
        highest_risk_lc = max(lc_totals, key=lc_totals.get)
        highest_risk_val = round(lc_totals[highest_risk_lc], 2)
        high_risk_text = (
            f"The primary High-Risk Zone is {highest_risk_lc}, with cumulative exposure indicating {highest_risk_val} sq km affected across historical events. "
            f"Mitigation efforts should prioritize these areas, especially where elevation dips below the AOI mean."
        )
    else:
        high_risk_text = "Insufficient land cover data to determine specific high-risk zones."

    return threshold_text, high_risk_text


def create_contextual_map(result):
    """
    Decodes the base64 heatmap into a raw BytesIO buffer 
    so docxtpl can cleanly ingest it as an InlineImage.
    """
    if "heatmap_b64" in result and result["heatmap_b64"]:
        img_data = base64.b64decode(result["heatmap_b64"])
        return io.BytesIO(img_data)
    
    return io.BytesIO()