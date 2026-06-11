import pystac_client
import planetary_computer
import geopandas as gpd
from shapely.geometry import shape
import io
import base64
import matplotlib
matplotlib.use('Agg') # Force non-GUI background backend to save headless overhead
import matplotlib.pyplot as plt
import contextily as cx
from docxtpl import DocxTemplate, InlineImage
from docx.shared import Cm
import requests
import pandas as pd
from datetime import datetime, timedelta
import numpy as np
import folium
import gc
import rasterio
from rasterio.transform import from_bounds
from shapely.validation import make_valid
import re

def export_to_tiff_bytes(array_2d, spatial_ref_da):
    """
    Writes data directly using rasterio profiles instead of 
    copying heavy rioxarray objects.
    """
    # Extract metadata cleanly from the spatial reference array
    bounds = spatial_ref_da.rio.bounds()
    width = spatial_ref_da.rio.width
    height = spatial_ref_da.rio.height
    crs = spatial_ref_da.rio.crs
    transform = from_bounds(*bounds, width, height)

    buf = io.BytesIO()
    with rasterio.open(
        buf,
        'w',
        driver='GTiff',
        height=height,
        width=width,
        count=1,
        dtype='float32',
        crs=crs,
        transform=transform,
    ) as dst:
        dst.write(array_2d.astype(np.float32), 1)

    bytes_data = buf.getvalue()
    buf.close()
    return bytes_data

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

    if not geom.is_valid:
        try:
            geom = make_valid(geom)
        except:
            geom = geom.buffer(0)

    minx, miny, maxx, maxy = geom.bounds
    bbox = [minx, miny, maxx, maxy]

    gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326").to_crs(3857)
    area_sq_km = float(gdf.geometry.area.iloc[0]) / 1e6

    bounds = gdf.geometry.bounds.iloc[0]
    width_km = (bounds.maxx - bounds.minx) / 1000
    height_km = (bounds.maxy - bounds.miny) / 1000

    del gdf, geom
    return bbox, area_sq_km, max(width_km, height_km)

def init_master_map(center_lat, center_lon, zoom_start=12, selected_basemap="Dark Mode Map"):
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
    res = list(search.items())
    del catalog, search
    return res

def fig_to_base64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches='tight', pad_inches=0, transparent=True, dpi=100)
    fig.clf()
    plt.close(fig) 
    b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    buf.close()
    del buf, fig
    return b64

def generate_basemap_b64(user_polygon):
    """Downsized canvas structure preventing contextily OOM spikes."""
    geom = shape(user_polygon)
    gdf = gpd.GeoDataFrame({'geometry': [geom]}, crs="EPSG:4326").to_crs(3857)

    fig, ax = plt.subplots(figsize=(4, 4)) # Reduced layout scale
    ax.set_axis_off()
    fig.patch.set_alpha(0)

    gdf.boundary.plot(ax=ax, color="red", linewidth=2)
    try:
        cx.add_basemap(ax, crs=gdf.crs.to_string(), source=cx.providers.Esri.WorldImagery, zoom='auto')
    except Exception:
        pass # Protect pipeline fallback if tile system returns a timeout response

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0, transparent=True, dpi=100)
    
    fig.clf()
    plt.close(fig)
    b64_data = base64.b64encode(buf.getvalue()).decode('utf-8')
    buf.close()
    del gdf, geom, fig, ax, buf
    return b64_data

def create_word_report(template_path, context):
    ctx = {**context}
    doc = DocxTemplate(template_path)

    def inject(img_source, width=15):
        if isinstance(img_source, str): 
            img = io.BytesIO(base64.b64decode(img_source))
        else: 
            img = img_source
            img.seek(0)
        return InlineImage(doc, img, width=Cm(width))

    image_keys = [
        "heatmap_image", "rainfall_chart", "tier_chart", 
        "landcover_chart", "dem_map", "wc_map", "jrc_map"
    ]

    for key in image_keys:
        if ctx.get(key):
            ctx[key] = inject(ctx[key])
        else:
            ctx[key] = "⚠️ This image has not been generated."

    doc.render(ctx)
    buf = io.BytesIO()
    doc.save(buf)
    report_bytes = buf.getvalue()
    buf.close()
    del doc, ctx
    return report_bytes

