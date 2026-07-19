"""
Yelp Fusion API — real businesses at/near a point, kept current BY the businesses themselves
(owners update hours/status, Yelp drops permanently-closed listings from search results) —
a meaningfully more accurate source than OpenStreetMap's volunteer-mapped POIs for "what's
actually operating here" (see springfield/businesses.py, the free OSM source this
supplements). Free tier: 5,000 calls/day.

Needs YELP_API_KEY (free at https://www.yelp.com/developers — Create App under Fusion API).
Gracefully returns None when no key is configured or the call fails — NOT an error, just a
signal for the caller to fall back to the free OSM source instead of showing nothing.

    from springfield.yelp import find_businesses_yelp
    find_businesses_yelp(42.094, -72.501)
"""
import json
import os
import urllib.parse
import urllib.request

API_KEY = os.environ.get("YELP_API_KEY")
SEARCH_URL = "https://api.yelp.com/v3/businesses/search"


def find_businesses_yelp(lat, lon, radius=70, limit=15, timeout=6):
    """[{name, type, brand, cuisine, distance_m, lat, lon}] near (lat, lon), sorted by
    distance — same shape as businesses.find_businesses() so callers can use either
    interchangeably. Returns None (not []) when unavailable, distinct from a real "found
    zero businesses" result, so the caller knows to fall back rather than trust an empty
    list."""
    if not API_KEY:
        return None
    params = {"latitude": lat, "longitude": lon, "radius": min(int(radius), 40000),
              "limit": min(limit, 50), "sort_by": "distance"}
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None

    out = []
    for b in data.get("businesses", []):
        cats = [c.get("title") for c in (b.get("categories") or []) if c.get("title")]
        coords = b.get("coordinates") or {}
        out.append({
            "name": b.get("name"),
            "type": cats[0] if cats else "",
            "brand": None,   # Yelp doesn't separate a chain brand from the listing name
            "cuisine": ", ".join(cats[1:3]) if len(cats) > 1 else None,
            "distance_m": round(b["distance"]) if b.get("distance") is not None else None,
            "lat": coords.get("latitude"), "lon": coords.get("longitude"),
        })
    return out


if __name__ == "__main__":
    import sys
    lat = float(sys.argv[1]) if len(sys.argv) > 1 else 42.094
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else -72.501
    res = find_businesses_yelp(lat, lon)
    if res is None:
        print("No YELP_API_KEY configured (or the call failed) — set it to test.")
    else:
        for b in res:
            print(f"  {b['distance_m']:>4}m  {b['name']}  ({b['type']})")
