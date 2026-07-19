"""
Foursquare Places API — real businesses at/near a point, kept current BY the businesses
themselves (Foursquare tracks open/closed status and drops permanently-closed places from
search results) — a meaningfully more accurate source than OpenStreetMap's volunteer-mapped
POIs for "what's actually operating here" (see springfield/businesses.py, the free OSM
source this supplements). Genuinely free for the first 500 calls/month, no subscription
(unlike Yelp's current paid-only Places API), then pay-as-you-go.

Needs FOURSQUARE_API_KEY (free at https://foursquare.com/developers — Create a project,
then generate a Service API Key). Gracefully returns None when no key is configured or the
call fails — NOT an error, just a signal for the caller to fall back to the free OSM source
instead of showing nothing.

    from springfield.foursquare import find_businesses_foursquare
    find_businesses_foursquare(42.094, -72.501)
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API_KEY = os.environ.get("FOURSQUARE_API_KEY")
SEARCH_URL = "https://places-api.foursquare.com/places/search"
API_VERSION = "2025-06-17"


def find_businesses_foursquare(lat, lon, radius=70, limit=15, timeout=6):
    """[{name, type, brand, cuisine, distance_m, lat, lon}] near (lat, lon), sorted by
    distance — same shape as businesses.find_businesses() / yelp.find_businesses_yelp() so
    callers can use any of the three interchangeably. Returns None (not []) when
    unavailable, distinct from a real "found zero businesses" result, so the caller knows to
    fall back rather than trust an empty list."""
    if not API_KEY:
        return None
    params = {"ll": f"{lat},{lon}", "radius": min(int(radius), 100000), "limit": min(limit, 50)}
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {API_KEY}",
        "X-Places-Api-Version": API_VERSION,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        # log the actual reason (bad key, wrong plan, bad params, etc.) — silently returning
        # None here previously made every failure indistinguishable and unfixable from the
        # logs, always just masked by the OSM fallback.
        try:
            body = e.read().decode("utf-8", "ignore")[:500]
        except Exception:
            body = ""
        print(f"  [foursquare] HTTP {e.code}: {body}")
        return None
    except Exception as e:
        print(f"  [foursquare] request failed: {e}")
        return None

    # be defensive about the exact envelope shape (a top-level "results" list vs. the list
    # itself) — cheap insurance against a docs/behavior mismatch, never a crash either way.
    places = data.get("results") if isinstance(data, dict) else data
    if not isinstance(places, list):
        return None

    out = []
    for p in places:
        cats = [c.get("name") for c in (p.get("categories") or []) if c.get("name")]
        out.append({
            "name": p.get("name"),
            "type": cats[0] if cats else "",
            "brand": None,
            "cuisine": ", ".join(cats[1:3]) if len(cats) > 1 else None,
            "distance_m": round(p["distance"]) if p.get("distance") is not None else None,
            "lat": p.get("latitude"), "lon": p.get("longitude"),
        })
    return out


if __name__ == "__main__":
    import sys
    lat = float(sys.argv[1]) if len(sys.argv) > 1 else 42.094
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else -72.501
    res = find_businesses_foursquare(lat, lon)
    if res is None:
        print("No FOURSQUARE_API_KEY configured (or the call failed) — set it to test.")
    else:
        for b in res:
            print(f"  {b['distance_m']:>4}m  {b['name']}  ({b['type']})")
