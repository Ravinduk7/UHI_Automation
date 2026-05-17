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
import os
import tempfile
from docxtpl import DocxTemplate, InlineImage
from docx.shared import Cm
import contextily as cx
import geopandas as gpd
from shapely.geometry import shape

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
                
                # --- 6. GENERATE THE 3 IMAGES FOR THE REPORT ---
                
                # Helper function to convert a matplotlib figure to base64
                def fig_to_base64(fig):
                    buf = io.BytesIO()
                    fig.savefig(buf, format="png", bbox_inches='tight', pad_inches=0, transparent=True)
                    plt.close(fig) # Close to save RAM
                    return base64.b64encode(buf.getvalue()).decode('utf-8')

                # IMAGE 1: Isolated Hotspots (The one you already had)
                fig1, ax1 = plt.subplots()
                ax1.set_aspect('equal')
                ax1.set_axis_off()
                fig1.patch.set_alpha(0)
                hotspots.plot(ax=ax1, cmap="inferno", add_colorbar=False, add_labels=False)
                hotspot_b64 = fig_to_base64(fig1)

                # IMAGE 2: Full Thermal Gradient (Everything, not just hotspots)
                fig2, ax2 = plt.subplots()
                ax2.set_aspect('equal')
                ax2.set_axis_off()
                fig2.patch.set_alpha(0)
                # Adding a colorbar here helps the client understand the scale!
                temp_celsius.plot(ax=ax2, cmap="magma", add_colorbar=True, add_labels=False)
                full_thermal_b64 = fig_to_base64(fig2)

                # IMAGE 3: Contextual Basemap (The 0.5-second optimization!)
                geom = shape(st.session_state.user_polygon)
                gdf = gpd.GeoDataFrame({'geometry': [geom]}, crs="EPSG:4326")
                gdf_web = gdf.to_crs(epsg=3857)
                
                fig3, ax3 = plt.subplots(figsize=(6, 6)) # Keep a good resolution
                ax3.set_axis_off()
                fig3.patch.set_alpha(0)
                gdf_web.boundary.plot(ax=ax3, color="red", linewidth=2)
                cx.add_basemap(ax3, crs=gdf_web.crs.to_string(), source=cx.providers.Esri.WorldImagery)
                true_color_b64 = fig_to_base64(fig3)
                
                # --- 7. STASH GEOTIFF IN MEMORY ---
                # We do this here so it's safely packed into the inventory layer
                with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
                    temp_filepath = tmp.name
                temp_celsius.rio.to_raster(temp_filepath)
                with open(temp_filepath, "rb") as file:
                    tiff_bytes = file.read()
                os.remove(temp_filepath)

                # --- 8. SAVE EVERYTHING TO INVENTORY ---
                bounds = temp_celsius.rio.bounds()
                folium_bounds = [[bounds[1], bounds[0]], [bounds[3], bounds[2]]]
                
                # Single, clean layer creation!
                new_layer = {
                    "name": f"UHI Hotspots ({selected_date})",
                    "image": f"data:image/png;base64,{hotspot_b64}", # Keep the hotspot for the Folium map
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
                st.session_state.layers.append(new_layer)
                
                # 9. Update the camera so it doesn't teleport away!
                center_lat = (folium_bounds[0][0] + folium_bounds[1][0]) / 2
                center_lon = (folium_bounds[0][1] + folium_bounds[1][1]) / 2
                st.session_state.map_center = [center_lat, center_lon]
                st.session_state.map_zoom = 13
                
                # 10. Reload the UI safely!
                st.rerun()
    else:
        st.error("No clear images found. Try a different year or area.")
else:
    st.warning("Please draw a polygon on the map to start.")


# --- EXPORT INTELLIGENCE HUB ---
if st.session_state.layers:
    st.divider()
    st.subheader("📥 Export Geospatial Intelligence")
    st.markdown("Download the raw spatial data or the automated executive report.")
    
    # 1. Select Layer
    layer_names = [layer["name"] for layer in st.session_state.layers]
    
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        selected_export_name = st.selectbox("Select Layer to Export:", layer_names)
    
    selected_layer = next(layer for layer in st.session_state.layers if layer["name"] == selected_export_name)
    
    # --- REPORT GENERATION MAGIC ---
    # We strip the "UHI Hotspots ( )" text just to get the raw date for the report
    clean_date = selected_export_name.replace("UHI Hotspots (", "").replace(")", "")
    
    # Load the template
    doc = DocxTemplate("template.docx")
    
    # Helper function to decode images for docxtpl
    def inject_image(base64_str, width_cm=15):
        img_stream = io.BytesIO(base64.b64decode(base64_str))
        return InlineImage(doc, img_stream, width=Cm(width_cm))

    # Map our Python variables to your Word {{ placeholders }}
    context = {
        "target_year": target_year,
        "selected_date": clean_date,
        "max_temp": f"{selected_layer['stats']['max']:.1f}",
        "mean_temp": f"{selected_layer['stats']['mean']:.1f}",
        "min_temp": f"{selected_layer['stats']['min']:.1f}",
        "threshold": f"{selected_layer['stats']['threshold']:.1f}",
        
        # Inject all three images!
        "heatmap_image": inject_image(selected_layer["report_images"]["hotspot"]),
        "full_thermal_image": inject_image(selected_layer["report_images"]["full_thermal"]),
        "true_color_image": inject_image(selected_layer["report_images"]["true_color"])
    }
    
    # Render and save to RAM buffer
    doc.render(context)
    report_buffer = io.BytesIO()
    doc.save(report_buffer)
    report_bytes = report_buffer.getvalue()
    
    # --- RENDER THE DOWNLOAD BUTTONS ---
    with col2:
        st.write("") # Spacing 
        st.write("") 
        st.download_button(
            label="Download GeoTIFF 🗺️",
            data=selected_layer["tiff_bytes"],
            file_name=f"{selected_export_name}.tif",
            mime="image/tiff",
            type="primary",
            use_container_width=True
        )
        
    with col3:
        st.write("") 
        st.write("") 
        st.download_button(
            label="Download Report 📄",
            data=report_bytes,
            file_name=f"UHI_Report_{clean_date}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            type="secondary",
            use_container_width=True
        )