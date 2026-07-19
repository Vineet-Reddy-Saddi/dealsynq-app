"""
Text -> coordinates, for the cases plain address parsing can't resolve — a landmark name
("Hall of Fame"), or a public/mailing address that doesn't match how the assessor recorded
the parcel (MGM Springfield's real address is "1 MGM Way"; the parcel is "12-24 MGM Way").
Free, no API key: OpenStreetMap Nominatim.

This is a LAST-RESORT fallback, not a primary matcher — it returns an approximate point, not
a verified parcel, so the caller must always show it as lower-confidence than a direct
assessor match (see server.py search()'s "geocoded" mode). Nominatim's usage policy caps the
public instance at ~1 request/second; the pacing lock below enforces that regardless of how
many concurrent searches hit this fallback.

    from springfield.geocode import geocode
    geocode("Naismith Memorial Basketball Hall of Fame", bbox=(42.05,-72.60,42.15,-72.45))
"""
import json
import threading
import time
import urllib.parse
import urllib.request

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim requires a real identifying User-Agent (not a browser UA) per its usage policy.
HEADERS = {"User-Agent": "DealSynq-PropertyIntel/1.0 (property lookup demo)"}

MIN_GAP_SECONDS = 1.1
_PACE_LOCK = threading.Lock()
_last_call = [0.0]


def geocode(query, bbox=None, timeout=6):
    """Best single match for `query`, or None. `bbox` is (min_lat,min_lon,max_lat,max_lon) —
    when given, results are constrained to it (Nominatim's viewbox+bounded=1) so a landmark
    name common in many cities doesn't resolve somewhere else entirely.

    Returns {"lat", "lon", "display_name", "importance"} — importance (Nominatim's own
    0-1 relevance score) is surfaced so callers can judge confidence, not just trust a hit."""
    q = query if "springfield" in query.lower() else f"{query}, Springfield, MA"
    params = {"q": q, "format": "json", "limit": 1, "addressdetails": 0, "countrycodes": "us"}
    if bbox:
        # Nominatim wants viewbox as lon,lat pairs: left,top,right,bottom
        params["viewbox"] = f"{bbox[1]},{bbox[2]},{bbox[3]},{bbox[0]}"
        params["bounded"] = 1
    url = NOMINATIM_URL + "?" + urllib.parse.urlencode(params)

    with _PACE_LOCK:
        gap = MIN_GAP_SECONDS - (time.time() - _last_call[0])
        if gap > 0:
            time.sleep(gap)
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
        finally:
            _last_call[0] = time.time()

    if not data:
        return None
    hit = data[0]
    try:
        return {"lat": float(hit["lat"]), "lon": float(hit["lon"]),
                "display_name": hit.get("display_name", ""),
                "importance": hit.get("importance")}
    except (KeyError, ValueError):
        return None
