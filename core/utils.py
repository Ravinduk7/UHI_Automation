from urllib import response
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

def process_user_polygon(user_polygon):
    """Extracts bounding box and validates that the user drew a mathematically sound area."""
    
    # 1. Null/Dictionary Check
    if not user_polygon or not isinstance(user_polygon, dict):
        raise ValueError("Invalid AOI data received. Please redraw your shape on the map.")

    # 2. Geometry Type Check (Reject lines and points)
    geom_type = user_polygon.get("type", "")
    if geom_type not in ["Polygon", "MultiPolygon"]:
        raise ValueError(f"Invalid shape type detected ({geom_type}). Please use the Polygon tool, not lines or markers.")

    # 3. Coordinate Extraction Check
    try:
        if geom_type == "Polygon":
            coords = user_polygon["coordinates"][0]
        else: # MultiPolygon
            coords = user_polygon["coordinates"][0][0]
    except (KeyError, IndexError):
        raise ValueError("Corrupted geometry data. Please clear the map and redraw your AOI.")

    # 4. Topological Check (A valid GeoJSON ring must have at least 4 points: 3 corners + 1 closing point)
    if len(coords) < 4:
        raise ValueError("Polygon is incomplete or too small. Please draw a distinct area with at least 3 corners.")

    """Takes a Folium drawing geometry, returns BBox, Area (sq km), and Max Dimension (km)."""
    
    # The "Pac-Man" Fix: Wrap longitudes
    lons = [((c[0] + 180) % 360) - 180 for c in coords]
    lats = [c[1] for c in coords]
    bbox = [min(lons), min(lats), max(lons), max(lats)]

    # Convert to Web Mercator for real-world math
    geom = shape(user_polygon)
    gdf = gpd.GeoDataFrame({'geometry': [geom]}, crs="EPSG:4326").to_crs(epsg=3857)
    
    bounds = gdf.geometry.bounds.iloc[0]
    width_km = (bounds['maxx'] - bounds['minx']) / 1000
    height_km = (bounds['maxy'] - bounds['miny']) / 1000
    area_sq_km = gdf.geometry.area.iloc[0] / 1000000 
    
    return bbox, area_sq_km, max(width_km, height_km)

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
    """Generates the true-color contextily basemap with the red polygon outline."""
    geom = shape(user_polygon)
    gdf = gpd.GeoDataFrame({'geometry': [geom]}, crs="EPSG:4326").to_crs(epsg=3857)
    
    fig, ax = plt.subplots(figsize=(6, 6))
    ax.set_axis_off()
    fig.patch.set_alpha(0)
    gdf.boundary.plot(ax=ax, color="red", linewidth=2)
    cx.add_basemap(ax, crs=gdf.crs.to_string(), source=cx.providers.Esri.WorldImagery)
    
    return fig_to_base64(fig)

def create_word_report(template_path, context):
    """Injects data and images into the Word template and returns the file bytes."""
    doc = DocxTemplate(template_path)
    
    # Helper to decode the base64 strings back into images for the Word doc
    def inject_image(base64_str, width_cm=15):
        img_stream = io.BytesIO(base64.b64decode(base64_str))
        return InlineImage(doc, img_stream, width=Cm(width_cm))

    # Swap the base64 strings in the dictionary with actual Word image objects
    context["heatmap_image"] = inject_image(context["report_images"]["hotspot"])
    context["full_thermal_image"] = inject_image(context["report_images"]["full_thermal"])
    context["true_color_image"] = inject_image(context["report_images"]["true_color"])
    
    # Render and save to RAM buffer
    doc.render(context)
    report_buffer = io.BytesIO()
    doc.save(report_buffer)
    return report_buffer.getvalue()


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
            return_period = round(years_back / historical_count, 1) if historical_count > 0 else years_back
            
            bucket = [p for p in remaining if p['3_day_sum'] >= threshold]
            selected = bucket[:backups_per_tier]
            tier_dates = [p['date'].strftime('%Y-%m-%d') for p in selected]
            
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
                return_period = round(years_back / historical_count, 1) if historical_count > 0 else years_back
                
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