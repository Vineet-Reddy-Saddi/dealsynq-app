"""
Building footprint & site metrics — the "aerial/CV" layer, mostly FREE via OpenStreetMap.

OSM has building POLYGONS for a large share of buildings. From the footprint polygon we
derive: footprint area, roof area (~= footprint for flat commercial roofs), estimated
height (from OSM levels/height tags), plus site estimates (parking capacity, solar
potential, existing FAR) computed from footprint + parcel land area.

Honest confidence:
  - footprint_sqft, levels/height : DIRECT from OSM where the building is mapped.
  - roof_area, parking, solar, far : DERIVED / ESTIMATED (clearly flagged as such).

    from springfield.footprint import site_metrics
    site_metrics(42.094, -72.501, land_sqft=859830, assessor_building_sqft=311873)
"""
import math

from springfield.businesses import overpass_query

SQFT_PER_SQM = 10.7639
FT_PER_M = 3.28084


def _poly_area_sqm(coords):
    """Area of a lat/lon polygon in square meters (local equirectangular projection)."""
    if len(coords) < 3:
        return 0.0
    lat0 = sum(c[0] for c in coords) / len(coords)
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(lat0))
    pts = [(c[1] * mlon, c[0] * mlat) for c in coords]  # (x=lon_m, y=lat_m)
    area = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def footprint(lat, lon, radius=45, deadline=None):
    """Sum of OSM building-polygon areas near the point + any levels/height tags.
    Fails fast (short HTTP timeout) rather than hanging — see overpass_query.
    `deadline`: absolute time.time() value shared with a sibling call (e.g. businesses),
    so this and other OSM lookups fired in parallel split one wall-clock budget."""
    q = f"""[out:json][timeout:5];
(way(around:{radius},{lat},{lon})[building];);
out geom tags;"""
    d = overpass_query(q, timeout=6, deadline=deadline)
    total_sqm, n, levels, height_m = 0.0, 0, None, None
    for el in d.get("elements", []):
        geom = el.get("geometry")
        if not geom:
            continue
        coords = [(g["lat"], g["lon"]) for g in geom]
        a = _poly_area_sqm(coords)
        if a < 20:  # ignore sheds/noise
            continue
        total_sqm += a
        n += 1
        t = el.get("tags", {})
        if t.get("building:levels") and not levels:
            try:
                levels = int(float(t["building:levels"]))
            except ValueError:
                pass
        if t.get("height") and not height_m:
            m = "".join(ch for ch in t["height"] if ch.isdigit() or ch == ".")
            if m:
                height_m = float(m)
    if n == 0:
        return None
    return {
        "footprint_sqft": round(total_sqm * SQFT_PER_SQM),
        "osm_building_count": n,
        "levels": levels,
        "height_m": height_m,
    }


def site_metrics(lat, lon, land_sqft=0, assessor_building_sqft=0, stories=None, deadline=None):
    """Footprint (OSM) + derived roof / height / parking / solar / FAR estimates."""
    # scale the footprint search radius to parcel size (a big plaza's buildings can sit
    # 100m+ from the parcel centroid; a house's footprint is right at the point)
    if land_sqft and land_sqft > 0:
        land_m2 = land_sqft / SQFT_PER_SQM
        radius = max(35, min(160, round(math.sqrt(land_m2) * 0.6)))
    else:
        radius = 45
    fp = None
    try:
        fp = footprint(lat, lon, radius=radius, deadline=deadline)
    except Exception:
        fp = None

    out = {"footprint_source": "OpenStreetMap building polygons" if fp else None}

    footprint_sqft = fp["footprint_sqft"] if fp else None
    # if OSM has no footprint but the assessor knows a single-story building area, that IS the footprint
    if not footprint_sqft and assessor_building_sqft and (stories in (None, "1", 1)):
        footprint_sqft = assessor_building_sqft
        out["footprint_source"] = "assessor (single-story building area)"

    if footprint_sqft:
        out["footprint_sqft"] = footprint_sqft
        out["roof_area_sqft"] = footprint_sqft  # flat commercial roof ~= footprint (est.)

        # height: OSM height > OSM levels*11ft > assessor stories*11ft
        height_ft = None
        if fp and fp.get("height_m"):
            height_ft = round(fp["height_m"] * FT_PER_M)
        elif fp and fp.get("levels"):
            height_ft = round(fp["levels"] * 11)
        elif stories:
            try:
                height_ft = round(float(str(stories)) * 11)
            except ValueError:
                pass
        if height_ft:
            out["estimated_height_ft"] = height_ft

        # existing FAR = gross building area / land area
        if assessor_building_sqft and land_sqft:
            out["existing_far"] = round(assessor_building_sqft / land_sqft, 2)

        # parking + solar are commercial metrics — only meaningful for commercial-scale
        # buildings, not a house with a driveway.
        if footprint_sqft >= 8000:
            # parking estimate: usable open land / 325 sqft per space (stall + aisle share)
            if land_sqft and land_sqft > footprint_sqft:
                open_land = (land_sqft - footprint_sqft) * 0.65
                out["estimated_parking_spaces"] = int(open_land / 325)
            # rooftop solar estimate: 70% of roof usable, ~15 W per usable sqft
            out["estimated_solar_kw"] = round(out["roof_area_sqft"] * 0.70 * 15 / 1000)

    return out if out.get("footprint_sqft") else None


if __name__ == "__main__":
    import json
    import sys
    lat = float(sys.argv[1]) if len(sys.argv) > 1 else 42.094
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else -72.501
    print(json.dumps(site_metrics(lat, lon, land_sqft=859830, assessor_building_sqft=311873), indent=2))
