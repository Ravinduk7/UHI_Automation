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

# --- UI SETUP ---
st.set_page_config(page_title="Urban Heat Island Architect", layout="wide")

# --- INVENTORY (SESSION STATE) SETUP ---
if "heatmap_bounds" not in st.session_state:
    st.session_state.heatmap_bounds = None
if "heatmap_image" not in st.session_state:
    st.session_state.heatmap_image = None


st.title("🏙️ Urban Heat Island Automation Tool")
st.markdown("Draw a boundary and select a year to identify local hotspots.")

# --- SIDEBAR INPUTS ---
with st.sidebar:
    st.header("Search Parameters")
    # Step 1: Select Year
    target_year = st.number_input("Select Year for Analysis", 
                                 min_value=2013, max_value=2026, value=2024)
    
    st.info("Note: Landsat 8/9 data is available from 2013 onwards.")

# --- THE INTERACTIVE MAP ---
st.subheader("1. Define your Area of Interest")

# Initialize the map
m = folium.Map(location=[6.9271, 79.8612], zoom_start=12)

# --- NEW OVERLAY LOGIC ---
if st.session_state.heatmap_image is not None:
    # Add the heat layer to the interactive map
    folium.raster_layers.ImageOverlay(
        image=st.session_state.heatmap_image,
        bounds=st.session_state.heatmap_bounds,
        opacity=0.7,
        colormap=lambda x: (1, 0, 0, x), # Fallback colormap
        name="Surface Temperature"
    ).add_to(m)
    
    # Add a layer control so the user can toggle the heat map on and off!
    folium.LayerControl().add_to(m)

# Add the "Draw" plugin
Draw(export=True).add_to(m)

# Display the map
output = st_folium(m, width=1000, height=500)

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
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    bbox = [min(lons), min(lats), max(lons), max(lats)]
    
    st.write(f"Generated BBox: {bbox}")

    # Connect to the Satellite Catalog
    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )

    # Search for Landsat Data
    with st.spinner("Hunting for clear satellite passes..."):
        search = catalog.search(
            collections=["landsat-c2-l2"],
            bbox=bbox,
            datetime=f"{target_year}-01-01/{target_year}-12-31",
            query={"eo:cloud_cover": {"lt": 10}}
        )
        items = search.item_collection()

    if len(items) > 0:
        st.success(f"Found {len(items)} clear satellite images for {target_year}!")
        item_dates = [item.datetime.strftime("%Y-%m-%d") for item in items]
        selected_date = st.selectbox("Which date would you like to analyze?", item_dates)
        
        if st.button("Generate Interactive Heat Map 🌡️"):
            with st.spinner("Reprojecting, clipping, and calculating... (Hands off the map!)"):
                
                selected_item = next(item for item in items if item.datetime.strftime("%Y-%m-%d") == selected_date)
                thermal_url = selected_item.assets["lwir11"].href
                
                # Open the raw data
                ds = rioxarray.open_rasterio(thermal_url)
                
                # THE REPROJECTION SPELL: Convert UTM meters to GPS degrees!
                ds_gps = ds.rio.reproject("EPSG:4326")
                
                # Clip the correctly projected image to the user's polygon
                ds_clipped = ds_gps.rio.clip([st.session_state.user_polygon], crs="epsg:4326")
                
                # Calculate Celsius 
                temp_celsius = (ds_clipped * 0.00341802 + 149.0) - 273.15
                
                # Generate colored image in memory
                fig, ax = plt.subplots()
                ax.set_axis_off()
                fig.patch.set_alpha(0) 
                
                im = temp_celsius.plot(ax=ax, cmap="inferno", add_colorbar=False)
                
                img_buffer = io.BytesIO()
                plt.savefig(img_buffer, format="png", bbox_inches='tight', pad_inches=0, transparent=True)
                
                bounds = temp_celsius.rio.bounds()
                folium_bounds = [[bounds[1], bounds[0]], [bounds[3], bounds[2]]]
                
                # Encode for the web
                image_bytes = img_buffer.getvalue()
                encoded_image = base64.b64encode(image_bytes).decode('utf-8')
                data_url = f"data:image/png;base64,{encoded_image}"
                
                # Save results to inventory
                st.session_state.heatmap_image = data_url
                st.session_state.heatmap_bounds = folium_bounds
                
                # Reload the UI safely!
                st.rerun()
    else:
        st.error("No clear images found. Try a different year or area.")
else:
    st.warning("Please draw a polygon on the map to start.")