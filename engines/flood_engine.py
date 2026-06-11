import pystac_client
import planetary_computer
import rioxarray
import numpy as np
import matplotlib.pyplot as plt
import io
import base64
from datetime import datetime, timedelta
import scipy.ndimage as ndimage
from scipy.ndimage import uniform_filter, label
from rasterio.io import MemoryFile
from core.utils import process_user_polygon, get_dynamic_rain_events, export_to_tiff_bytes

# Helper to quickly convert a generic array to a b64 PNG string
def array_to_b64(arr, cmap='viridis', vmin=None, vmax=None):
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_axis_off()
    fig.patch.set_alpha(0)
    ax.imshow(arr, cmap=cmap, vmin=vmin, vmax=vmax)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0, transparent=True)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode('utf-8')

def fetch_sar_image(catalog, bbox, target_date_str, is_baseline=False):
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d")
    
    if is_baseline:
        start_date = target_date - timedelta(days=15)
        end_date = target_date + timedelta(days=15)
    else:
        start_date = target_date
        end_date = target_date + timedelta(days=5)
        
    time_window = f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"
    
    search = catalog.search(
        collections=["sentinel-1-rtc"],
        bbox=bbox,
        datetime=time_window
    )
    items = list(search.items())
    
    if not items:
        print(f"  [-] STAC MISS: No satellite pass found for {time_window}.")
        return None, None
        
    asset_href = items[0].assets["vv"].href
    
    try:
        rds = rioxarray.open_rasterio(asset_href)
        clipped = rds.rio.clip_box(*bbox, crs="EPSG:4326")
        array = clipped.squeeze().values
        return array, clipped
    except Exception as e:
        print(f"  [!] RIOXARRAY CRASH: {e}")
        return None, None
    
def fetch_terrain_mask(catalog, bbox, sar_spatial_reference):
    print("🏔️ Fetching Copernicus DEM & calculating gravity masks...")
    search = catalog.search(collections=["cop-dem-glo-30"], bbox=bbox)
    items = list(search.items())
    
    if not items:
        print("  [!] Warning: No DEM found for this region. Masking disabled.")
        return None, None
        
    try:
        dem_rds = rioxarray.open_rasterio(items[0].assets["data"].href)
        dem_matched = dem_rds.rio.reproject_match(sar_spatial_reference)
        elevation = dem_matched.squeeze().values
        
        dy, dx = np.gradient(elevation, 10, 10)  # 10m SAR pixel resolution
        slope = np.degrees(np.arctan(np.sqrt(dx**2 + dy**2)))
        
        valid_terrain = (elevation > 0) & (slope < 5)
        return valid_terrain, elevation
    except Exception as e:
        print(f"  [!] DEM Error: {e}")
        return None, None
    
def fetch_environmental_mask(catalog, bbox, sar_spatial_reference):
    print("🌍 Fetching JRC Permanent Water & ESA WorldCover masks...")

    valid_env_mask = None
    wc_array = None
    jrc_array_raw = None 
    
    # --- 1. JRC Global Surface Water ---
    try:
        jrc_search = catalog.search(collections=["jrc-gsw"], bbox=bbox)
        jrc_items = list(jrc_search.items())
        if jrc_items:
            jrc_rds = rioxarray.open_rasterio(jrc_items[0].assets["occurrence"].href)
            jrc_matched = jrc_rds.rio.reproject_match(sar_spatial_reference)
            jrc_array_raw = jrc_matched.squeeze().values 
            
            valid_jrc = jrc_array_raw < 80
            valid_env_mask = valid_jrc
            print("  ✅ JRC Permanent Water mask aligned.")
    except Exception as e:
        print(f"  [!] JRC Fetch Error: {e}")

    # --- 2. ESA WorldCover ---
    try:
        wc_search = catalog.search(collections=["esa-worldcover"], bbox=bbox)
        wc_items = list(wc_search.items())
        if wc_items:
            wc_rds = rioxarray.open_rasterio(wc_items[0].assets["map"].href)
            wc_matched = wc_rds.rio.reproject_match(sar_spatial_reference)
            wc_array = wc_matched.squeeze().values
            
            # FIXED: explicitly whitelist valid landcover codes to prevent nuking the map
            valid_wc = np.isin(wc_array, [10, 20, 30, 40, 50, 60, 70])
            
            if valid_env_mask is not None:
                valid_env_mask = valid_env_mask & valid_wc
            else:
                valid_env_mask = valid_wc
            print("  ✅ ESA WorldCover mask aligned.")
    except Exception as e:
        print(f"  [!] WorldCover Fetch Error: {e}")

    return valid_env_mask, wc_array, jrc_array_raw 
    
