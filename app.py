import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw
import pystac_client
import planetary_computer
import rioxarray
import matplotlib.pyplot as plt
import branca.colormap as cm
import io
import base64
import numpy as np

# --- UI SETUP ---
st.set_page_config(page_title="Urban Heat Island Architect", layout="wide")

# --- INVENTORY (SESSION STATE) SETUP ---
if "layers" not in st.session_state:
    st.session_state.layers = [] # The new expanding array!
if "map_center" not in st.session_state:
    st.session_state.map_center = [6.9271, 79.8612] # Dynamic camera anchor
if "map_zoom" not in st.session_state:
    st.session_state.map_zoom = 12
if "user_polygon" not in st.session_state:
    st.session_state.user_polygon = None


st.title("🏙️ Urban Heat Island Automation Tool")
st.markdown("Automated Land Surface Temperature (LST) extraction using Landsat Thermal Infrared Sensor (TIRS) data.")

# --- SIDEBAR INPUTS ---
with st.sidebar:
    st.header("Search Parameters")
    # Step 1: Select Year
    target_year = st.number_input("Select Year for Analysis", 
                                 min_value=2013, max_value=2026, value=2024)
    layer_opacity = st.slider("Heatmap Transparency", min_value=0.1, max_value=1.0, value=0.7, step=0.1)
    
    st.info("Note: Landsat 8/9 data is available from 2013 onwards.")
    st.info("💡 Pro Tip: Satellites capture data in grid 'swaths'. Keep your polygons focused on specific cities/regions to avoid clipping the edge of a satellite's camera path!")

# --- THE INTERACTIVE MAP ---
st.subheader("1. Define your Area of Interest")

# Initialize the map with dynamic camera!
m = folium.Map(location=st.session_state.map_center, zoom_start=st.session_state.map_zoom)

# --- STAT DASHBOARD ---
if st.session_state.layers:
    st.subheader("📊 Latest UHI Statistics")
    latest_stats = st.session_state.layers[-1]["stats"]
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Maximum Temp", f"{latest_stats['max']:.1f} °C")
    col2.metric("Average Temp", f"{latest_stats['mean']:.1f} °C")
    col3.metric("Minimum Temp", f"{latest_stats['min']:.1f} °C")
    col4.metric("UHI Threshold", f"> {latest_stats['threshold']:.1f} °C", "Hotspot Trigger")
    st.markdown("---")

# --- MULTI-LAYER OVERLAY LOGIC ---
for layer in st.session_state.layers:
    folium.raster_layers.ImageOverlay(
        image=layer["image"],
        bounds=layer["bounds"],
        opacity=layer_opacity,
        colormap=lambda x: (1, 0, 0, x),
        name=layer["name"]
    ).add_to(m)

# Only add the layer toggle menu if we actually have layers to show
if st.session_state.layers:
    folium.LayerControl().add_to(m)

# Add the "Draw" plugin and display
Draw(export=True).add_to(m)
# By adding the key, we force the map to rebuild when opacity changes!
output = st_folium(m, width=1000, height=500, key=f"map_layer_opacity_{layer_opacity}")

if "user_polygon" not in st.session_state:
    st.session_state.user_polygon = None

# --- CAPTURING THE INPUT ---
# 1. Save the drawing to the inventory so it survives reruns!
if output["all_drawings"]:
    st.session_state.user_polygon = output["all_drawings"][-1]["geometry"]

