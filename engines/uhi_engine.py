import rioxarray
import geopandas as gpd
from shapely.geometry import box
import tempfile
import os
import matplotlib.pyplot as plt

# Import the shared gear you just made!
from core.utils import fig_to_base64, generate_basemap_b64

def run_uhi_pipeline(thermal_url, user_polygon, bbox, area_sq_km):
    """
    The pure math engine. 
    Takes raw satellite data and geometry -> Returns maps and stats.
    """
    # 1. Open and Reproject Data
    ds = rioxarray.open_rasterio(thermal_url)
    ds_gps = ds.rio.reproject("EPSG:4326")
    ds_clipped = ds_gps.rio.clip([user_polygon], crs="epsg:4326")
    
    # Ignore empty space
    ds_valid = ds_clipped.where(ds_clipped > 0)
    
    # 2. THE SHAPE-AWARE FAIL-FAST DETECTOR
    bbox_geom = box(*bbox)
    gdf_bbox = gpd.GeoDataFrame({'geometry': [bbox_geom]}, crs="EPSG:4326").to_crs(epsg=3857)
    bbox_area_sq_km = gdf_bbox.geometry.area.iloc[0] / 1000000
    
    expected_ratio = area_sq_km / bbox_area_sq_km
    total_pixels = ds_valid.size
    valid_pixels = ds_valid.count().item()
    actual_ratio = valid_pixels / total_pixels
    
    polygon_data_ratio = actual_ratio / expected_ratio
    missing_ratio = 1.0 - polygon_data_ratio
    
    # Throw an error to the frontend instead of using st.error!
    if missing_ratio > 0.30:
        raise ValueError(f"Swath Edge Detected! {missing_ratio*100:.1f}% of your drawn area is missing satellite data. Please select a different date or draw your polygon further inland.")
        
    # 3. Calculate LST (Celsius)
    temp_celsius = (ds_valid * 0.00341802 + 149.0) - 273.15
    
    # 4. Calculate Stats & Thresholds
    mean_temp = float(temp_celsius.mean().item())
    std_temp = float(temp_celsius.std().item())
    max_temp = float(temp_celsius.max().item())
    min_temp = float(temp_celsius.min().item())
    uhi_threshold = mean_temp + (0.5 * std_temp)
    
    hotspots = temp_celsius.where(temp_celsius > uhi_threshold)
    
    # 5. Generate Image Assets (Using your imported fig_to_base64)
    fig1, ax1 = plt.subplots(); ax1.set_aspect('equal'); ax1.set_axis_off(); fig1.patch.set_alpha(0)
    hotspots.plot(ax=ax1, cmap="inferno", add_colorbar=False, add_labels=False)
    hotspot_b64 = fig_to_base64(fig1)

    fig2, ax2 = plt.subplots(); ax2.set_aspect('equal'); ax2.set_axis_off(); fig2.patch.set_alpha(0)
    temp_celsius.plot(ax=ax2, cmap="magma", add_colorbar=True, add_labels=False)
    full_thermal_b64 = fig_to_base64(fig2)
    
    # Generate True Color Basemap
    true_color_b64 = generate_basemap_b64(user_polygon)
    
    # 6. Generate GeoTIFF bytes
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        temp_filepath = tmp.name
    temp_celsius.rio.to_raster(temp_filepath)
    with open(temp_filepath, "rb") as file:
        tiff_bytes = file.read()
    os.remove(temp_filepath)
    
    # 7. Package the Payload
    bounds = temp_celsius.rio.bounds()
    folium_bounds = [[bounds[1], bounds[0]], [bounds[3], bounds[2]]]
    
    # Return a clean dictionary to the frontend
    return {
        "image_b64": hotspot_b64,
        "report_images": {
            "hotspot": hotspot_b64,
            "full_thermal": full_thermal_b64,
            "true_color": true_color_b64
        },
        "bounds": folium_bounds,
        "tiff_bytes": tiff_bytes,
        "stats": {
            "max": max_temp,
            "min": min_temp,
            "mean": mean_temp,
            "threshold": uhi_threshold
        }
    }