def run_flood_pipeline(user_polygon, years_back=3):
    try:
        bbox, area_sq_km, max_dim = process_user_polygon(user_polygon)
    except ValueError as e:
        return {"error": str(e)}
        
    center_lon = (bbox[0] + bbox[2]) / 2
    center_lat = (bbox[1] + bbox[3]) / 2
    
    print("🛰️ Establishing connection to Planetary Computer STAC Catalog...")
    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    
    print("📡 Scouting historical rain events...")
    try:
        # Ensure we are getting the variables expected
        peak_dates_list, baseline_candidates, storm_metadata = get_dynamic_rain_events(center_lat, center_lon, years_back)
    except Exception as e:
        return {"error": f"IMERG Error: {e}"}
        
    # Safety checks
    if not peak_dates_list:
        return {"error": "No extreme rainfall events found here. Try increasing the historical window."}
    # Rename for clarity to avoid confusion with loop variables
    peak_dates = peak_dates_list
    if not baseline_candidates:
        return {"error": "Could not establish any dry periods under 10mm for this region."}

    print("🏜️ Searching for a valid Dry Baseline SAR image...")
    dry_array = None
    locked_baseline_date = None
    
    for b_date in baseline_candidates:
        print(f"  -> Testing dry candidate ({b_date})...")
        dry_array, dry_spatial_ref = fetch_sar_image(catalog, bbox, b_date, is_baseline=True) 
        if dry_array is not None:
            locked_baseline_date = b_date
            print(f"  ✅ Success! Locked in dry baseline near {locked_baseline_date}")
            break 
            
    if dry_array is None:
        return {"error": "Sentinel-1 did not capture a baseline image during ANY of the 5 tested dry periods."}
        
    dry_array_filtered = uniform_filter(dry_array, size=3)
    dry_db = 10 * np.log10(np.clip(dry_array_filtered, 1e-10, None))
    
    terrain_mask, elevation = fetch_terrain_mask(catalog, bbox, dry_spatial_ref)
    env_mask, wc_array, jrc_array = fetch_environmental_mask(catalog, bbox, dry_spatial_ref)

    mask_b64 = None
    if env_mask is not None:
        fig_mask, ax_mask = plt.subplots(figsize=(8, 8))
        ax_mask.set_axis_off()
        fig_mask.patch.set_alpha(0)
        cmap_mask = plt.cm.Wistia 
        cmap_mask.set_under('none')
        ax_mask.imshow((~env_mask).astype(int) * 100, cmap=cmap_mask, vmin=1, vmax=100, alpha=0.6)
        
        buf_mask = io.BytesIO()
        fig_mask.savefig(buf_mask, format='png', bbox_inches='tight', pad_inches=0, transparent=True)
        plt.close(fig_mask)
        mask_b64 = base64.b64encode(buf_mask.getvalue()).decode('utf-8')

    flood_hits = np.zeros_like(dry_array, dtype=float)
    total_weight = 0.0
    successful_scans = 0
    actual_dates_used = [] 
    individual_layers = {} 
    min_rainfall_to_flood = np.full(dry_array.shape, np.inf)

    def label_tier(rp):
        try:
            val = float(rp)
            if val >= 2.0: return "Historic Deluge"
            if val >= 0.5: return "Severe Event"
            if val >= 0.2: return "Moderate Event"
            return "Marginal Event"
        except: return "Standard Event"

    def get_lc_sqkm(wc, code, flood_mask):
        if wc is None:
            return "N/A"
        return round(((wc == code) & flood_mask).sum() * 100 / 1e6, 2)
        
    # Use a different variable name for the inner loop to avoid clobbering
    for tier_idx, current_tier_dates in enumerate(peak_dates):
        print(f"\n🌊 Processing Severity Tier {tier_idx + 1}...")
        tier_success = False
        
        # Iterate over the dates in this specific tier
        for date_str in current_tier_dates:
            print(f"  -> Testing storm on {date_str}...")
            wet_array, wet_spatial = fetch_sar_image(catalog, bbox, date_str) 
            
            if wet_array is not None:
                if wet_array.shape != dry_array.shape:
                    print(f"  [!] Shape mismatch. Forcing alignment...")
                    try:
                        wet_matched = wet_spatial.rio.reproject_match(dry_spatial_ref)
                        wet_array = wet_matched.squeeze().values
                    except Exception as e:
                        print(f"  [!] Realignment failed: {e}. Skipping backup.")
                        continue
                    
                wet_array_filtered = uniform_filter(wet_array, size=3)
                wet_db = 10 * np.log10(np.clip(wet_array_filtered, 1e-10, None))
                
                dynamic_threshold = np.full(wet_db.shape, -14.0)
                if wc_array is not None:
                    dynamic_threshold[wc_array == 10] = -12.0  
                    dynamic_threshold[wc_array == 20] = -13.0  
                    dynamic_threshold[wc_array == 50] = -12.0  
                    dynamic_threshold[wc_array == 60] = -16.0  
                
                raw_flood = (wet_db < dynamic_threshold) & ((wet_db - dry_db) < -2.5)
                filled_flood = ndimage.binary_dilation(raw_flood, iterations=2)
                
                is_flooded = filled_flood.copy()
                if terrain_mask is not None:
                    is_flooded = is_flooded & terrain_mask
                if env_mask is not None:
                    is_flooded = is_flooded & env_mask

                labeled, num_features = ndimage.label(is_flooded)
                component_sizes = np.bincount(labeled.ravel())
                too_small = component_sizes < 5
                too_small[0] = False  
                is_flooded = is_flooded & ~too_small[labeled]

                fig_tier, ax_tier = plt.subplots(figsize=(8, 8))
                ax_tier.set_axis_off()
                fig_tier.patch.set_alpha(0)
                cmap_tier = plt.cm.Reds 
                cmap_tier.set_under('none')
                
                ax_tier.imshow(is_flooded.astype(int) * 100, cmap=cmap_tier, vmin=1, vmax=100)
                buf_tier = io.BytesIO()
                fig_tier.savefig(buf_tier, format='png', bbox_inches='tight', pad_inches=0, transparent=True)
                plt.close(fig_tier)
                
                flooded_pixels = is_flooded.sum()
                flooded_sqkm = (flooded_pixels * 100) / 1_000_000

                current_rain = float(storm_metadata.get(date_str, {}).get("rainfall_mm", np.inf))
                min_rainfall_to_flood[is_flooded & (current_rain < min_rainfall_to_flood)] = current_rain

                individual_layers[f"Tier_{tier_idx + 1}"] = {
                    "date": date_str,
                    "image_b64": base64.b64encode(buf_tier.getvalue()).decode('utf-8'),
                    "tiff_bytes": export_to_tiff_bytes(is_flooded, dry_spatial_ref), 
                    "flooded_sqkm": round(flooded_sqkm, 2),
                    "rainfall_mm": current_rain,
                    "return_period": storm_metadata.get(date_str, {}).get("return_period_years", "N/A"),
                    "tier_label": label_tier(storm_metadata.get(date_str, {}).get("return_period_years", 0)),
                    "land_cover": {
                        "cropland_sqkm": get_lc_sqkm(wc_array, 40, is_flooded),
                        "urban_sqkm": get_lc_sqkm(wc_array, 50, is_flooded),
                        "forest_sqkm": get_lc_sqkm(wc_array, 10, is_flooded)
                    }
                }

                rp_raw = storm_metadata.get(date_str, {}).get("return_period_years", years_back)
                try:
                    rp = float(rp_raw)
                except (ValueError, TypeError):
                    rp = years_back
                
                annual_weight = min(1.0 / rp, 1.0)  
                flood_hits += is_flooded.astype(float) * annual_weight
                total_weight += annual_weight

                successful_scans += 1
                actual_dates_used.append(date_str)
                tier_success = True
                print(f"  ✅ Success! Locked in Tier {tier_idx + 1} storm.")
                break 
                
        if not tier_success:
            print(f"  ❌ Failed to find any SAR images for Tier {tier_idx + 1}.")
            
    if successful_scans == 0:
        return {"error": "Found rain events, but no Sentinel-1 satellite passes matched any of the backups."}

    if total_weight > 0:
        susceptibility_matrix = (flood_hits / total_weight) * 100
    else:
        susceptibility_matrix = (flood_hits / max(1, successful_scans)) * 100

    print("\n🎨 Generating Master UI overlay...")
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_axis_off()
    fig.patch.set_alpha(0) 
    cmap = plt.cm.Blues
    cmap.set_under('none') 
    ax.imshow(susceptibility_matrix, cmap=cmap, vmin=1, vmax=100)
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0, transparent=True)
    plt.close(fig)
    heatmap_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    
    risk_threshold = 30
    risk_pixels = (susceptibility_matrix >= risk_threshold).sum()
    high_susceptibility_sqkm = (risk_pixels * 100) / 1_000_000

    master_tiff = export_to_tiff_bytes(susceptibility_matrix, dry_spatial_ref)
    jrc_tiff = export_to_tiff_bytes(jrc_array, dry_spatial_ref) if jrc_array is not None else None
    wc_tiff = export_to_tiff_bytes(wc_array, dry_spatial_ref) if wc_array is not None else None

    

    def sum_lc(key):
        total = 0
        for layer in individual_layers.values():
            val = layer.get("land_cover", {}).get(key, 0)
            if val != "N/A":
                total += val
        return round(total, 2)
    
    min_rainfall_clean = np.where(np.isinf(min_rainfall_to_flood), 0, min_rainfall_to_flood)
    min_rainfall_tiff = export_to_tiff_bytes(min_rainfall_clean, dry_spatial_ref)

    # 🗺️ Generating the 4-Panel Context Map ONCE to save memory
    print("\n🗺️ Generating 4-Panel Context Map...")
    fig_4p, axs = plt.subplots(2, 2, figsize=(10, 10))
    
    if elevation is not None:
        axs[0, 0].imshow(elevation, cmap='terrain')
    axs[0, 0].set_title("Elevation (DEM)")
    axs[0, 0].axis('off')
    
    if wc_array is not None:
        axs[0, 1].imshow(wc_array, cmap='tab20')
    axs[0, 1].set_title("ESA WorldCover")
    axs[0, 1].axis('off')
    
    if jrc_array is not None:
        axs[1, 0].imshow(jrc_array, cmap='Blues', vmin=0, vmax=100)
    axs[1, 0].set_title("JRC Permanent Water")
    axs[1, 0].axis('off')
    
    axs[1, 1].imshow(susceptibility_matrix, cmap='Reds', vmin=1, vmax=100)
    axs[1, 1].set_title("Master Flood Susceptibility")
    axs[1, 1].axis('off')
    
    plt.tight_layout()
    buf_4p = io.BytesIO()
    fig_4p.savefig(buf_4p, format='png', bbox_inches='tight', transparent=True)
    plt.close(fig_4p)
    four_panel_b64 = base64.b64encode(buf_4p.getvalue()).decode('utf-8')

    # Now generate the visuals
    visual_previews = {
        "jrc_map": array_to_b64(jrc_array, 'Blues', 0, 100) if jrc_array is not None else None,
        "wc_map": array_to_b64(wc_array, 'tab20') if wc_array is not None else None,
        "dem_map": array_to_b64(elevation, 'terrain') if elevation is not None else None
    }

    return {
        "success": True,
        "heatmap_b64": heatmap_b64,
        "visual_previews": visual_previews,
        "four_panel_map_b64": four_panel_b64,
        "master_stats": {
            "high_susceptibility_sqkm": round(high_susceptibility_sqkm, 2),
            "max_susceptibility_score": int(susceptibility_matrix.max()),
            "selected_area_sqkm": round(area_sq_km, 2),
            "baseline_date": locked_baseline_date, 
            "center_lat": round(center_lat, 4),
            "center_lon": round(center_lon, 4),
            "cropland_total_sqkm": sum_lc("cropland_sqkm"),
            "urban_total_sqkm": sum_lc("urban_sqkm"),
            "forest_total_sqkm": sum_lc("forest_sqkm"),
            "mean_elevation_m": (
                round(float(np.mean(elevation[elevation > 0])), 1)
                if elevation is not None and np.any(elevation > 0) else "N/A"
            ),
            "confidence_score": round((successful_scans / len(peak_dates)) * 100, 1) 
        },
        "individual_layers": individual_layers,
        "scans_used": successful_scans,
        "dates_analyzed": actual_dates_used,
        "debug_mask_b64": mask_b64,
        "deliverables": {
            "master_tiff": master_tiff,
            "jrc_tiff": jrc_tiff,
            "worldcover_tiff": wc_tiff,
            "min_rainfall_tiff": min_rainfall_tiff
        }
    }