def get_dynamic_rain_events(lat, lon, years_back=3, num_tiers=5, backups_per_tier=3):
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
    
    worst_storms = df.sort_values(by="3_day_sum", ascending=False)
    all_independent_peaks = []
    
    for index, row in worst_storms.iterrows():
        if row['3_day_sum'] < 15.0:
            continue
            
        is_independent = all(abs((row['date'] - p['date']).days) > 14 for p in all_independent_peaks)
        if is_independent:
            all_independent_peaks.append(row)

    tiered_candidates = []
    storm_metadata = {} 
    total_peaks = len(all_independent_peaks)
    
    if total_peaks >= num_tiers:
        peak_values = [p['3_day_sum'] for p in all_independent_peaks]
        percentiles = [100 - (100 / num_tiers) * i for i in range(1, num_tiers)] 
        boundaries = [np.percentile(peak_values, q) for q in percentiles]
        
        remaining = all_independent_peaks.copy()
        original_peaks = all_independent_peaks.copy() 
        
        for threshold in boundaries:
            historical_count = sum(1 for p in original_peaks if p['3_day_sum'] >= threshold)
            return_period = round(years_back / max(historical_count, 1), 1) if historical_count > 0 else years_back
            
            bucket = [p for p in remaining if p['3_day_sum'] >= threshold]
            selected = bucket[:backups_per_tier]
            tier_dates = [p['date'].strftime('%Y-%m-%d') for p in selected]
            
            if tier_dates:
                tiered_candidates.append(tier_dates)
                for p in selected:
                    date_str = p['date'].strftime('%Y-%m-%d')
                    storm_metadata[date_str] = {
                        "rainfall_mm": round(p['3_day_sum'], 1),
                        "return_period_years": return_period 
                    }
                    
            remaining = [r for r in remaining if r['date'] not in [s['date'] for s in selected]]
            
        if remaining:
            selected = remaining[:backups_per_tier]
            tier_dates = [p['date'].strftime('%Y-%m-%d') for p in selected]
            
            if tier_dates:
                tiered_candidates.append(tier_dates)
                historical_count = len(original_peaks) 
                return_period = round(years_back / max(historical_count, 1), 1) if historical_count > 0 else years_back
                
                for p in selected:
                    date_str = p['date'].strftime('%Y-%m-%d')
                    storm_metadata[date_str] = {
                        "rainfall_mm": round(p['3_day_sum'], 1),
                        "return_period_years": return_period 
                    }
    else:
        tiered_candidates = []
        for p in all_independent_peaks[:num_tiers]:
            peak_dt = p['date']
            backup_cluster = [
                (peak_dt - timedelta(days=1)).strftime('%Y-%m-%d'),
                peak_dt.strftime('%Y-%m-%d'),
                (peak_dt + timedelta(days=1)).strftime('%Y-%m-%d')
            ]
            tiered_candidates.append(backup_cluster)
            
            for d in backup_cluster:
                storm_metadata[d] = {
                    "rainfall_mm": round(p['3_day_sum'], 1),
                    "return_period_years": years_back 
                }
            
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
    del df, worst_storms, driest_periods, all_independent_peaks
    return tiered_candidates, baseline_strs, storm_metadata

def build_flood_report_context(result):
    master = result.get("master_stats", {})
    
    center_lat = result.get("center_lat") or master.get("center_lat") or master.get("latitude")
    center_lon = result.get("center_lon") or master.get("center_lon") or master.get("longitude")
    
    if (not center_lat or not center_lon) and "bbox" in result:
        bbox = result["bbox"]
        center_lat = (bbox[1] + bbox[3]) / 2
        center_lon = (bbox[0] + bbox[2]) / 2
    
    center_lat = round(center_lat, 4) if isinstance(center_lat, (int, float)) else "N/A"
    center_lon = round(center_lon, 4) if isinstance(center_lon, (int, float)) else "N/A"

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
        "rainfall_chart": rainfall_chart_buf or "", 
        "tier_chart": tier_chart_buf or "",
        "landcover_chart": landcover_chart_buf or "",
        "cropland_total": master.get("cropland_total_sqkm") or 0,
        "urban_total": master.get("urban_total_sqkm") or 0,
        "forest_total": master.get("forest_total_sqkm") or 0,
        "threshold_table": result.get("threshold_table") or "", 
        "risk_summary": result.get("risk_summary") or "Generated from SAR-based flood susceptibility model."
    }
    return context

