import streamlit as st
import folium
from streamlit_folium import st_folium
import zipfile
import io
import gc
from core.utils import (
    process_user_polygon,
    init_master_map,
    build_flood_report_context,
    create_word_report,
    generate_insights
)
from engines.flood_engine import run_flood_pipeline

def render_flood_module():
    st.title("🌊 Flood Risk Assessor")
    st.markdown("### SAR Inundation Probability Pipeline")
    st.divider()

    # --- BACKGROUND INVENTORY INIT ---
    if "flood_inventory" not in st.session_state:
        st.session_state.flood_inventory = []
    if "current_drawing" not in st.session_state:
        st.session_state.current_drawing = None

    # --- SIDEBAR CONTROLS ---
    with st.sidebar:
        st.header("Risk Parameters")
        years_back = st.slider("Historical Window (Years)", 1, 5, 3)

        selected_basemap = st.selectbox(
            "Base Map Style",
            ["Dark Mode Map", "Satellite View", "Light Minimal", "OpenStreetMap"],
            index=0,
            key="active_basemap_style"
        )

        layer_opacity = st.slider("Result Opacity", 0.1, 1.0, 0.7, 0.1)
        st.divider()

        # The Cleaned-Up Generate Button
        if st.button("🚀 Run Risk Assessment", type="primary", width="stretch"):
            if st.session_state.current_drawing is not None:
                with st.spinner("Scouting historical storms & fetching radar... (Takes 30-60s)"):
                    result = run_flood_pipeline(st.session_state.current_drawing, years_back)
                    
                    if "error" in result:
                        st.error(result["error"])
                    else:
                        run_id = len(st.session_state.flood_inventory) + 1
                        st.session_state.flood_inventory.append({
                            "id": run_id,
                            "polygon": st.session_state.current_drawing,
                            "result": result,
                            "show_master": True,
                            "active_layers": {}
                        })
                        st.rerun()
            else:
                st.error("Please draw an Area of Interest (AOI) on the map first.")

        # --- DYNAMIC MULTI-LAYER MANAGER ---
        if st.session_state.flood_inventory:
            st.success(f"✅ Inventory Active ({len(st.session_state.flood_inventory)} regions)")
            
            for i, item in enumerate(st.session_state.flood_inventory):
                with st.expander(f"📍 Area {item['id']} Manager", expanded=(i == len(st.session_state.flood_inventory)-1)):
                    res = item["result"]
                    if "visual_previews" in res:
                        st.write("**Context Previews**")
                        cols = st.columns(3)
                        previews = res["visual_previews"]
                        
                        with cols[0]:
                            if previews.get("dem_map"):
                                st.image(f"data:image/png;base64,{previews['dem_map']}", caption="Elevation")
                        with cols[1]:
                            if previews.get("wc_map"):
                                st.image(f"data:image/png;base64,{previews['wc_map']}", caption="Land Cover")
                        with cols[2]:
                            if previews.get("jrc_map"):
                                st.image(f"data:image/png;base64,{previews['jrc_map']}", caption="Water Mask")
                    
                    item["show_master"] = st.checkbox(
                        "🌊 Rainfall-Weighted Flood Susceptibility", 
                        value=item.get("show_master", True), 
                        key=f"master_{item['id']}",
                        help="Susceptibility score is weighted by historical rainfall return periods, not raw flood frequency."
                    )

                    if "debug_mask_b64" in res and res["debug_mask_b64"]:
                        item["show_debug"] = st.checkbox(
                            "🛡️ Environment Shield Mask", 
                            value=item.get("show_debug", False), 
                            key=f"debug_{item['id']}"
                        )
                    
                    st.divider()
                    st.write("**Toggle Individual Storms**")
                    
                    for tier_key, tier_data in res['individual_layers'].items():
                        storm_date = tier_data["date"]
                        is_checked = item["active_layers"].get(tier_key, False)
                        
                        item["active_layers"][tier_key] = st.checkbox(
                            f"🌩️ {tier_key} ({storm_date})", 
                            value=is_checked, 
                            key=f"chk_{item['id']}_{tier_key}"
                        )
                    
                    st.divider()
                    with st.popover(f"🗑️ Delete Area {item['id']}", help="Remove this analysis"):
                        st.markdown("**Delete this area?**")
                        if st.button("Confirm", key=f"confirm_del_{item['id']}", type="primary"):
                            st.session_state.flood_inventory.pop(i)
                            gc.collect() # Force free RAM from high-fidelity arrays immediately
                            st.rerun()

    # --- MAP HUD ---    
    st.write("📍 **Step 1: Draw your Area of Interest (AOI) and hit Run.**")

    m = init_master_map(center_lat=7.8731, center_lon=80.7718, zoom_start=7, selected_basemap=selected_basemap)
    
    folium.plugins.Draw(
        export=True,
        position='topleft',
        draw_options={'polyline': False, 'polygon': True, 'circle': False, 'marker': False, 'circlemarker': False}
    ).add_to(m)

    def get_aoi_style(feature):
        return {'color': '#ff0000', 'fillColor': 'transparent', 'weight': 2}
    
    # --- MULTI-OVERLAY PAINTER ---
    for item in st.session_state.flood_inventory:
        res = item["result"]
        
        try:
            bbox, _, _ = process_user_polygon(item["polygon"])
        except ValueError:
            st.error(f"Area {item['id']} contains corrupted geometry and could not be rendered.")
            continue
            
        bounds = [[bbox[1], bbox[0]], [bbox[3], bbox[2]]]
        
        if item == st.session_state.flood_inventory[-1]:
            m.fit_bounds(bounds)
        
        feature = {
            "type": "Feature",
            "geometry": item["polygon"],
            "properties": {}
        }
            
        folium.GeoJson(
            feature,
            style_function=get_aoi_style
        ).add_to(m)
        
        for tier_key, is_active in item["active_layers"].items():
            if is_active:
                layer_b64 = res['individual_layers'][tier_key]['image_b64']
                storm_date = res['individual_layers'][tier_key]['date']
                
                folium.raster_layers.ImageOverlay(
                    image=f"data:image/png;base64,{layer_b64}",
                    bounds=bounds,
                    opacity=layer_opacity,
                    name=f"Storm {storm_date}"
                ).add_to(m)
        
        if item["show_master"]:
            folium.raster_layers.ImageOverlay(
                image=f"data:image/png;base64,{res['heatmap_b64']}",
                bounds=bounds,
                opacity=layer_opacity,
                name="Master Susceptibility"
            ).add_to(m)

        if item.get("show_debug") and "debug_mask_b64" in res and res["debug_mask_b64"]:
            folium.raster_layers.ImageOverlay(
                image=f"data:image/png;base64,{res['debug_mask_b64']}",
                bounds=bounds,
                opacity=layer_opacity,
                name="Debug Shield"
            ).add_to(m)

    folium.LayerControl(position="topright").add_to(m)

    output = st_folium(m, width=1000, height=500, key="flood_master_map", returned_objects=["last_active_drawing"])

    if output and isinstance(output, dict):
        if output.get("last_active_drawing"):
            st.session_state.current_drawing = output["last_active_drawing"]["geometry"]
    
    # --- STATS TRACKER TABLE ---
    if st.session_state.flood_inventory:
        st.subheader("📊 Selected Area Statistics Tracker")
        st.caption("ℹ️ Susceptibility score is weighted by historical rainfall return periods, not raw flood frequency.")
        
        table_data = []
        for item in st.session_state.flood_inventory:
            area_id = f"Area {item['id']}"
            res = item["result"]
            
            analyzed_area = res.get('master_stats', {}).get('selected_area_sqkm', 'N/A')
            high_susceptibility = res.get('master_stats', {}).get('high_susceptibility_sqkm', 'N/A')
            
            for tier_key, tier_data in res['individual_layers'].items():
                rp = tier_data.get('return_period', 'N/A')
                
                table_data.append({
                    "Area": area_id,
                    "Selected Area (sq km)": analyzed_area,
                    "High Susceptibility Area (sq km)": high_susceptibility,
                    "Storm Tier": tier_key,
                    "Date": tier_data["date"],
                    "Rainfall (mm)": tier_data["rainfall_mm"],
                    "Tier Flooded (sq km)": tier_data["flooded_sqkm"],
                    "Return Period": f"1-in-{rp} yr" if rp != "N/A" else "N/A"
                })
                
        st.dataframe(table_data, width="stretch", hide_index=True)

        # --- EXTRACT TARGETED DATA (ZIP DOWNLOADER) ---
        st.divider()
        st.subheader("📦 Extract GIS Data")
        
        download_options = ["All Active Areas Combined"] + [f"Area {item['id']}" for item in st.session_state.flood_inventory]
        selected_download = st.selectbox("🎯 Choose Extraction Target", download_options)
        
        st.write(f"Preparing raw high-fidelity GeoTIFF targets for **{selected_download}**.")

        if selected_download == "All Active Areas Combined":
            targets_to_zip = st.session_state.flood_inventory
            file_name_suffix = "All_Areas"
        else:
            selected_id = int(selected_download.split()[-1])
            targets_to_zip = [item for item in st.session_state.flood_inventory if item["id"] == selected_id]
            file_name_suffix = f"Area_{selected_id}"

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for item in targets_to_zip:
                area_folder = f"Area_{item['id']}"
                res = item["result"]
                
                deliverables = res.get("deliverables", {})
                if deliverables.get("master_tiff"):
                    zip_file.writestr(f"{area_folder}/Master_Susceptibility.tif", deliverables["master_tiff"])
                if deliverables.get("jrc_tiff"):
                    zip_file.writestr(f"{area_folder}/JRC_Water_Mask.tif", deliverables["jrc_tiff"])
                if deliverables.get("worldcover_tiff"):
                    zip_file.writestr(f"{area_folder}/ESA_WorldCover.tif", deliverables["worldcover_tiff"])
                
                for tier_key, tier_data in res.get("individual_layers", {}).items():
                    if "tiff_bytes" in tier_data and tier_data["tiff_bytes"]:
                        file_name = f"{area_folder}/{tier_key}_{tier_data['date']}.tif"
                        zip_file.writestr(file_name, tier_data["tiff_bytes"])
        
        st.download_button(
            label=f"💾 Download {selected_download} Bundle",
            data=zip_buffer.getvalue(),
            file_name=f"Flood_Analysis_{file_name_suffix}.zip",
            mime="application/zip",
            type="primary"
        )
        zip_buffer.close()

        # --- REPORT GENERATION PANEL ---
        st.divider()
        st.subheader("📄 Generate Intelligence Report")

        report_options = [f"Area {item['id']}" for item in st.session_state.flood_inventory]
        
        if report_options:
            selected_report = st.selectbox("🎯 Choose Report Target", report_options)
            
            selected_id = int(selected_report.split()[-1])
            item = next(i for i in st.session_state.flood_inventory if i["id"] == selected_id)
            res = item["result"]

            # Use centralized context generator from utils
            context = build_flood_report_context(res)
            threshold_text, high_risk_text = generate_insights(res)

            # Supplement contextual fields and custom templates markers
            context.update({
                "region_name": f"Area {selected_id}",
                "dem_map": res['visual_previews'].get("dem_map") or "",
                "wc_map": res['visual_previews'].get("wc_map") or "",
                "jrc_map": res['visual_previews'].get("jrc_map") or "",
                "critical_thresholds": threshold_text,
                "high_risk_zones": high_risk_text
            })

            report_bytes = create_word_report("templates/flood_template.docx", context)

            st.download_button(
                label=f"減️ Download {selected_report} Report",
                data=report_bytes,
                file_name=f"Flood_Report_Area_{selected_id}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                type="primary"
            )
            del context, report_bytes
        else:
            st.info("Run an assessment first to unlock report generation.")

if __name__ == "__main__":
    render_flood_module()