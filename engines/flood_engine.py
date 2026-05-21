import pystac_client
import planetary_computer
import rioxarray
import numpy as np
import matplotlib.pyplot as plt
import io
import base64
from datetime import datetime, timedelta
import scipy.ndimage as ndimage

# Import the tools we built in the Megabase!
from core.utils import process_user_polygon, get_dynamic_rain_events

def fetch_sar_image(bbox, target_date_str, is_baseline=False):
    """Fetches and clips a Sentinel-1 VV array with aggressive windows and error logging."""
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d")
    
    if is_baseline:
        # Dry baseline stays wide (it's dry for weeks anyway)
        start_date = target_date - timedelta(days=15)
        end_date = target_date + timedelta(days=15)
    else:
        # THE FIX IS BACK: Strict 0 to +5 days to catch the actual floodwater!
        start_date = target_date
        end_date = target_date + timedelta(days=5)
        
    time_window = f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"
    
    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    
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
        return array, clipped # THE FIX: Return the spatial object too so the DEM can copy it!
    except Exception as e:
        print(f"  [!] RIOXARRAY CRASH: {e}")
        return None, None
    
def fetch_terrain_mask(bbox, sar_spatial_reference):
    """Fetches DEM, aligns it to SAR, and calculates the Ocean & Gravity Masks."""
    print("🏔️ Fetching Copernicus DEM & calculating gravity masks...")
    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    
    # cop-dem-glo-30 is the global 30m elevation model
    search = catalog.search(
        collections=["cop-dem-glo-30"],
        bbox=bbox
    )
    items = list(search.items())
    
    if not items:
        print("  [!] Warning: No DEM found for this region. Masking disabled.")
        return None
        
    try:
        dem_rds = rioxarray.open_rasterio(items[0].assets["data"].href)
        
        # THE CHEAT CODE: Morph the 30m DEM to perfectly match the 10m SAR grid!
        dem_matched = dem_rds.rio.reproject_match(sar_spatial_reference)
        elevation = dem_matched.squeeze().values
        
        # Calculate Slope (Rise over Run using Numpy gradients)
        dy, dx = np.gradient(elevation, 10, 10) # 10m SAR pixel resolution
        slope = np.degrees(np.arctan(np.sqrt(dx**2 + dy**2)))
        
        # THE MASK: Must be above sea level (> 0m) AND relatively flat (< 5 degrees)
        valid_terrain = (elevation > 0) & (slope < 5)
        
        return valid_terrain
    except Exception as e:
        print(f"  [!] DEM Error: {e}")
        return None
    
