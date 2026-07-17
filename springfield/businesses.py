"""
What business actually operates here? — general POI lookup via OpenStreetMap Overpass.

The assessor says "324: Supermarket"; this says "Big Y". Free, no API key, works
anywhere in the world (OSM is global). Given a lat/lon, returns nearby named businesses
with brand / type / cuisine, sorted by distance.

    from springfield.businesses import find_businesses
    find_businesses(42.094, -72.501)
"""
import json
import math
import urllib.parse
import urllib.request

# The public Overpass instances are frequently overloaded (504/429); rotate across mirrors.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
HEADERS = {"User-Agent": "DealSynq-PropertyIntel/1.0"}


def overpass_query(q, timeout=6, max_mirrors=2):
    """Run an Overpass QL query, rotating across mirrors. Short per-mirror timeout and a
    cap on how many mirrors we try — these are free public servers that are often
    overloaded (slow, not always down), and this call MUST fail fast: it backs a live
    web request, so a 60s hang here means a stuck page for the user."""
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for endpoint in OVERPASS_ENDPOINTS[:max_mirrors]:
        try:
            req = urllib.request.Request(endpoint, data=data, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"all Overpass mirrors failed/slow: {last}")


def _haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_businesses(lat, lon, radius=70, limit=15, timeout=6, max_mirrors=4):
    """Return [{name, type, brand, cuisine, distance_m}] for named POIs within `radius` m.
    `timeout` is the HTTP request budget (per mirror) — kept short so a slow/overloaded
    public Overpass server fails fast instead of hanging the caller. `max_mirrors` is how
    many of the public mirrors to try before giving up; these are free, frequently
    overloaded servers, so trying all of them materially improves the hit rate."""
    q = f"""[out:json][timeout:5];
(
  nwr(around:{radius},{lat},{lon})[name][shop];
  nwr(around:{radius},{lat},{lon})[name][amenity~"restaurant|fast_food|cafe|bank|pharmacy|fuel|car_wash|cinema|gym|fitness_centre|marketplace|bar|pub|fitness"];
  nwr(around:{radius},{lat},{lon})[name][office];
  nwr(around:{radius},{lat},{lon})[name][leisure~"fitness_centre|sports_centre|bowling_alley"];
);
out center tags {limit * 4};"""
    d = overpass_query(q, timeout=timeout, max_mirrors=max_mirrors)

    out, seen = [], set()
    for e in d.get("elements", []):
        t = e.get("tags", {})
        name = t.get("name")
        if not name or name.upper() in seen:
            continue
        seen.add(name.upper())
        c = e.get("center") or {"lat": e.get("lat"), "lon": e.get("lon")}
        dist = None
        if c.get("lat") is not None:
            dist = round(_haversine(lat, lon, c["lat"], c["lon"]))
        out.append({
            "name": name,
            "type": t.get("shop") or t.get("amenity") or t.get("leisure") or t.get("office") or "",
            "brand": t.get("brand"),
            "cuisine": t.get("cuisine"),
            "distance_m": dist,
        })
    out.sort(key=lambda x: (x["distance_m"] is None, x["distance_m"] or 0))
    return out[:limit]


if __name__ == "__main__":
    import sys
    lat = float(sys.argv[1]) if len(sys.argv) > 1 else 42.094
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else -72.501
    for b in find_businesses(lat, lon):
        print(f"  {b['distance_m']:>4}m  {b['name']}  ({b['type']}{'/'+b['cuisine'] if b['cuisine'] else ''})")
