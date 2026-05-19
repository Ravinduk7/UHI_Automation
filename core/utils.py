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

def process_user_polygon(user_polygon):
    """Takes a Folium drawing geometry, returns BBox, Area (sq km), and Max Dimension (km)."""
    coords = user_polygon["coordinates"][0]
    
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