def run_flood_pipeline(user_polygon, years_back=3):
    """The Master Pipeline: Coordinates the Scout, the Fetcher, and the Physics."""
    # 1. Geometry Prep
    try:
        bbox, area_sq_km, max_dim = process_user_polygon(user_polygon)
    except ValueError as e:
        # Gracefully exit and send the error string back to the Streamlit UI
        return {"error": str(e)}
    center_lon = (bbox[0] + bbox[2]) / 2
    center_lat = (bbox[1] + bbox[3]) / 2
    
    # 2. The IMERG Scout
    print("📡 Scouting historical rain events...")
    try:
        peak_dates, baseline_candidates, storm_metadata = get_dynamic_rain_events(center_lat, center_lon, years_back)
    except Exception as e:
        return {"error": f"IMERG Error: {e}"}
        
    if not peak_dates:
        return {"error": "No extreme rainfall events found here. Try increasing the historical window."}
    if not baseline_candidates:
        return {"error": "Could not establish any dry periods under 10mm for this region."}

    # 3. Fetch Baseline SAR
    print("🏜️ Searching for a valid Dry Baseline SAR image...")
    dry_array = None
    locked_baseline_date = None
    
    for b_date in baseline_candidates:
        print(f"  -> Testing dry candidate ({b_date})...")
        dry_array, dry_spatial_ref = fetch_sar_image(bbox, b_date, is_baseline=True) 
        
        if dry_array is not None:
            locked_baseline_date = b_date
            print(f"  ✅ Success! Locked in dry baseline near {locked_baseline_date}")
            break 
            
    if dry_array is None:
        return {"error": "Sentinel-1 did not capture a baseline image during ANY of the 5 tested dry periods."}
        
    dry_db = 10 * np.log10(np.clip(dry_array, 1e-10, None))
    terrain_mask = fetch_terrain_mask(bbox, dry_spatial_ref)

    # 4. Process Wet Events (The Tiered Ensemble & Layer Generator)
    flood_hits = np.zeros_like(dry_array, dtype=float)
    successful_scans = 0
    actual_dates_used = [] 
    
    # NEW: Dictionary to hold the individual base64 images for each storm
    individual_layers = {} 
    
    for tier_idx, tier_dates in enumerate(peak_dates):
        print(f"\n🌊 Processing Severity Tier {tier_idx + 1}...")
        tier_success = False
        
        for date_str in tier_dates:
            print(f"  -> Testing storm on {date_str}...")
            # We need to catch the spatial object now, not just the array
            wet_array, wet_spatial = fetch_sar_image(bbox, date_str) 
            
            if wet_array is not None:
                if wet_array.shape != dry_array.shape:
                    print(f"  [!] Shape mismatch ({wet_array.shape} vs {dry_array.shape}). Forcing alignment...")
                    try:
                        # THE CHEAT CODE: Morph the wet image to perfectly match the dry baseline grid!
                        wet_matched = wet_spatial.rio.reproject_match(dry_spatial_ref)
                        wet_array = wet_matched.squeeze().values
                    except Exception as e:
                        print(f"  [!] Realignment failed: {e}. Skipping backup.")
                        continue
                    
                wet_db = 10 * np.log10(np.clip(wet_array, 1e-10, None))
                
                # 1. Base SAR Physics
                raw_flood = (wet_db < -14) & ((wet_db - dry_db) < -2.5)
                
                # 2. Hydro-Conditioning (Region Growing to fix Donuts/False Negatives)
                # Dilates the raw flood outward by 2 pixels (approx 20 meters) to fill gaps
                filled_flood = ndimage.binary_dilation(raw_flood, iterations=2)
                
                # 3. Apply the Gravity Mask (Eliminate Oceans & Mountains/False Positives)
                if terrain_mask is not None:
                    is_flooded = filled_flood & terrain_mask
                else:
                    is_flooded = filled_flood


                # --- INDIVIDUAL LAYER GENERATION ---
                fig_tier, ax_tier = plt.subplots(figsize=(8, 8))
                ax_tier.set_axis_off()
                fig_tier.patch.set_alpha(0)
                cmap_tier = plt.cm.Reds # Let's paint individual storms RED to contrast with the master BLUE map
                cmap_tier.set_under('none')
                
                # Multiply by 100 so the boolean True (1) becomes 100, triggering the color map
                ax_tier.imshow(is_flooded.astype(int) * 100, cmap=cmap_tier, vmin=1, vmax=100)
                buf_tier = io.BytesIO()
                fig_tier.savefig(buf_tier, format='png', bbox_inches='tight', pad_inches=0, transparent=True)
                plt.close(fig_tier)
                
                # Save the image to our dictionary using the date as the key
                # THE STAT TRACKER: 10m x 10m = 100 sq meters per pixel
                flooded_pixels = is_flooded.sum()
                flooded_sqkm = (flooded_pixels * 100) / 1_000_000
                
                # Save as a dictionary instead of just a string, so the UI gets everything!
                individual_layers[f"Tier_{tier_idx + 1}"] = {
                    "date": date_str,
                    "image_b64": base64.b64encode(buf_tier.getvalue()).decode('utf-8'),
                    "flooded_sqkm": round(flooded_sqkm, 2),
                    "rainfall_mm": storm_metadata.get(date_str, {}).get("rainfall_mm", "N/A"),
                    "return_period": storm_metadata.get(date_str, {}).get("return_period_years", "N/A")
                }
                # -----------------------------------

                flood_hits += is_flooded.astype(int)
                successful_scans += 1
                actual_dates_used.append(date_str)
                tier_success = True
                print(f"  ✅ Success! Locked in Tier {tier_idx + 1} representative storm.")
                break 
                
        if not tier_success:
            print(f"  ❌ Failed to find any SAR images for Tier {tier_idx + 1} across all backups.")
            
    if successful_scans == 0:
        return {"error": "Found rain events, but no Sentinel-1 satellite passes matched any of the backups."}

    # 5. Probability Math (The Master Compound Map)
    probability_matrix = (flood_hits / successful_scans) * 100

    # 6. Packaging Master Heatmap for the UI
    print("\n🎨 Generating Master UI overlay...")
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_axis_off()
    fig.patch.set_alpha(0) 
    
    cmap = plt.cm.Blues
    cmap.set_under('none') 
    
    ax.imshow(probability_matrix, cmap=cmap, vmin=1, vmax=100)
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0, transparent=True)
    plt.close(fig)
    heatmap_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    
    # Calculate Total Area at Risk (Probability > 0%)
    risk_pixels = (probability_matrix > 0).sum()
    total_risk_sqkm = (risk_pixels * 100) / 1_000_000

    return {
        "success": True,
        "heatmap_b64": heatmap_b64,
        "master_stats": {
            "total_risk_sqkm": round(total_risk_sqkm, 2),
            "max_probability": int(probability_matrix.max()),
            "analyzed_area_sqkm": round(area_sq_km, 2)
        },
        "individual_layers": individual_layers,
        "scans_used": successful_scans,
        "dates_analyzed": actual_dates_used,
    }

    