# 2. Use the inventory as our source of truth
if st.session_state.user_polygon:
    coords = st.session_state.user_polygon["coordinates"][0]
    
    # Calculate the Bounding Box
    # The "Pac-Man" Fix: Wrap longitudes back to the -180 to 180 range
    lons = [((c[0] + 180) % 360) - 180 for c in coords]
    lats = [c[1] for c in coords]
    bbox = [min(lons), min(lats), max(lons), max(lats)]
    
    #st.write(f"Generated BBox: {bbox}")

    # Connect to the Satellite Catalog
    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )

    # Search for Landsat Data
    with st.spinner("Querying Planetary Computer STAC API for cloud-free assets..."):
        search = catalog.search(
            collections=["landsat-c2-l2"],
            bbox=bbox,
            datetime=f"{target_year}-01-01/{target_year}-12-31",
            query={"eo:cloud_cover": {"lt": 10}}
        )
        items = search.item_collection()

    if len(items) > 0:
        st.success(f"Successfully retrieved {len(items)} low-cloud satellite passes for {target_year}.")
        item_dates = [item.datetime.strftime("%Y-%m-%d") for item in items]
        selected_date = st.selectbox("Which date would you like to analyze?", item_dates)
        
        if st.button("Generate Interactive Heat Map 🌡️"):
            with st.spinner("Processing thermal bands & calculating Land Surface Temperature..."):
                
                selected_item = next(item for item in items if item.datetime.strftime("%Y-%m-%d") == selected_date)
                thermal_url = selected_item.assets["lwir11"].href
                
                # 1. Open the raw data
                ds = rioxarray.open_rasterio(thermal_url)
                
                # 2. THE REPROJECTION SPELL: Convert UTM meters to GPS degrees (Fixes the US Bug!)
                ds_gps = ds.rio.reproject("EPSG:4326")
                
                # 3. Clip the correctly projected image to the user's polygon
                ds_clipped = ds_gps.rio.clip([st.session_state.user_polygon], crs="epsg:4326")
                
                # 4. THE VOID FILTER: Ignore empty space outside the satellite path
                ds_valid = ds_clipped.where(ds_clipped > 0)
                
                # 5. Calculate Celsius ONLY on valid pixels
                temp_celsius = (ds_valid * 0.00341802 + 149.0) - 273.15
                
                # --- THE STAT TRACKER & UHI MATH ---
                # Get the exact stats for the polygon
                mean_temp = float(temp_celsius.mean().item())
                std_temp = float(temp_celsius.std().item())
                max_temp = float(temp_celsius.max().item())
                min_temp = float(temp_celsius.min().item())
                
                # Calculate the UHI Threshold (Mean + 0.5 Sigma)
                uhi_threshold = mean_temp + (0.5 * std_temp)
                
                # ISOLATE HOTSPOTS: Erase everything below the threshold!
                hotspots = temp_celsius.where(temp_celsius > uhi_threshold)
                
                # 6. Generate colored image in memory (Plotting ONLY the hotspots)
                fig, ax = plt.subplots()
                ax.set_axis_off()
                fig.patch.set_alpha(0) 
                
                # Using 'Reds' or 'inferno' here looks incredible over the normal map
                im = hotspots.plot(ax=ax, cmap="inferno", add_colorbar=False)
                
                img_buffer = io.BytesIO()
                plt.savefig(img_buffer, format="png", bbox_inches='tight', pad_inches=0, transparent=True)
                
                # 7. Get GPS boundaries
                bounds = temp_celsius.rio.bounds()
                folium_bounds = [[bounds[1], bounds[0]], [bounds[3], bounds[2]]]
                
                # 8. Encode for the web
                image_bytes = img_buffer.getvalue()
                encoded_image = base64.b64encode(image_bytes).decode('utf-8')
                data_url = f"data:image/png;base64,{encoded_image}"
                
                # 9. Save to the Multi-Layer Inventory WITH STATS!
                new_layer = {
                    "name": f"UHI Hotspots ({selected_date})",
                    "image": data_url,
                    "bounds": folium_bounds,
                    "stats": {
                        "max": max_temp,
                        "min": min_temp,
                        "mean": mean_temp,
                        "threshold": uhi_threshold
                    }
                }
                st.session_state.layers.append(new_layer)
                
                # 10. Update the camera so it doesn't teleport away!
                center_lat = (folium_bounds[0][0] + folium_bounds[1][0]) / 2
                center_lon = (folium_bounds[0][1] + folium_bounds[1][1]) / 2
                st.session_state.map_center = [center_lat, center_lon]
                st.session_state.map_zoom = 13
                
                # 11. Reload the UI safely!
                st.rerun()
    else:
        st.error("No clear images found. Try a different year or area.")
else:
    st.warning("Please draw a polygon on the map to start.")