def create_tier_chart(result):
    tiers = []
    areas = []
    for k in sorted(result["individual_layers"].keys(), key=lambda x: int(x.split("_")[1])):
        tiers.append(k)
        areas.append(result["individual_layers"][k]["flooded_sqkm"])

    fig, ax = plt.subplots(figsize=(5, 3))
    ax.bar(tiers, areas, color='#2b5c8f')
    ax.set_title("Flooded Area per Tier", fontsize=10, fontweight='bold')
    ax.tick_params(axis='both', labelsize=8)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
    fig.clf()
    plt.close(fig)
    buf.seek(0)
    del fig, ax
    return buf

def create_rainfall_chart(result):
    rain = []
    float_areas = []

    for k, v in sorted(
        result.get("individual_layers", {}).items(), 
        key=lambda x: int("".join(filter(str.isdigit, str(x[0])))) if any(c.isdigit() for c in str(x[0])) else x[0]
    ):
        rain.append(v.get("rainfall_mm", 0))
        float_areas.append(v.get("flooded_sqkm", 0))

    fig, ax = plt.subplots(figsize=(5, 3))
    ax.scatter(rain, float_areas, color='#1f77b4', s=60, zorder=5)
    ax.plot(rain, float_areas, color='#aec7e8', linestyle='--', linewidth=1.5, zorder=4) 
    
    ax.set_xlabel("Rainfall (mm)", fontsize=8)
    ax.set_ylabel("Flooded Area (sq km)", fontsize=8)
    ax.set_title("Rainfall vs Flood Response", fontsize=10, fontweight='bold')
    ax.tick_params(axis='both', labelsize=8)
    ax.grid(True, linestyle=':', alpha=0.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
    fig.clf()
    plt.close(fig)
    buf.seek(0)
    del fig, ax
    return buf

def create_landcover_chart(result):
    master = result.get("master_stats", {})
    labels = ['Cropland', 'Urban', 'Forest']
    sizes = [
        master.get("cropland_total_sqkm", 0),
        master.get("urban_total_sqkm", 0),
        master.get("forest_total_sqkm", 0)
    ]
    filtered_data = [(l, s) for l, s in zip(labels, sizes) if s > 0]
    
    fig, ax = plt.subplots(figsize=(4, 4))
    if filtered_data:
        f_labels, f_sizes = zip(*filtered_data)
        colors = ['#ff9999', '#66b3ff', '#99ff99']
        ax.pie(f_sizes, labels=f_labels, autopct='%1.1f%%', startangle=90, colors=colors, wedgeprops={'edgecolor': 'white'}, textprops={'fontsize': 8})
        ax.axis('equal') 
    else:
        ax.text(0.5, 0.5, 'No Land Cover Data', ha='center', va='center', color='gray', fontsize=9)
        ax.set_axis_off()

    ax.set_title("Land Cover Impact", fontsize=10, fontweight='bold')
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
    fig.clf()
    plt.close(fig)
    buf.seek(0)
    del fig, ax
    return buf

def img_from_buf(doc, buf):
    return InlineImage(doc, buf, width=Cm(15))

def generate_insights(result):
    layers = result.get('individual_layers', {})
    significant_tiers = [data for data in layers.values() if data.get('flooded_sqkm', 0) > 5.0]
    
    if significant_tiers:
        significant_tiers.sort(key=lambda x: x['rainfall_mm'])
        threshold = significant_tiers[0]
        threshold_text = (
            f"Non-linear flood expansion begins near {threshold['rainfall_mm']} mm of rainfall. "
            f"Events exceeding this threshold show a sharp increase in inundated area (>{threshold['flooded_sqkm']} sq km)."
        )
    else:
        threshold_text = "No severe non-linear flood expansion detected in the current historical tiers."

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
    if "heatmap_b64" in result and result["heatmap_b64"]:
        img_data = base64.b64decode(result["heatmap_b64"])
        return io.BytesIO(img_data)
    return io.BytesIO()