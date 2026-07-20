"""
DealSynq — Property Intelligence (single-property demo web app).

Type an address -> get the property back.

Two layers, honestly scoped:
  * LIVE for any Springfield address: resolves address -> parcel -> owning entity ->
    the full set of parcels that entity owns (the assemblage), from the town assessor
    data already on disk (outputs/springfield_ownportal.csv).
  * DEEP profile: for properties we've fully enriched end-to-end (currently Five Town
    Plaza), the resolved owner is matched to fivetownplaza/PROFILE.json and the rich
    profile (ownership chain, transactions, tenants, permits, confidence tiers) is shown.

Run:   python fivetownplaza/webapp/server.py
Open:  http://localhost:8770/
Stdlib only — no external dependencies.
"""
import csv
import datetime
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

csv.field_size_limit(10 ** 7)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from springfield.record_card import fetch_parcel  # noqa: E402  live per-parcel enrichment
from springfield.businesses import find_businesses  # noqa: E402  OSM business/tenant lookup
from springfield.yelp import find_businesses_yelp  # noqa: E402  Yelp business lookup (dormant, paid)
from springfield.foursquare import find_businesses_foursquare  # noqa: E402  Foursquare (primary, free)
from springfield.footprint import site_metrics  # noqa: E402  OSM footprint + aerial estimates
from research.keyword_crawler import crawl as research_crawl, generate_queries, load_proxies  # noqa: E402
from springfield.zoning import lookup as zoning_lookup  # noqa: E402  ordinance detail per zoning code
from deeds.hampden_browser import (fetch_records as deeds_fetch,  # noqa: E402
                                   summarize as deeds_summarize)  # registry of deeds (browser)
from springfield.geocode import geocode  # noqa: E402  last-resort text->coords for landmarks
from springfield.sec_edgar import match_public_reit  # noqa: E402  live public-REIT detection
CSV_PATH = os.path.join(ROOT, "outputs", "springfield_ownportal.csv")
PROFILE_PATH = os.path.join(ROOT, "fivetownplaza", "PROFILE.json")
RESEARCH_PATH = os.path.join(ROOT, "fivetownplaza", "RESEARCH.json")
PRECACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "precache")
PORT = int(os.environ.get("PORT", "8770"))
# Bind localhost for local dev; a cloud host must reach the process from outside, so set
# HOST=0.0.0.0 there (render.yaml does this). Never hard-code 0.0.0.0 as the local default.
HOST = os.environ.get("HOST", "127.0.0.1")

# ---- Load data once at startup ------------------------------------------
KEEP = ["assessor_Parcel_Number", "assessor_Parcel_Address", "assessor_Owner_Name",
        "assessor_Assessed_Value", "assessor_Land_Area_In_Square_Feet",
        "ZONE_NAME", "NEIHOOD", "FLOODZONE"]

PARCELS = []
BY_OWNER = {}
BY_APN = {}
SPRINGFIELD_BBOX = None   # set by load(): (min_lat,min_lon,max_lat,max_lon) across all parcels
CITY_ZIPS = set()         # every ZIP seen in the assessor data (the citywide postal set)
CITY_ZIP_PREFIXES = set() # their 3-digit prefixes — unique institutional ZIPs share these

_COORD = re.compile(r"-?\d{1,3}\.\d+")


def _coords(geojson):
    """Parse a parcel's geometry into (lats, lons) vertex lists, or (None, None)."""
    nums = _COORD.findall(geojson or "")
    if len(nums) < 2:
        return None, None
    lons = [float(nums[i]) for i in range(0, len(nums) - 1, 2)]
    lats = [float(nums[i]) for i in range(1, len(nums), 2)]
    if not lons or not lats:
        return None, None
    return lats, lons


def _centroid(geojson):
    """Cheap approximate centroid: average of all lon/lat vertex pairs. Returns (lat, lon)."""
    lats, lons = _coords(geojson)
    if not lats:
        return None
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def _bbox(geojson):
    """Axis-aligned bounding box (min_lat, min_lon, max_lat, max_lon) of a parcel, or None.
    Four floats — the cheap footprint we use for adjacency clustering (see assemblage_cluster)."""
    lats, lons = _coords(geojson)
    if not lats:
        return None
    return (min(lats), min(lons), max(lats), max(lons))


def _num(s):
    try:
        return float(str(s).replace(",", "").strip() or 0)
    except Exception:
        return 0.0


# Canonicalize the trailing street-TYPE word so "Street"/"St."/"ST" all match. Applied
# ONLY to the last token (the actual suffix) so a street NAMED "Court"/"Park"/"Way" isn't
# mangled (e.g. "36 Court St" must NOT become "36 CT ST").
SUFFIX = {
    "STREET": "ST", "STR": "ST", "AVENUE": "AVE", "AV": "AVE", "ROAD": "RD",
    "DRIVE": "DR", "DRV": "DR", "LANE": "LN", "COURT": "CT", "CRT": "CT", "PLACE": "PL",
    "BOULEVARD": "BLVD", "CIRCLE": "CIR", "TERRACE": "TER", "TERR": "TER",
    "HIGHWAY": "HWY", "PARKWAY": "PKWY", "SQUARE": "SQ", "TRAIL": "TRL",
    "WAY": "WAY", "WY": "WAY",
}
_DIR = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
_STRIP = re.compile(r"\b(SPRINGFIELD|MASSACHUSETTS|MASS|MA|USA)\b")
# unit / suite / apt / floor designators — strip these and whatever follows them
_UNIT = re.compile(r"\b(UNIT|STE|SUITE|APT|APARTMENT|FL|FLR|FLOOR|BLDG|BUILDING|RM|ROOM|LOT|NO)\b\.?\s*[A-Z0-9-]*")
_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def normalize_addr(s):
    """Normalize an address to one canonical string: uppercase, drop punctuation, strip
    city/state/zip and unit/suite, canonicalize the trailing street suffix + leading dir."""
    s = (s or "").upper()
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(r"#\s*[A-Z0-9-]+", " ", s)   # "#820"
    s = _UNIT.sub(" ", s)                    # "UNIT 820", "STE 100", ...
    s = _STRIP.sub(" ", s)
    toks = [t for t in s.split() if not re.fullmatch(r"\d{5}(-\d{4})?", t)]  # drop zip
    if toks:
        toks[-1] = SUFFIX.get(toks[-1], toks[-1])          # suffix: last token only
        toks[0] = _DIR.get(toks[0], toks[0])               # leading direction word
    return " ".join(toks).strip()


def zip_of(s):
    """The 5-digit ZIP in a raw query, if any."""
    m = _ZIP.search(s or "")
    return m.group(1) if m else None


def street_of(norm):
    """Return the street part of a normalized address (drop the leading house number)."""
    toks = norm.split()
    return " ".join(toks[1:]) if toks and re.match(r"^\d", toks[0]) else norm


def load():
    print("Loading Springfield assessor data ...")
    with open(CSV_PATH, encoding="utf-8", errors="ignore") as f:
        for row in csv.DictReader(f):
            addr = (row["assessor_Parcel_Address"] or "").strip()
            gj = row.get("geometry_geojson", "")
            lats, lons = _coords(gj)
            cen = (sum(lats) / len(lats), sum(lons) / len(lons)) if lats else None
            bb = (min(lats), min(lons), max(lats), max(lons)) if lats else None
            # keep the actual vertex ring for point-in-polygon tests (businesses_at), not
            # just its bounding box — a bbox is a poor proxy for an irregular lot shape (a
            # rectangle drawn around an L-shaped or angled parcel includes area that isn't
            # actually the property). Only for a simple single-ring Polygon: _coords()
            # flattens ALL coordinate numbers in the GeoJSON regardless of ring structure,
            # so a MultiPolygon (several disjoint parts) or a Polygon with holes would
            # concatenate multiple rings into one bogus shape — skip those, bbox is the
            # fallback (still used for the coarse OSM-query radius either way).
            ring = (lats, lons) if (lats and "MultiPolygon" not in gj) else None
            rec = {
                "apn": row["assessor_Parcel_Number"],
                "address": addr,
                "norm": normalize_addr(addr),
                "owner": (row["assessor_Owner_Name"] or "").strip(),
                "assessed": _num(row["assessor_Assessed_Value"]),
                "land_sqft": _num(row["assessor_Land_Area_In_Square_Feet"]),
                "zone": (row["ZONE_NAME"] or "").strip(),
                "neighborhood": (row["NEIHOOD"] or "").strip(),
                "flood": (row["FLOODZONE"] or "").strip(),
                "zip": (row.get("ZIP_CODE") or "").strip()[:5],
                "owner_mail": " ".join(x for x in [(row.get("assessor_Owner_Address1") or "").strip(),
                                                   (row.get("assessor_Owner_Address2") or "").strip()] if x),
                "bbox": bb,
                "ring": ring,
                "lat": cen[0] if cen else None,
                "lon": cen[1] if cen else None,
            }
            PARCELS.append(rec)
            BY_OWNER.setdefault(rec["owner"].upper(), []).append(rec)
            BY_APN[rec["apn"]] = rec
            if rec["zip"] and len(rec["zip"]) == 5:
                CITY_ZIPS.add(rec["zip"])
                CITY_ZIP_PREFIXES.add(rec["zip"][:3])
    global SPRINGFIELD_BBOX
    boxes = [p["bbox"] for p in PARCELS if p.get("bbox")]
    if boxes:
        # small buffer so a landmark right at the data's edge isn't rejected
        pad_lat, pad_lon = 0.01, 0.01
        SPRINGFIELD_BBOX = (min(b[0] for b in boxes) - pad_lat, min(b[1] for b in boxes) - pad_lon,
                           max(b[2] for b in boxes) + pad_lat, max(b[3] for b in boxes) + pad_lon)
    print(f"  {len(PARCELS):,} parcels, {len(BY_OWNER):,} distinct owners loaded.")


PROFILE = {}
if os.path.exists(PROFILE_PATH):
    PROFILE = json.load(open(PROFILE_PATH, encoding="utf-8"))
DEEP_OWNER = (PROFILE.get("ownership", {}).get("current_owner", {}).get("name", "") or "").upper()


# ---- Parcel assemblage (contiguity, not just same owner) ----------------
def _bbox_adjacent(a, b, gap_lat, gap_lon):
    """True if two parcel bounding boxes (min_lat,min_lon,max_lat,max_lon), each expanded
    by the gap, overlap — i.e. the parcels touch or nearly touch."""
    return ((a[0] - gap_lat) <= b[2] and (a[2] + gap_lat) >= b[0] and
            (a[1] - gap_lon) <= b[3] and (a[3] + gap_lon) >= b[1])


def assemblage_cluster(anchor, owner_parcels, gap_m=15.0):
    """The actual PROPERTY = the anchor plus every same-owner parcel that is CONTIGUOUS
    with it (a connected cluster of touching bounding boxes), not everything the owner
    happens to hold.

    Same owner != same property. A landlord's scattered lots, or the City's ~800 parcels,
    must not merge into one assemblage just because they share an owner name. Validated on
    real data: this keeps Five Town Plaza's 9 contiguous parcels whole while reducing
    scattered owners (DNEPRO, City of Springfield, ...) to just the searched parcel's true
    neighbours.

    A parcel with no usable geometry can't be adjacency-tested, so a lone anchor comes back
    as a 1-parcel property — the correct, conservative answer."""
    ps = [p for p in owner_parcels if p.get("bbox")]
    if not anchor.get("bbox"):
        return [anchor]
    lat0 = anchor["bbox"][0]
    gap_lat = gap_m / 111320.0
    gap_lon = gap_m / (111320.0 * max(0.15, math.cos(math.radians(lat0))))
    by_apn = {p["apn"]: p for p in ps}
    by_apn.setdefault(anchor["apn"], anchor)   # ensure the anchor is in the pool
    pool = list(by_apn.values())
    seen = {anchor["apn"]}
    stack = [anchor]
    while stack:
        cur = stack.pop()
        for p in pool:
            if p["apn"] not in seen and _bbox_adjacent(cur["bbox"], p["bbox"], gap_lat, gap_lon):
                seen.add(p["apn"])
                stack.append(p)
    return [by_apn[a] for a in seen]


# ---- Search --------------------------------------------------------------
def search(q):
    raw = (q or "").strip()
    if not raw:
        return {"matched": False, "error": "empty query"}
    nq = normalize_addr(raw)
    if not nq:
        return {"matched": False, "error": "empty query"}
    toks = nq.split()
    has_number = bool(re.match(r"^\d", toks[0])) if toks else False
    mode = "address"
    hits = []

    def _sopt(p):
        return {"address": p["address"], "owner": p["owner"]}

    if has_number:
        # ADDRESS query. A bare number ("1") is too ambiguous — require a street too.
        if len(toks) < 2:
            return {"matched": False, "query": raw, "ambiguous": "number",
                    "error": "Include a street name too — e.g. “380 Cooley St”."}
        qnum = int(re.match(r"^(\d+)", toks[0]).group(1))
        qstreet = " ".join(toks[1:])
        exact = [p for p in PARCELS if p["norm"] == nq]
        if exact:
            hits = exact
        else:
            # match a house number that falls inside a parcel's range ("1387-1391 MAIN ST")
            for p in PARCELS:
                pt = p["norm"].split()
                if not pt or street_of(p["norm"]) != qstreet:
                    continue
                m = re.match(r"^(\d+)(?:-(\d+))?$", pt[0])
                if not m:
                    continue
                lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
                # US street numbering runs odd/even on opposite sides — a range like
                # "1055-1063" is the ODD side only. Numerically-inside-the-range but
                # wrong-parity ("1060" inside "1055-1063") is NOT that parcel; it's an
                # unlisted address on the other side of the street. Without this check,
                # "1060 Main St" (Red Rose Pizzeria) silently matched "1055-1063 Main St"
                # (Caring Health Center Inc) purely because 1060 falls between 1055 and
                # 1063 numerically. Single-number ranges (lo==hi) have nothing to check.
                if lo <= qnum <= hi and (lo == hi or qnum % 2 == lo % 2):
                    hits.append(p)
            # The typed number can legitimately not match ANY assessor range — a landmark's
            # public/mailing address (Google Maps, signage) is often not how the assessor
            # numbered the parcel: MGM Springfield's real address is "1 MGM Way" but its
            # parcel is recorded as "12-24 MGM Way"; a private access road can be numbered
            # entirely differently from its public entrance. When the named street has
            # EXACTLY ONE parcel, there is no ambiguity about which property was meant —
            # auto-resolve to it rather than making the user click through a suggestion.
            # (A street with many parcels, e.g. Cooley St, still requires disambiguation —
            # this only fires when there is nothing else it could be.)
            if not hits:
                same_street = [p for p in PARCELS if street_of(p["norm"]) == qstreet]
                if len(same_street) == 1:
                    hits = same_street
                    mode = "street-unique"
    else:
        # NAME query: a whole street, or an owner. NEVER a loose substring (that's what let
        # "LLC" match "HILLCREST" and "Main St" match every Main St parcel).
        street_hits = [p for p in PARCELS if street_of(p["norm"]) == nq]
        if street_hits:
            opts = sorted(street_hits, key=lambda p: p["assessed"], reverse=True)[:12]
            return {"matched": False, "query": raw, "ambiguous": "street", "street": nq,
                    "suggestions": [_sopt(p) for p in opts]}
        uq = re.sub(r"\s+", " ", raw.strip().upper())
        if len(uq) < 3:
            return {"matched": False, "query": raw, "ambiguous": "short",
                    "error": "Too short — enter a full address or owner name."}
        owner_hits = [p for p in PARCELS if re.search(r"\b" + re.escape(uq) + r"\b", p["owner"].upper())]
        owners = {p["owner"].upper() for p in owner_hits}
        if len(owners) > 25 or len(owner_hits) > 60:
            return {"matched": False, "query": raw, "ambiguous": "too_broad",
                    "error": "That matches too many owners — be more specific."}
        if len(owners) > 1:
            opts = sorted(owner_hits, key=lambda p: p["assessed"], reverse=True)[:12]
            return {"matched": False, "query": raw, "ambiguous": "owner",
                    "suggestions": [_sopt(p) for p in opts]}
        if owner_hits:
            hits, mode = owner_hits, "owner"

    geocode_info = None
    if not hits:
        # LAST RESORT: text->coordinates via OSM geocoding, then point-in-parcel. This is
        # what resolves a landmark NAME ("Hall of Fame") that never touches our address/
        # street/owner index at all, or a public address whose numbering the assessor
        # doesn't share (already handled for single-parcel streets above; this covers
        # everything else). Always weaker evidence than a direct assessor match — never
        # returned silently as if it were one (see mode="geocoded" handling below and in
        # the frontend). Bounded to our own data's extent so an ambiguous name can't
        # resolve to a same-named place in a different city.
        try:
            hit = geocode(raw, bbox=SPRINGFIELD_BBOX)
        except Exception:
            hit = None
        if hit and SPRINGFIELD_BBOX and (
                SPRINGFIELD_BBOX[0] <= hit["lat"] <= SPRINGFIELD_BBOX[2]
                and SPRINGFIELD_BBOX[1] <= hit["lon"] <= SPRINGFIELD_BBOX[3]):
            gp, how = find_parcel_at_point(hit["lat"], hit["lon"])
            if gp:
                hits, mode = [gp], "geocoded"
                geocode_info = {"query": raw, "display_name": hit["display_name"], "how": how}

    if not hits:
        # No match. Offer other parcels on the SAME street (whole-street match, not substring).
        st = street_of(nq)
        suggestions = []
        if st and len(st) >= 3:
            same = sorted((p for p in PARCELS if street_of(p["norm"]) == st),
                          key=lambda p: p["assessed"], reverse=True)[:8]
            suggestions = [_sopt(p) for p in same]
        return {"matched": False, "query": raw, "street": st, "suggestions": suggestions}

    # pick the highest-assessed matching parcel as the anchor, resolve its owner
    anchor = max(hits, key=lambda p: p["assessed"])
    owner = anchor["owner"]
    # ZIP sanity check. The assessor parcel ZIP is NOT postal ground truth — large
    # institutions legitimately publish their own unique ZIPs (Baystate = 01199 while its
    # parcel is recorded 01107), and USPS boundaries don't track parcel records. So a
    # mismatch against the PARCEL's zip must never be called "wrong". Instead, validate
    # against the CITYWIDE zip set (all zips in the dataset + the city's 3-digit prefix,
    # which unique/institutional zips share) and only warn when the entered zip isn't
    # plausible for Springfield at all (99999, a Boston zip, ...). This also closes the
    # earlier bypass where a parcel with a blank zip (e.g. 380 Cooley St) skipped the
    # check entirely.
    qzip = zip_of(raw)
    zip_warning = None
    if qzip and CITY_ZIPS and qzip not in CITY_ZIPS and qzip[:3] not in CITY_ZIP_PREFIXES:
        zip_warning = (f"ZIP {qzip} doesn't look like a Springfield, MA ZIP code. "
                       "Showing the address match — double-check it's the right property.")
    # the assemblage is the CONTIGUOUS cluster around the anchor, not every parcel the
    # owner holds (same owner != same property — see assemblage_cluster).
    owner_parcels = BY_OWNER.get(owner.upper(), [anchor])
    assemblage = sorted(assemblage_cluster(anchor, owner_parcels),
                        key=lambda p: p["assessed"], reverse=True)
    owner_other_parcels = max(0, len(owner_parcels) - len(assemblage))

    total_val = sum(p["assessed"] for p in assemblage)
    total_land = sum(p["land_sqft"] for p in assemblage)
    deep = PROFILE if owner.upper() == DEEP_OWNER else None

    # LIVE per-parcel enrichment via the assessor record card (skip for the pre-built
    # deep-profile property, which already has this and much more). This is the ONLY
    # slow live call left in the critical path, and it's our own reliable scraper, not
    # a third-party server — the OSM "extras" (businesses, footprint) are intentionally
    # NOT fetched here. They are slow, best-effort, third-party (Overpass) calls, so they
    # are fetched by the frontend afterward via /api/extra, in the background, and never
    # block the main result from showing.
    enrichment = None
    if not deep:
        # Hard wall-clock cap: a City-owned megaparcel (e.g. 299 Sumner Ave / Forest Park)
        # can have many record cards, and the assessor site occasionally stalls — without a
        # ceiling the whole search hangs "forever". Cap it; on timeout we return the parcel
        # with enrichment=None (the frontend shows the record card as unavailable + retry)
        # rather than blocking the result.
        # NB: deliberately NOT `with ThreadPoolExecutor(...)` — the context manager's exit
        # calls shutdown(wait=True), which BLOCKS on the still-running enrich thread even
        # after result(timeout=14) has already raised. That silently turned the 14s cap
        # into "however long the stalled scrape takes" (~36s observed on 299 Sumner Ave).
        # shutdown(wait=False) returns immediately; the orphaned thread finishes (and
        # warms ENRICH_CACHE) in the background without holding up this response.
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            enrichment = ex.submit(enrich, anchor["apn"]).result(timeout=14)
        except Exception:
            enrichment = None
        finally:
            ex.shutdown(wait=False)

    bsqft = 336205 if deep else (enrichment or {}).get("total_building_sqft") or 0
    stories = None
    if enrichment:
        b0 = (enrichment.get("buildings") or [{}])[0]
        stories = (b0.get("detail") or {}).get("stories")

    return {
        "matched": True,
        "mode": mode,
        "anchor_address": anchor["address"],
        "owner": owner,
        "owner_mailing": anchor.get("owner_mail") or "",
        "neighborhood": anchor["neighborhood"],
        "flood_zone": anchor["flood"],
        "assemblage": assemblage,
        "totals": {
            "parcels": len(assemblage),
            "assessed": total_val,
            "land_sqft": total_land,
            "land_acres": round(total_land / 43560, 2),
        },
        "deep": deep,
        "enrichment": enrichment,
        "zoning": resolve_zoning(anchor, assemblage, enrichment, deep),
        "disposition": disposition_signals(anchor, owner, len(owner_parcels), enrichment, deep),
        "match_count": len(hits),
        "zip_warning": zip_warning,
        "geocode": geocode_info,
        # how many OTHER parcels this owner holds that are NOT part of this property (they
        # aren't contiguous) — shown as context, never merged into the assemblage.
        "owner_other_parcels": owner_other_parcels,
        "owner_total_parcels": len(owner_parcels),
        # coordinates + building sqft/stories so the frontend can call /api/extra itself
        "extra_params": {
            "apn": anchor["apn"], "lat": anchor["lat"], "lon": anchor["lon"],
            "land_sqft": anchor["land_sqft"], "building_sqft": bsqft, "stories": stories,
        } if anchor.get("lat") is not None else None,
    }


def resolve_zoning(anchor, assemblage, enrichment, deep):
    """Map a property's zoning to the consolidated ordinance detail. Tries, in order:
    the assessor record-card code (e.g. B3), the anchor parcel's district name, then any
    non-"Split" district among the assemblage. Handles Split parcels by resolving to the
    dominant business/commercial district and flagging it."""
    candidates = []
    if enrichment and enrichment.get("zoning"):
        candidates.append(enrichment["zoning"])
    az = (anchor.get("zone") or "").strip()
    if az and az.lower() != "split":
        candidates.append(az)
    # assemblage district names (unique, non-Split) — commercial ones sort first
    zones = []
    for p in assemblage:
        z = (p.get("zone") or "").strip()
        if z and z.lower() != "split" and z not in zones:
            zones.append(z)
    zones.sort(key=lambda z: (z.lower().startswith("residence"), z))
    candidates += zones
    if deep:
        coarse = (deep.get("zoning", {}) or {}).get("coarse_code_from_gis_export", "")
        for part in re.split(r"[/,]", coarse):
            if part.strip():
                candidates.append(part.strip())
    is_split = az.lower() == "split" or any((p.get("zone") or "").lower() == "split" for p in assemblage)
    for c in candidates:
        z = zoning_lookup(c)
        if z:
            z["raw_zone"] = c
            if is_split:
                dn = z.get("district_name", "")
                kind = "district" if dn.lower().startswith("residence") else "commercial district"
                z["split_note"] = ('This assemblage spans multiple zoning districts ("Split"); '
                                   f"showing {dn}, its primary {kind}.")
            return z
    return None


# ---- Disposition signals ("likelihood of selling") ----------------------
# A transparent, source-tagged read of the factors that genuinely correlate with a CRE
# owner disposing an asset — NOT a probability (a real probability needs a model trained
# on sale outcomes we don't have). We weight LEADING indicators (hold period, debt maturity,
# owner posture) over LAGGING ones (vacancy/price cuts, which research shows appear AFTER
# the decision). Every factor carries its source + confidence; inferred ones say so.
#
# Owner posture works for EVERY owner, not just public REITs: SEC covers the handful that
# are public, but for private owners we read posture from entity type + how many properties
# they hold in our own parcel index (portfolio footprint). Per the 2026-07 manager note.
def _owner_posture(owner, parcel_count, deep):
    name = (owner or "").upper()
    if deep:
        return ("Public REIT", "owner is a public REIT (SEC filer) that actively manages and "
                "prunes its portfolio", "raises", "verified", "SEC EDGAR")
    # Government / public authority — check FIRST so a "...AUTHORITY INC" isn't mislabeled an
    # investor. These almost never make an opportunistic market disposition.
    if re.search(r"\bCITY OF\b|\bTOWN OF\b|COMMONWEALTH|UNITED STATES|\bU S A\b|\bCOUNTY\b|"
                 r"AUTHORITY|\bAUTH\b|REDEVELOPMENT AUTH|HOUSING AUTH|PARKING AUTH|CONVENTION CENTER|"
                 r"COMMISSION|DEPARTMENT|\bD P W\b|\bM B T A\b|\bDCR\b", name):
        return ("Government / public", "government or public-authority owner — rarely a market "
                "disposition", "lowers", "strong", "entity name")
    # Institutional, mission-driven owners (hospitals, universities, churches, insurers,
    # nonprofits). Also checked BEFORE the LLC/Inc test — "BAYSTATE MEDICAL CENTER INC" is a
    # hospital, not an "active investor"; "MASS MUTUAL ... INSURANCE CO" is an insurer, not an
    # individual. These hold for mission/long-term reasons, so they lower disposition odds.
    # NB: \bHOSPITALS?\b — the closing word boundary is load-bearing. A bare "HOSPITAL"
    # substring-matches "HOSPITALITY", turning hotel operators ("MITTAS HOSPITALITY LLC")
    # into institutions. Same care with the other stems: MINISTR/EDUCAT are DELIBERATE
    # prefixes (Ministry/Ministries, Education/Educational), the rest are word-bounded.
    if re.search(r"\bHOSPITALS?\b|MEDICAL CENTER|\bMEDICAL\b|HEALTH ?CARE|\bHEALTH\b|\bCLINIC\b|"
                 r"UNIVERSITY|\bCOLLEGE\b|\bACADEMY\b|\bSCHOOL\b|EDUCAT|"
                 r"\bCHURCH\b|\bTEMPLE\b|SYNAGOGUE|MOSQUE|\bPARISH\b|DIOCESE|ARCHDIOCESE|MINISTR|CONGREGATION|"
                 r"FOUNDATION|NON.?PROFIT|ASSOCIATION|\bSOCIETY\b|\bMUSEUM\b|\bLIBRARY\b|\bYMCA\b|\bYWCA\b|"
                 r"INSURANCE|MUTUAL LIFE|MASS ?MUTUAL", name):
        return ("Institutional owner", "institutional / mission-driven owner (health, education, "
                "religious, nonprofit or insurer) — holds long-term, rarely an opportunistic seller",
                "lowers", "strong", "entity name")
    # Live public-REIT check (springfield/sec_edgar.py) — only attempted for corporate-
    # shaped names (an individual's name could never be an SEC filer, so skip the network
    # call entirely for those). Most commercial owners here are shell subsidiary LLCs whose
    # name has no resemblance to their public parent (Five Town Plaza's own "Five Town
    # Station LLC" -> Phillips Edison is exactly that case, only findable by hand), so this
    # is expected to return no match for most owners — that's the honest, correct outcome,
    # not a failure. "strong" not "verified": reserve "verified" for a human-confirmed
    # parent-chain trace (like the deep profile's SEC Exhibit 21.1 citation); this is a
    # same-name automated match, one notch more cautious.
    if re.search(r"\bLLC\b|\bL L C\b|\bLP\b|\bLLP\b|LIMITED PARTNERSHIP|\bINC\b|CORP|COMPANY|"
                 r"\bLTD\b|\bPLC\b|\bTRUST\b", name):
        reit = match_public_reit(name)
        if reit:
            return ("Public REIT", f"owner name matches SEC-registered public REIT "
                    f"{reit['name']} (ticker {reit['ticker']}), classified as a "
                    f"{reit['sic_description']} — actively manages and prunes its portfolio",
                    "raises", "strong", "SEC EDGAR (live name + industry-code match)")
    if re.search(r"\bLLC\b|\bL L C\b|\bLP\b|\bLLP\b|LIMITED PARTNERSHIP|\bINC\b|CORP|COMPANY", name):
        if parcel_count >= 6:
            return ("Active investor", f"investment entity (LLC/LP/Corp) holding {parcel_count} "
                    "parcels locally — an active property investor", "raises", "strong",
                    "entity name + our parcel index")
        return ("Private investor", "investment entity (LLC/LP/Corp) holding "
                f"{parcel_count} parcel(s) — a private / single-asset investor", "neutral",
                "strong", "entity name + our parcel index")
    if re.search(r"\bTRUST\b|\bTR\b|TRUSTEE|\bEST\b|ESTATE", name):
        return ("Trust / estate", f"held in trust/estate ({parcel_count} parcel(s)) — can "
                "transition on a life event", "neutral", "moderate", "entity name")
    if parcel_count <= 1:
        return ("Owner-occupant / small holder", "individual owner of a single property — "
                "typically owner-occupied, sells on life events, not opportunistically",
                "lowers", "moderate", "owner name + our parcel index")
    return ("Individual landlord", f"individual holding {parcel_count} properties — a small "
            "private landlord", "neutral", "moderate", "owner name + our parcel index")


def _year_of(s):
    m = re.search(r"(19|20)\d{2}", str(s or ""))
    return int(m.group(0)) if m else None


def disposition_signals(anchor, owner, owner_parcel_count, enrichment, deep):
    """Leading-indicator disposition read from synchronously-available data (hold period,
    owner posture, property vintage). Debt maturity, active listing and liens are added
    client-side once Recorded Documents / Web Research load — see the frontend."""
    yr = datetime.date.today().year
    factors = []

    def add(key, label, finding, direction, weight, source, confidence):
        factors.append({"key": key, "label": label, "finding": finding, "direction": direction,
                        "weight": weight, "source": source, "confidence": confidence})

    # --- hold period (strongest leading signal we have synchronously) ---
    sale_date = None
    if deep:
        sale_date = ((deep.get("transaction_history") or [{}])[0]).get("date")
    elif enrichment and enrichment.get("sales"):
        sale_date = (enrichment["sales"][0] or {}).get("date")
    hy = (yr - _year_of(sale_date)) if _year_of(sale_date) else None
    if hy is not None:
        if hy < 3:
            add("hold", "Hold period", f"acquired ~{hy} yr ago — early in a typical hold", "lowers", -2, "Assessor / Registry sale date", "verified")
        elif hy <= 7:
            add("hold", "Hold period", f"held ~{hy} yrs — mid the typical 5–10 yr hold", "neutral", 0, "Assessor / Registry sale date", "verified")
        elif hy <= 10:
            add("hold", "Hold period", f"held ~{hy} yrs — nearing the end of a typical hold", "raises", 1, "Assessor / Registry sale date", "verified")
        else:
            add("hold", "Hold period", f"held ~{hy} yrs — well past the 5–10 yr norm (mature phase)", "raises", 2, "Assessor / Registry sale date", "verified")

    # --- owner posture (works for any owner) ---
    ptype, pdetail, pdir, pconf, psrc = _owner_posture(owner, owner_parcel_count, deep)
    add("owner", "Owner posture", ptype + " — " + pdetail, pdir,
        {"raises": 1, "neutral": 0, "lowers": -1}[pdir], psrc, pconf)

    # --- property vintage / capex pressure ---
    yb = None
    last_permit = None
    if not deep and enrichment:
        yb = ((enrichment.get("buildings") or [{}])[0]).get("year_built")
        if enrichment.get("permits"):
            last_permit = (enrichment["permits"][0] or {}).get("date")
    aby = _year_of(yb)
    if aby:
        age = yr - aby
        permit_recent = bool(_year_of(last_permit) and (yr - _year_of(last_permit) <= 5))
        if permit_recent:
            add("vintage", "Property vintage", f"built ~{aby}, permitted/renovated in the last 5 yrs — owner reinvesting", "lowers", -1, "Assessor record card", "verified")
        elif age >= 40:
            add("vintage", "Property vintage", f"built ~{aby} (~{age} yrs), no major permit in 5 yrs — reinvest-or-sell pressure", "raises", 1, "Assessor record card", "verified")
        else:
            add("vintage", "Property vintage", f"built ~{aby} (~{age} yrs)", "neutral", 0, "Assessor record card", "verified")

    # Government owner: hold-period and vintage are PRIVATE-market signals — "held 40 yrs,
    # mature phase" and "old building, reinvest-or-sell pressure" describe how commercial
    # investors behave, not how a city or public authority holds civic assets (they hold
    # indefinitely and dispose through political/administrative processes, not market
    # timing). Counting those signals pushed a convention center to "Moderate". Neutralize
    # them (weight 0, with the reason stated in the finding) rather than counter-weighting
    # the posture factor — the signals don't apply, so they shouldn't vote at all.
    if ptype.startswith("Government"):
        for f in factors:
            if f["key"] in ("hold", "vintage") and f["weight"] > 0:
                f["weight"] = 0
                f["direction"] = "neutral"
                f["finding"] += " — not treated as a sale signal for a government/public owner"

    return {
        "factors": factors,
        "base_score": sum(f["weight"] for f in factors),
        # these refine the read once the background sections load (computed client-side)
        "pending": ["Debt maturity — from Recorded Documents",
                    "Active for-sale listing — from Deep Web Research",
                    "Liens / distress — from Recorded Documents"],
    }


SITE_CACHE = {}
BIZ_CACHE = {}


def site_at(apn, lat, lon, land_sqft, building_sqft, stories, deadline=None):
    """OSM footprint + aerial-derived metrics. Cached; failures are NOT cached (so a
    slow/overloaded Overpass mirror gets retried next time, not stuck permanently)."""
    if apn in SITE_CACHE:
        return SITE_CACHE[apn]
    try:
        res = site_metrics(lat, lon, land_sqft=land_sqft, assessor_building_sqft=building_sqft,
                           stories=stories, deadline=deadline)
    except Exception as e:
        print(f"  [site {apn}] failed/slow: {e}")
        return None
    SITE_CACHE[apn] = res
    return res


def _bbox_union(parcels):
    """Union bounding box (min_lat,min_lon,max_lat,max_lon) across a set of parcels, or
    None if none of them have usable geometry."""
    bxs = [p["bbox"] for p in parcels if p.get("bbox")]
    if not bxs:
        return None
    return (min(b[0] for b in bxs), min(b[1] for b in bxs),
            max(b[2] for b in bxs), max(b[3] for b in bxs))


def _point_in_ring(lat, lon, lats, lons):
    """Ray-casting point-in-polygon test against one vertex ring."""
    n = len(lats)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi, yj, xj = lats[i], lons[i], lats[j], lons[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi:
            inside = not inside
        j = i
    return inside


def _on_parcel(lat, lon, parcels, bbox, buffer_m=15.0):
    """Is (lat, lon) actually on one of these parcels? Uses the real polygon shape when a
    parcel has a usable ring (accurate for irregular lots — a bbox rectangle around an
    L-shaped or angled parcel wrongly includes area that isn't the property); falls back to
    a buffered bounding-box test only for parcels with no ring. Returns None if we have
    neither a ring nor a bbox to test against (unknown, not a claim either way)."""
    if lat is None or lon is None:
        return None
    have_geometry = False
    for p in parcels:
        ring = p.get("ring")
        if ring and ring[0]:
            have_geometry = True
            if _point_in_ring(lat, lon, ring[0], ring[1]):
                return True
    if have_geometry:
        return False   # had real shapes to test against and matched none of them
    if not bbox:
        return None
    lat0 = (bbox[0] + bbox[2]) / 2
    gap_lat = buffer_m / 111320.0
    gap_lon = buffer_m / (111320.0 * max(0.15, math.cos(math.radians(lat0))))
    return (bbox[0] - gap_lat) <= lat <= (bbox[2] + gap_lat) and (bbox[1] - gap_lon) <= lon <= (bbox[3] + gap_lon)


def _deg_dist_m(lat1, lon1, lat2, lon2):
    """Cheap planar-approximation distance in meters (fine at this scale, no need for
    full haversine)."""
    lat0 = (lat1 + lat2) / 2
    dlat = (lat2 - lat1) * 111320.0
    dlon = (lon2 - lon1) * 111320.0 * max(0.15, math.cos(math.radians(lat0)))
    return math.hypot(dlat, dlon)


# A geocoded point can miss every parcel's polygon by a few meters (sidewalk, parking edge,
# OSM/assessor coordinate drift) — this is how far we'll still accept a "nearest" match, but
# it's flagged as lower-confidence than a genuine polygon-contains match (see find_parcel_
# at_point's `how` return value and search()'s "geocoded" mode handling).
_NEAREST_FALLBACK_M = 40.0


def find_parcel_at_point(lat, lon):
    """Which parcel, if any, contains (lat, lon)? Used ONLY by the geocoding fallback (see
    search()) — a last resort when address/street/owner text matching found nothing.

    Returns (parcel, how) where how is "exact" (point genuinely inside the parcel's real
    polygon — high confidence) or "nearest" (no polygon contained it, but this centroid is
    within _NEAREST_FALLBACK_M — lower confidence, caller must say so), or (None, "none")."""
    if lat is None or lon is None:
        return None, "none"
    # cheap bbox prefilter across all 42k parcels before the precise ring test
    candidates = [p for p in PARCELS if p.get("bbox")
                 and p["bbox"][0] <= lat <= p["bbox"][2] and p["bbox"][1] <= lon <= p["bbox"][3]]
    for p in candidates:
        ring = p.get("ring")
        if ring and ring[0] and _point_in_ring(lat, lon, ring[0], ring[1]):
            return p, "exact"
    nearest, nearest_d = None, None
    for p in PARCELS:
        if p.get("lat") is None:
            continue
        d = _deg_dist_m(lat, lon, p["lat"], p["lon"])
        if nearest_d is None or d < nearest_d:
            nearest, nearest_d = p, d
    if nearest and nearest_d <= _NEAREST_FALLBACK_M:
        return nearest, "nearest"
    return None, "none"


def businesses_at(apn, lat, lon, land_sqft, deadline=None):
    """Named businesses operating at/near this parcel. Cached; failures are not.

    Source priority: Foursquare Places (genuinely free up to 500 calls/month, no
    subscription; springfield/foursquare.py) -> Yelp Fusion (dormant unless a paid
    YELP_API_KEY is ever configured — Yelp's free self-serve tier was discontinued;
    springfield/yelp.py) -> OpenStreetMap Overpass (free, no key, but volunteer-mapped and
    often sparse for small multi-tenant buildings; springfield/businesses.py). Each fallback
    only fires when the one before it is unconfigured or its call itself fails; a call that
    SUCCEEDS with zero results is trusted as real information (that source found genuinely
    nothing nearby), not treated as a failure to fall back from. The returned dict's "source"
    field tells the caller which one actually answered, so the UI can label it honestly
    instead of assuming.

    Classification of "on this parcel" vs. "merely nearby" uses the assemblage's REAL polygon
    shape when available (point-in-polygon, not just a fixed-radius circle from the centroid)
    — a blind radius sweeps in unrelated standalone businesses across the street for a large
    commercial parcel (verified: at a 7.88-acre parcel, several hits within the old 170m
    radius were genuinely outside the parcel boundary — correctly excluded once the real
    shape is used instead of a bounding-box guess). Falls back to a buffered bbox when no
    usable polygon is available.

    `deadline` (an absolute time.time() value, shared with the sibling site_at() call) bounds
    the OSM path's narrow-then-widen retries to ONE wall-clock budget across both calls —
    without it, they could each independently retry every mirror and blow far past whatever
    the caller intended to wait (this previously caused this lookup to reliably time out on a
    slower/cloud host while the sibling footprint call, a single round-trip, finished fine)."""
    if apn in BIZ_CACHE:
        return BIZ_CACHE[apn]
    bbox, assemblage = None, []
    anchor = BY_APN.get(apn)
    if anchor:
        owner_parcels = BY_OWNER.get(anchor["owner"].upper(), [anchor])
        assemblage = assemblage_cluster(anchor, owner_parcels)
        bbox = _bbox_union(assemblage)
    # scale search radius to parcel size (small pad/house = tight; big plaza = wide)
    radius = 60 if land_sqft < 20000 else 100 if land_sqft < 80000 else 170
    if bbox:
        # make sure the query circle fully covers the real boundary + a margin, even
        # if the given lat/lon sits off-center relative to the assemblage's true extent
        corners = ((bbox[0], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[1]), (bbox[2], bbox[3]))
        corner_d = max(_deg_dist_m(lat, lon, c[0], c[1]) for c in corners)
        radius = max(radius, min(400, round(corner_d) + 40))

    res, source = None, None
    try:
        fsq_res = find_businesses_foursquare(lat, lon, radius=radius)
        if fsq_res is not None:      # None = unconfigured/failed; [] = genuinely nothing nearby
            res, source = fsq_res, "foursquare"
    except Exception as e:
        print(f"  [businesses-foursquare {apn}] failed: {e}")

    if res is None:
        try:
            yelp_res = find_businesses_yelp(lat, lon, radius=radius)
            if yelp_res is not None:
                res, source = yelp_res, "yelp"
        except Exception as e:
            print(f"  [businesses-yelp {apn}] failed: {e}")

    if res is None:
        try:
            res = find_businesses(lat, lon, radius=radius, deadline=deadline)
            source = "osm"
            # only attempt the wider retry if there's meaningful time left on the SAME deadline
            if not res and (deadline is None or (deadline - time.time()) > 1.5):
                res = find_businesses(lat, lon, radius=300, timeout=7, deadline=deadline)  # widen once
                for r in res:      # flag these: they are NEAR the parcel, not on it
                    r["widened"] = True
        except Exception as e:
            print(f"  [businesses-osm {apn}] failed/slow: {e}")
            return None

    # classify against the REAL boundary when we have one — overrides any coarse
    # radius-implied flag above with an actual on/off-parcel determination. Applies to
    # either source, since both return the same {lat, lon, ...} shape.
    if assemblage:
        for r in res:
            inside = _on_parcel(r.get("lat"), r.get("lon"), assemblage, bbox)
            if inside is not None:
                r["widened"] = not inside
    res = {"source": source, "items": res}
    BIZ_CACHE[apn] = res
    return res


ENRICH_CACHE = {}


def enrich(apn):
    """Live record-card enrichment for one parcel, cached. Never fatal — returns None on error."""
    if apn in ENRICH_CACHE:
        return ENRICH_CACHE[apn]
    try:
        data = fetch_parcel(apn)
    except Exception as e:
        print(f"  [enrich {apn}] failed: {e}")
        data = None
    ENRICH_CACHE[apn] = data
    return data


# ---- Deep web research (address -> ranked list of every website) ---------
# Slow (dozens of paced search queries), so it NEVER runs in the request path: the
# frontend triggers it explicitly, we run it in a background thread, and the page
# polls for progress. A pre-built RESEARCH.json (the flagship Five Town Plaza run) is
# served instantly; any other address runs live but with a conservative query cap so a
# demo viewer can't get the host IP rate-limited.
LIVE_MAX_QUERIES = 24      # cap for on-demand live runs (full cached runs are larger)
LIVE_PER_QUERY = 10
LIVE_PACE = (1.5, 3.0)
RESEARCH_MAX_SECONDS = 90  # HARD overall ceiling — return partial rather than run 400s+

# Route live research through the shared Decodo proxy pool when available. DuckDuckGo
# rate-limits repeated searches per IP; rotating proxy IPs both improves reliability
# and — importantly for a public demo — keeps the HOST server's IP from getting flagged.
try:
    RESEARCH_PROXIES = load_proxies()
    print(f"  research: rotating {len(RESEARCH_PROXIES)} proxy IPs")
except SystemExit:
    RESEARCH_PROXIES = None
    print("  research: no proxy pool found — live runs go direct (may rate-limit)")

RESEARCH_PREBUILT = {}     # normalized address -> pre-built research doc
if os.path.exists(RESEARCH_PATH):
    try:
        _doc = json.load(open(RESEARCH_PATH, encoding="utf-8"))
        RESEARCH_PREBUILT[normalize_addr(_doc.get("address", ""))] = _doc
        print(f"  loaded pre-built research for {_doc.get('address')}")
    except Exception as e:
        print(f"  [research] could not load {RESEARCH_PATH}: {e}")


def load_precache():
    """Load the demo pre-cache (webapp/precache/): record-card enrichment keyed by APN and
    research keyed by address. This makes the featured demo addresses load instantly and
    never depend on a live scrape/crawl at presentation time. Absent dir = no-op."""
    if not os.path.isdir(PRECACHE_DIR):
        return
    loaded_e = loaded_r = loaded_d = 0
    for fn in sorted(os.listdir(PRECACHE_DIR)):
        path = os.path.join(PRECACHE_DIR, fn)
        try:
            doc = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print(f"  [precache] skip {fn}: {e}")
            continue
        if fn.startswith("enrich_") and doc.get("apn"):
            ENRICH_CACHE[doc["apn"]] = doc
            loaded_e += 1
        elif fn.startswith("research_") and doc.get("address"):
            RESEARCH_PREBUILT[normalize_addr(doc["address"])] = doc
            loaded_r += 1
        elif fn.startswith("deeds_") and doc.get("owner"):
            # re-derive summary from the raw records at load time (cheap, no network) so a
            # precached file always reflects the CURRENT summarize() logic, not whatever
            # version was live when precache_demo.py last ran.
            if doc.get("records") is not None:
                doc["summary"] = deeds_summarize(doc["records"])
            DEEDS_DONE[_norm_owner(doc["owner"])] = doc
            loaded_d += 1
    if loaded_e or loaded_r or loaded_d:
        print(f"  precache: {loaded_e} record-card, {loaded_r} research, {loaded_d} deeds doc(s) loaded")

RESEARCH_JOBS = {}         # job_id -> job dict
RESEARCH_DONE = {}         # normalized address -> finished doc (in-memory cache for live runs)
_RJOBS_LOCK = threading.Lock()


def _run_research_job(job_id, address, name):
    job = RESEARCH_JOBS[job_id]

    def progress(done, total, query, n_found):
        job["done"] = done
        job["total"] = total
        job["last_query"] = query
    try:
        doc = research_crawl(address, name=name, per_query=LIVE_PER_QUERY, pace=LIVE_PACE,
                             proxies_pool=RESEARCH_PROXIES, progress_cb=progress,
                             max_queries=LIVE_MAX_QUERIES, max_seconds=RESEARCH_MAX_SECONDS,
                             should_stop=lambda: job.get("cancelled"))
        job["result"] = doc
        job["status"] = "done"
        RESEARCH_DONE[normalize_addr(address)] = doc
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def start_research(address, name=None):
    """Return (payload, ...) describing an instant cached result, an already-finished
    live run, or a freshly-started background job the frontend should poll."""
    norm = normalize_addr(address)
    if norm in RESEARCH_PREBUILT:
        return {"status": "done", "cached": True, "result": RESEARCH_PREBUILT[norm]}
    if norm in RESEARCH_DONE:
        return {"status": "done", "cached": True, "result": RESEARCH_DONE[norm]}
    # is a job already running for this address? reuse it.
    with _RJOBS_LOCK:
        for jid, j in RESEARCH_JOBS.items():
            if j["norm"] == norm and j["status"] == "running":
                return {"status": "running", "job": jid, "total": j["total"]}
        # a live research run for a DIFFERENT address means the user moved on — cancel those
        # so an abandoned crawl stops consuming the host's (rate-limited) IP + threads
        # instead of running to completion for a page nobody is watching.
        for j in RESEARCH_JOBS.values():
            if j["status"] == "running" and j["norm"] != norm:
                j["cancelled"] = True
        job_id = uuid.uuid4().hex[:12]
        total = min(len(generate_queries(address, name)), LIVE_MAX_QUERIES)
        RESEARCH_JOBS[job_id] = {"status": "running", "norm": norm, "done": 0,
                                 "total": total, "last_query": "", "result": None,
                                 "error": None, "started": time.time()}
    threading.Thread(target=_run_research_job, args=(job_id, address, name), daemon=True).start()
    return {"status": "running", "job": job_id, "total": total}


# ---- Registry of Deeds (recorded documents for an owner) ----------------
# The Hampden registry is behind Incapsula: plain HTTP is fully blocked, so this runs a
# real browser, one fresh context per lookup, paced ~25s apart (see deeds/hampden_browser).
# That's ~6s+ per lookup, so it follows the same rule as research: background job, cached
# per owner, never in the request path.
DEEDS_JOBS = {}            # job_id -> job dict
DEEDS_DONE = {}            # normalized owner name -> finished doc
_DJOBS_LOCK = threading.Lock()


def _norm_owner(name):
    return re.sub(r"\s+", " ", (name or "").strip().upper())


def _run_deeds_job(job_id, owner):
    job = DEEDS_JOBS[job_id]
    try:
        records = deeds_fetch(owner)
        doc = {"owner": owner, "summary": deeds_summarize(records), "records": records,
               "source": "Hampden County Registry of Deeds", "fetched_at": time.strftime("%Y-%m-%d")}
        job["result"] = doc
        job["status"] = "done"
        DEEDS_DONE[_norm_owner(owner)] = doc
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def start_deeds(owner, peek=False):
    """Cached result, an in-flight job, or a freshly started one. Never blocks.

    peek=True: report cache/in-flight status ONLY and never START a browser job. The UI
    calls this on render so it can auto-show an already-cached result WITHOUT silently
    kicking off a ~30s registry scrape for a property the user only glanced at. A real
    lookup requires an explicit click (peek=False)."""
    key = _norm_owner(owner)
    if not key:
        return {"status": "error", "error": "no owner name"}
    if key in DEEDS_DONE:
        return {"status": "done", "cached": True, "result": DEEDS_DONE[key]}
    with _DJOBS_LOCK:
        for jid, j in DEEDS_JOBS.items():
            if j["key"] == key and j["status"] == "running":
                return {"status": "running", "job": jid}
        if peek:
            return {"status": "idle", "cached": False}   # not cached, and we won't start one
        job_id = uuid.uuid4().hex[:12]
        DEEDS_JOBS[job_id] = {"status": "running", "key": key, "result": None,
                              "error": None, "started": time.time()}
    threading.Thread(target=_run_deeds_job, args=(job_id, owner), daemon=True).start()
    return {"status": "running", "job": job_id}


def deeds_status(job_id):
    job = DEEDS_JOBS.get(job_id)
    if not job:
        return {"status": "error", "error": "unknown job"}
    out = {"status": job["status"], "elapsed": round(time.time() - job["started"])}
    if job["status"] == "done":
        out["result"] = job["result"]
    elif job["status"] == "error":
        out["error"] = job["error"]
    return out


# All caches (enrichment / research / deeds) are defined above, so the pre-cache can now
# be loaded into them.
load_precache()


def research_status(job_id):
    job = RESEARCH_JOBS.get(job_id)
    if not job:
        return {"status": "error", "error": "unknown job"}
    out = {"status": job["status"], "done": job["done"], "total": job["total"],
           "last_query": job["last_query"]}
    if job["status"] == "done":
        out["result"] = job["result"]
    elif job["status"] == "error":
        out["error"] = job["error"]
    return out


# ---- HTTP handler --------------------------------------------------------
# Security headers on every response (defense in depth — no exploitable XSS was found in
# testing, but these harden against classes of attack rather than specific bugs):
#   CSP        — inline script/style are required (the whole frontend is one inline page),
#                but connect/img/frame/object are locked down; fonts allowed from Google
#                Fonts (the only external resource the page loads).
#   HSTS       — Render terminates TLS for us; pin browsers to HTTPS.
#   nosniff / frame-ancestors / X-Frame-Options — MIME-sniffing + clickjacking protection.
#   Referrer-Policy — don't leak searched addresses to external sites via Referer.
_SEC_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self' 'unsafe-inline'; "
     "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
     "font-src https://fonts.gstatic.com; img-src 'self' data:; "
     "connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
]


class Handler(BaseHTTPRequestHandler):
    # don't advertise "BaseHTTP/0.6 Python/3.12" in the Server header
    server_version = "DealSynq"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in _SEC_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif u.path == "/api/search":
            q = parse_qs(u.query).get("q", [""])[0]
            self._send(200, json.dumps(search(q)))
        elif u.path == "/api/extra":
            # Slow, best-effort, third-party (OSM Overpass) extras — fetched by the
            # frontend AFTER the main result renders, never blocking it.
            qs = parse_qs(u.query)
            apn = qs.get("apn", [""])[0]
            lat = float(qs.get("lat", ["0"])[0])
            lon = float(qs.get("lon", ["0"])[0])
            land_sqft = float(qs.get("land_sqft", ["0"])[0])
            building_sqft = float(qs.get("building_sqft", ["0"])[0])
            stories = qs.get("stories", [None])[0]
            # Both OSM lookups run in PARALLEL under ONE shared, absolute deadline, passed
            # into each so their own internal retries (mirror rotation, and businesses_at's
            # narrow-then-widen sequence) self-bound to it rather than each independently
            # retrying and blowing past whatever time we can actually afford. On some hosts
            # (a cloud datacenter IP) Overpass responds slowly, which used to make this
            # endpoint block 60s+ before this fix. shutdown(wait=False) means even if a
            # request somehow still overruns, it can never hold up the HTTP response.
            deadline = time.time() + 12.0   # client-side abort is 14s — 2s margin for transit
            ex = ThreadPoolExecutor(max_workers=2)
            f_biz = ex.submit(businesses_at, apn, lat, lon, land_sqft, deadline)
            f_site = ex.submit(site_at, apn, lat, lon, land_sqft, building_sqft, stories, deadline)

            def _bounded(fut):
                try:
                    return fut.result(timeout=max(0.1, deadline - time.time()))
                except Exception:
                    return None
            biz = _bounded(f_biz)
            site = _bounded(f_site)
            ex.shutdown(wait=False)
            self._send(200, json.dumps({"businesses": biz, "site": site}))
        elif u.path == "/api/research":
            # Kick off (or return cached) deep web research for an address. Slow, so this
            # returns immediately with either a cached result or a job id to poll.
            qs = parse_qs(u.query)
            addr = qs.get("q", [""])[0]
            name = qs.get("name", [None])[0] or None
            self._send(200, json.dumps(start_research(addr, name)))
        elif u.path == "/api/research/status":
            job = parse_qs(u.query).get("job", [""])[0]
            self._send(200, json.dumps(research_status(job)))
        elif u.path == "/api/deeds":
            # Registry of Deeds for an owner. Browser-driven + paced, so it's a background
            # job like research: returns a cached doc or a job id to poll.
            qp = parse_qs(u.query)
            owner = qp.get("owner", [""])[0]
            peek = qp.get("peek", ["0"])[0] == "1"   # peek: check cache WITHOUT starting a job
            self._send(200, json.dumps(start_deeds(owner, peek=peek)))
        elif u.path == "/api/deeds/status":
            job = parse_qs(u.query).get("job", [""])[0]
            self._send(200, json.dumps(deeds_status(job)))
        else:
            self._send(404, json.dumps({"error": "not found"}))


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0C1B38">
<title>DealSynq — Property Intelligence</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Fraunces:ital,opsz,wght@0,9..144,400..800;1,9..144,400..800&display=swap">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='22' fill='%230E2C49'/%3E%3Ctext x='50' y='70' font-size='62' font-family='Georgia,serif' font-weight='bold' fill='%23CFA24C' text-anchor='middle'%3ED%3C/text%3E%3C/svg%3E">
<style>
  /* ==================================================================== */
  /*  DealSynq design system — institutional CRE intelligence.            */
  /*  One coherent layer: deep navy + burnished gold + cool neutrals,     */
  /*  Fraunces display serif over a refined system sans for data.         */
  /* ==================================================================== */
  :root{
    /* brand */
    --navy:#0B2540; --navy2:#143A5E; --navy3:#1C4E79;
    --gold:#A9822F; --gold2:#CFA452; --gold3:#E9D5A4;
    /* neutrals (cool, blue-cast) */
    --ink:#1A2633; --slate:#5B6B7C; --faint:#8794A4;
    --paper:#EFF2F6; --card:#FFFFFF; --mist:#F5F7FA; --mist2:#ECF0F5;
    --line:#E2E8EF; --hairline:#EBEFF4;
    /* semantic status (distinct from the gold accent) */
    --verified:#1B7A45; --strong:#2563A0; --est:#A2731F; --unres:#8E9BA9;
    --blue:#2563A0;
    /* elevation */
    --sh-sm:0 1px 2px rgba(11,37,64,.06);
    --sh-md:0 1px 2px rgba(11,37,64,.05),0 12px 32px rgba(11,37,64,.08);
    --sh-lg:0 2px 6px rgba(11,37,64,.12),0 24px 60px rgba(11,37,64,.22);
    /* type */
    --wordmark:'Cormorant Garamond','Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif;
    --serif:'Fraunces','Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif;
    --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
    --mono:ui-monospace,'Cascadia Code','SF Mono',Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html{-webkit-text-size-adjust:100%}
  body{font:15px/1.55 var(--sans);color:var(--ink);-webkit-font-smoothing:antialiased;
       text-rendering:optimizeLegibility;
       background:radial-gradient(1100px 420px at 50% -60px,#FDFEFF 0%,rgba(253,254,255,0) 70%),
                  linear-gradient(180deg,var(--paper) 0%,#EAEDF2 100%);
       background-attachment:fixed}
  ::selection{background:rgba(207,164,82,.28)}
  button{font-family:inherit}
  button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--gold2);outline-offset:2px;border-radius:4px}

  /* ------------------------------ hero ------------------------------- */
  .hero{position:relative;overflow:hidden;color:#fff;text-align:center;
        padding:66px 20px 96px;background:#0C1B38}
  .hero::before{content:"";position:absolute;inset:0;pointer-events:none;
        background:radial-gradient(ellipse at 50% -20%,rgba(40,75,125,.18),transparent 58%)}
  .hero::after{content:"";position:absolute;left:0;right:0;bottom:0;height:2px;
        background:linear-gradient(90deg,transparent 8%,rgba(207,164,82,.85) 50%,transparent 92%)}
  .hero>*{position:relative}
  /* Restrained editorial wordmark matching the approved brand direction. */
  .hero .wordmark{font-family:var(--wordmark);font-weight:600;font-size:48px;letter-spacing:.16em;
        text-indent:.16em;margin:0 0 12px;line-height:.92;color:#fff;
        text-transform:uppercase;text-shadow:0 1px 1px rgba(0,0,0,.18)}
  .hero .wordmark-tag{display:flex;flex-direction:column;align-items:center;gap:10px;margin:0 0 28px}
  .hero .wordmark-tag .rule-row{display:flex;align-items:center;gap:5px}
  .hero .wordmark-tag .rule{width:48px;height:1px;background:var(--gold2)}
  .hero .wordmark-tag .rule.r{background:var(--gold2)}
  .hero .wordmark-tag .dot{width:5px;height:5px;border-radius:50%;background:var(--gold2);flex:none}
  .hero .wordmark-tag .label{font-size:10px;font-weight:600;letter-spacing:.42em;text-transform:uppercase;
        color:#E2B329;text-indent:.42em}
  .hero h1{font-family:var(--serif);font-weight:550;font-size:46px;line-height:1.08;
        letter-spacing:-.01em;margin:18px auto 12px;max-width:700px;color:#F8FAFC;text-wrap:balance}
  .hero h1 em{font-style:italic;font-weight:480;color:var(--gold3)}
  .hero p{color:#AFC4D8;font-size:15.5px;line-height:1.65;max-width:560px;margin:0 auto}

  .searchwrap{max-width:660px;margin:30px auto 0;display:flex;gap:10px}
  .searchbox{flex:1;position:relative;display:flex;align-items:center}
  .searchbox svg{position:absolute;left:17px;width:18px;height:18px;color:#8AA0B5;pointer-events:none}
  #q{flex:1;padding:17px 18px 17px 48px;border:1px solid rgba(255,255,255,.14);border-radius:14px;
     font:16px var(--sans);color:var(--ink);outline:none;background:#fff;
     box-shadow:var(--sh-lg);transition:box-shadow .18s ease,border-color .18s ease}
  #q::placeholder{color:#93A2B2}
  #q:focus{border-color:var(--gold2);box-shadow:0 0 0 4px rgba(207,164,82,.28),var(--sh-lg)}
  #go{border:none;border-radius:14px;padding:0 30px;cursor:pointer;
      font:700 15px/1 var(--sans);letter-spacing:.02em;color:#231903;
      background:linear-gradient(180deg,#E3C075 0%,#C79B44 55%,#B98E3C 100%);
      box-shadow:inset 0 1px 0 rgba(255,255,255,.45),0 8px 22px rgba(169,130,47,.38);
      transition:transform .15s ease,box-shadow .15s ease,filter .15s ease}
  #go:hover{filter:brightness(1.05);transform:translateY(-1px);
      box-shadow:inset 0 1px 0 rgba(255,255,255,.45),0 12px 26px rgba(169,130,47,.44)}
  #go:active{transform:translateY(0);filter:brightness(.98)}
  .hint{color:#8FA6BC;font-size:12.5px;margin-top:16px}
  .hint b{color:#E8EFF6;font-weight:600}
  .examples{max-width:760px;margin:14px auto 0;display:flex;gap:10px;flex-wrap:wrap;justify-content:center}
  .ex{background:rgba(255,255,255,.055);border:1px solid rgba(255,255,255,.13);border-radius:12px;
      padding:10px 16px;cursor:pointer;color:#fff;text-align:left;line-height:1.3;
      -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);
      transition:transform .16s ease,background .16s ease,border-color .16s ease}
  .ex:hover{transform:translateY(-2px);background:rgba(207,164,82,.16);border-color:rgba(207,164,82,.55)}
  .ex b{display:block;font-size:13.5px;font-weight:700;letter-spacing:.01em}
  .ex span{display:block;font-size:10.5px;color:#9DB3C8;margin-top:2px}

  /* --------------------------- layout & cards ------------------------ */
  .wrap{max-width:1020px;margin:-56px auto 28px;padding:0 22px}
  .wrap:empty{margin:0 auto;padding:0}
  .card{background:var(--card);border:1px solid rgba(11,37,64,.09);border-radius:18px;
        padding:26px 28px;margin-bottom:18px;box-shadow:var(--sh-md);
        transition:box-shadow .25s ease;
        animation:rise .5s cubic-bezier(.2,.7,.3,1) both}
  .card:hover{box-shadow:0 2px 4px rgba(11,37,64,.06),0 18px 44px rgba(11,37,64,.11)}
  @keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
  .card:nth-child(2){animation-delay:.05s}.card:nth-child(3){animation-delay:.09s}
  .card:nth-child(4){animation-delay:.13s}.card:nth-child(n+5){animation-delay:.16s}

  /* --------------------------- property header ----------------------- */
  .prophead{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:14px}
  .prophead h2{font-family:var(--serif);font-weight:580;font-size:31px;line-height:1.1;
        letter-spacing:-.01em;color:var(--navy)}
  .prophead .sub{color:var(--slate);font-size:13.5px;margin-top:7px;letter-spacing:.01em}
  .badge{display:inline-flex;align-items:center;gap:7px;padding:7px 15px;border-radius:999px;
        font-size:10.5px;font-weight:800;letter-spacing:.09em;color:#fff;box-shadow:var(--sh-sm)}
  .badge::before{content:"";width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,.9);
        box-shadow:0 0 0 3px rgba(255,255,255,.22)}

  /* ---------------------------- KPI tiles ---------------------------- */
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(126px,1fr));gap:12px;margin-top:22px}
  .kpi{position:relative;overflow:hidden;text-align:left;border:1px solid var(--line);border-radius:14px;
       padding:15px 16px 13px;
       background:radial-gradient(110px 64px at 100% 0,rgba(207,164,82,.05),transparent 70%),
                  linear-gradient(180deg,#fff 0%,#FBFCFE 100%);
       transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease}
  .kpi::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;
       background:linear-gradient(90deg,var(--gold2),rgba(207,164,82,0) 78%)}
  .kpi:hover{transform:translateY(-2px);box-shadow:var(--sh-md);border-color:#D7E0EA}
  .kpi .n{font-size:24px;font-weight:800;color:var(--navy);letter-spacing:-.02em;
       font-variant-numeric:tabular-nums lining-nums;font-feature-settings:"tnum"}
  .kpi .l{font-size:9.5px;font-weight:700;color:var(--faint);text-transform:uppercase;
       letter-spacing:.11em;margin-top:6px}

  /* -------------------------- section headers ------------------------ */
  h3.sec{display:flex;align-items:center;flex-wrap:wrap;gap:8px;min-height:18px;
       font-family:var(--sans);font-size:12px;font-weight:800;text-transform:uppercase;
       letter-spacing:.14em;color:var(--navy);position:relative;padding:0 0 0 18px;margin:2px 0 16px}
  h3.sec::before{content:"";position:absolute;left:2px;top:50%;width:7px;height:7px;border-radius:1.5px;
       transform:translateY(-50%) rotate(45deg);
       background:linear-gradient(135deg,var(--gold2),var(--gold))}
  h3.sec::after{content:"";flex:1 1 24px;height:1px;margin-left:8px;min-width:24px;
       background:linear-gradient(90deg,var(--line),rgba(226,232,239,0))}
  h3.sec span{text-transform:none;letter-spacing:.01em;font-weight:600}

  /* ------------------------------ tables ----------------------------- */
  .tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--line);
       border-radius:14px;box-shadow:var(--sh-sm);scrollbar-width:thin;scrollbar-color:#C4CFDB transparent}
  .tscroll::-webkit-scrollbar{height:8px}
  .tscroll::-webkit-scrollbar-thumb{background:#C4CFDB;border-radius:8px}
  .tscroll::-webkit-scrollbar-track{background:transparent}
  table{width:100%;border-collapse:collapse;font-size:13.5px}
  th{background:#F4F7FA;color:var(--slate);text-align:left;padding:11px 14px;
     font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;
     border-bottom:1px solid var(--line);white-space:nowrap}
  td{padding:10px 14px;border-bottom:1px solid var(--hairline)}
  tr:nth-child(even) td{background:#FAFBFD}
  tr:hover td{background:#F3F7FB}
  tr:last-child td{border-bottom:none}
  .num{text-align:right;font-variant-numeric:tabular-nums lining-nums;font-feature-settings:"tnum"}
  .tot td{font-weight:800;color:var(--navy);background:var(--mist2)!important;
       border-top:2px solid var(--gold2)}

  /* ---------------------------- key / value --------------------------- */
  .kv{display:grid;grid-template-columns:196px 1fr;gap:11px 18px;font-size:14px}
  .kv .k{color:var(--faint);font-weight:700;font-size:11px;text-transform:uppercase;
       letter-spacing:.08em;padding-top:2.5px}

  /* --------------------- pills / chips / badges ----------------------- */
  .pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:10.5px;font-weight:800;
       letter-spacing:.04em;color:#fff;background:var(--strong)}
  .zbadge{display:inline-block;background:linear-gradient(180deg,var(--gold2),var(--gold));color:#fff;
       font-weight:800;font-size:11px;letter-spacing:.05em;padding:3px 11px;border-radius:8px;margin-left:8px;
       box-shadow:var(--sh-sm)}
  .uchips{display:flex;flex-wrap:wrap;gap:6px}
  .uchip{font-size:12px;padding:4px 12px;border-radius:999px;font-weight:600;border:1px solid}
  .uchip.p{background:#EAF6EF;border-color:#C4E3D0;color:#186B3E}
  .uchip.s{background:#FBF3E1;border-color:#EBD8AC;color:#7E5A12}
  .uchip.n{background:#F2F5F8;border-color:var(--line);color:var(--slate)}
  .uchip .t{opacity:.65;font-size:10px;margin-left:3px}
  .zgrp{margin-top:14px}
  .zgrp .lbl{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.09em;margin-bottom:7px}

  /* research filter chips */
  .rchips{display:flex;flex-wrap:wrap;gap:7px;margin:4px 0 2px}
  .rchip{background:#fff;border:1px solid var(--line);border-radius:999px;padding:5px 13px;
       font-size:12px;color:var(--navy);font-weight:600;cursor:pointer;font-family:inherit;line-height:1.4;
       transition:background .15s ease,border-color .15s ease,color .15s ease}
  .rchip b{color:var(--gold);font-weight:800;font-feature-settings:"tnum"}
  .rchip:hover{border-color:var(--gold2);background:#FBF6E9}
  .rchip:focus-visible{outline:2px solid var(--gold2);outline-offset:2px}
  .rchip.on{background:var(--navy);border-color:var(--navy);color:#fff}
  .rchip.on b{color:var(--gold3)}

  /* tiny metadata tags */
  .rtag{display:inline-block;background:var(--mist2);border-radius:5px;padding:1px 7px;
       font-family:var(--mono);font-size:10px;color:var(--slate);margin-left:6px;
       vertical-align:1px;letter-spacing:0}

  /* recorded-document summary pills */
  .dsum{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 6px}
  .dpill{border:1px solid var(--line);border-radius:11px;padding:8px 14px;background:#fff;
       font-size:12px;color:var(--slate);font-weight:600;box-shadow:var(--sh-sm)}
  .dpill b{color:var(--navy);font-size:16px;font-weight:800;margin-right:6px;
       font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
  .dpill.hot{background:#FCF0EF;border-color:#F0CFCB;color:#93332B}
  .dpill.hot b{color:#B3261E}
  .dpill.clear{background:#EAF6EF;border-color:#C4E3D0;color:#186B3E}
  .dpill.clear b{color:#1B7A45}
  .dtype{display:inline-block;padding:2px 9px;border-radius:6px;font-size:10.5px;font-weight:700;
       letter-spacing:.02em;background:var(--mist2);color:var(--slate)}
  .dtype.mtg{background:#FBF3E1;color:#7E5A12}
  .dtype.deed{background:#E9F1F9;color:#215585}
  .dtype.lien{background:#FCF0EF;color:#B3261E}

  /* -------------------------- confidence tiers ------------------------ */
  .tiers .row{display:flex;gap:14px;padding:10px 0;border-bottom:1px solid var(--hairline);align-items:flex-start}
  .tiers .row:last-child{border:none}
  .tlabel{width:100px;flex:none;text-align:center;padding:5px 0;border-radius:8px;color:#fff;
       font-size:10px;font-weight:800;letter-spacing:.08em}

  /* ----------------------- notes, cites, empty ------------------------ */
  .muted{color:var(--slate);font-size:12.5px;margin-top:8px;line-height:1.6}
  .empty{text-align:center;color:var(--slate);padding:44px 16px}
  .empty>b{font-family:var(--serif);font-size:18px;font-weight:600;color:var(--navy)}
  .note{background:linear-gradient(180deg,#FCF7EA,#FAF4E3);border:1px solid #EFE2C0;
       border-left:3px solid var(--gold2);padding:13px 16px;border-radius:12px;
       font-size:13px;line-height:1.6;color:#63501F;margin-top:14px}
  .cite{background:var(--mist);border:1px solid var(--hairline);border-left:3px solid var(--strong);
       padding:10px 14px;border-radius:10px;font-size:11.5px;line-height:1.65;color:var(--slate);margin-top:16px}
  /* small uniform per-card provenance line — every card gets one; the more detailed
     .cite/.muted footnotes some cards already have say MORE than this, not less, so this
     never replaces those, only fills cards that had no source line at all. */
  .srcbox{margin-top:12px;padding-top:8px;border-top:1px dashed var(--hairline);
       font-size:10.5px;letter-spacing:.03em;text-transform:uppercase;font-weight:700;color:var(--faint)}
  .srcbox b{color:var(--slate);text-transform:none;letter-spacing:0;font-weight:600}
  a.src{color:var(--blue);font-size:12px;font-weight:600;text-decoration:none}
  a.src:hover{text-decoration:underline;text-decoration-color:var(--gold2)}

  /* ------------------------- loading & progress ----------------------- */
  .loading{text-align:center;color:var(--slate);padding:44px 10px;font-size:14px}
  .spin{width:28px;height:28px;border:3px solid var(--mist2);border-top-color:var(--gold2);
       border-radius:50%;margin:0 auto 14px;animation:sp .7s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}
  .prog{height:6px;background:var(--mist2);border-radius:999px;overflow:hidden;margin:14px 0 8px}
  .prog>i{display:block;height:100%;border-radius:999px;width:0;
       background:linear-gradient(90deg,var(--strong),var(--gold2));
       background-size:200% 100%;animation:flow 1.6s linear infinite;transition:width .35s ease}
  @keyframes flow{to{background-position:-200% 0}}

  /* ------------------------ suggestion buttons ------------------------ */
  .sugg{display:block;width:100%;text-align:left;background:#fff;border:1px solid var(--line);
       border-radius:12px;padding:11px 15px;margin-bottom:8px;cursor:pointer;
       transition:border-color .15s ease,box-shadow .15s ease,transform .15s ease}
  .sugg:hover{border-color:var(--gold2);box-shadow:var(--sh-sm);transform:translateY(-1px)}
  .sugg b{display:block;font-size:14px;color:var(--navy);font-weight:700}
  .sugg span{display:block;font-size:11.5px;color:var(--slate);margin-top:2px}

  /* ------------------------------ tenants ----------------------------- */
  .bizwrap{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
  .biz{background:#fff;border:1px solid var(--line);border-left:3px solid var(--gold2);
       border-radius:12px;padding:10px 13px;box-shadow:var(--sh-sm);
       transition:transform .15s ease,box-shadow .15s ease}
  .biz:hover{transform:translateY(-1px);box-shadow:var(--sh-md)}
  .biz b{display:block;font-size:13.5px;color:var(--navy);font-weight:700}
  .biz span{display:block;font-size:11px;color:var(--slate);margin-top:2px;text-transform:capitalize}

  /* --------------------------- action buttons ------------------------- */
  .rbtn{border:none;border-radius:12px;padding:12px 22px;cursor:pointer;color:#fff;
       font:700 13.5px/1.2 var(--sans);letter-spacing:.03em;
       background:linear-gradient(180deg,#1A4A74 0%,#0F3050 60%,#0B2540 100%);
       box-shadow:inset 0 1px 0 rgba(255,255,255,.14),0 6px 16px rgba(11,37,64,.28);
       transition:transform .15s ease,box-shadow .15s ease,filter .15s ease}
  .rbtn:hover{filter:brightness(1.12);transform:translateY(-1px);
       box-shadow:inset 0 1px 0 rgba(255,255,255,.14),0 10px 22px rgba(11,37,64,.32)}
  .rbtn:active{transform:translateY(0)}
  .rbtn:disabled{opacity:.55;cursor:default;transform:none;filter:none}

  /* --------------------------- research list -------------------------- */
  .rcat{margin-top:14px}
  .rcat h4{font-size:12px;color:var(--navy);margin:0 0 6px;text-transform:uppercase;letter-spacing:.08em}
  .rsrc{padding:10px 2px;border-bottom:1px solid var(--hairline)}
  .rsrc:last-child{border-bottom:none}
  .rsrc a{color:var(--navy2);font-size:13.5px;font-weight:650;text-decoration:none}
  .rsrc a:hover{color:var(--blue);text-decoration:underline;text-decoration-color:var(--gold2);
       text-underline-offset:3px}
  .rsrc .dom{color:var(--slate);font-size:11.5px}
  .rsrc .snip{color:#4A5866;font-size:12px;line-height:1.55;margin-top:3px}

  /* ------------------------------ footer ------------------------------ */
  .foot{max-width:1020px;margin:0 auto;padding:16px 22px 54px;color:var(--faint);
       font-size:11.5px;text-align:center;line-height:1.8}
  .foot .brand{font-family:var(--wordmark);font-weight:600;color:var(--navy);
       letter-spacing:.18em;font-size:14px}
  .foot a{color:var(--slate)}
  .foot .dot{margin:0 7px;opacity:.45;color:var(--gold)}

  /* ------------------------------ mobile ------------------------------ */
  @media(max-width:680px){
    .hero{padding:44px 16px 70px}
    .hero .wordmark{font-size:38px;letter-spacing:.14em;text-indent:.14em}
    .hero .wordmark-tag .rule{width:42px}
    .hero h1{font-size:30px}
    .hero p{font-size:14px}
    .searchwrap{flex-direction:column}
    #q{padding:15px 16px 15px 46px}
    #go{padding:15px}
    .wrap{padding:0 13px;margin-top:-44px}
    .card{padding:20px 17px;border-radius:16px}
    .prophead h2{font-size:24px}
    .kpis{gap:10px;grid-template-columns:repeat(auto-fit,minmax(108px,1fr))}
    .kpi{padding:13px 13px 11px}
    .kpi .n{font-size:20px}
    .kv{grid-template-columns:1fr;gap:2px 0}
    .kv .k{margin-top:11px}
    .tscroll table{min-width:560px}
    h3.sec{font-size:11px}
  }

  /* --------------------------- reduced motion -------------------------- */
  @media(prefers-reduced-motion:reduce){
    .card{animation:none}
    .prog>i{animation:none}
    .kpi,.ex,.biz,.sugg,#go,.rbtn{transition:none}
    .kpi:hover,.ex:hover,.biz:hover,.sugg:hover,#go:hover,.rbtn:hover{transform:none}
    .spin{animation-duration:1.5s}
  }
  /* ---- Disposition signals ---- */
  .disphead{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:4px}
  .dispband{display:inline-flex;align-items:center;gap:7px;padding:6px 14px;border-radius:30px;font-weight:800;
            font-size:11.5px;letter-spacing:.6px;text-transform:uppercase}
  .dispband::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
  .dispband.low{background:#E7F5EC;color:#1E7A46}
  .dispband.mod{background:#FBF3E2;color:#8A5D0F}
  .dispband.elev{background:#FDE8E8;color:#B3261E}
  .dispband.market{background:linear-gradient(180deg,var(--gold2),var(--gold));color:#fff}
  .dispsum{color:var(--slate);font-size:13.5px;margin:8px 0 6px}
  .dfac{display:flex;gap:12px;padding:11px 0;border-bottom:1px solid var(--line)}
  .dfac:last-child{border-bottom:none}
  .dfac .arw{flex:none;width:26px;height:26px;border-radius:8px;display:flex;align-items:center;
             justify-content:center;font-weight:800;font-size:13px}
  .arw.up{background:#FDE8E8;color:#B3261E}
  .arw.dn{background:#E7F5EC;color:#1E7A46}
  .arw.nt{background:var(--mist);color:var(--slate)}
  .dfac .fl{font-weight:700;color:var(--navy);font-size:13.5px}
  .dfac .ff{color:var(--slate);font-size:12.5px;margin-top:1px;line-height:1.45}
  .dfac .fsrc{font-size:10.5px;color:var(--faint);margin-top:3px;text-transform:uppercase;letter-spacing:.4px}
  .dpend{background:var(--mist);border-radius:10px;padding:9px 12px;font-size:12px;color:var(--slate);margin-top:12px}
  .visually-hidden{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
</style></head><body>
<header class="hero">
  <div class="wordmark" aria-label="DealSynq">DEALSYNQ</div>
  <div class="wordmark-tag">
    <div class="rule-row"><span class="rule l"></span><span class="dot"></span><span class="rule r"></span></div>
    <div class="label">Property Intelligence</div>
  </div>
  <h1>Every public record on a property, in <em>one search</em>.</h1>
  <p>Ownership &amp; assemblage, zoning, recorded deeds &amp; mortgages, tenants, and a deep web sweep &mdash; assembled live from public sources.</p>
  <div class="searchwrap" role="search">
    <label for="q" class="visually-hidden">Search by Springfield address or owner name</label>
    <div class="searchbox">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
      <input id="q" placeholder="e.g. 380 Cooley St  &mdash;  or an owner name" autofocus aria-label="Search by Springfield address or owner name">
    </div>
    <button id="go">Search</button>
  </div>
  <div class="hint">Type any Springfield address &mdash; or try one of these:</div>
  <div class="examples">
    <button class="ex" data-q="380 Cooley St"><b>380 Cooley St</b><span>Five Town Plaza &bull; full profile</span></button>
    <button class="ex" data-q="415 Cooley St"><b>415 Cooley St</b><span>supermarket &bull; commercial</span></button>
    <button class="ex" data-q="115 Cooley St"><b>115 Cooley St</b><span>single-family home</span></button>
    <button class="ex" data-q="1391 Main St"><b>1391 Main St</b><span>another address</span></button>
  </div>
</header>
<main class="wrap" id="out" aria-live="polite" aria-busy="false"></main>
<footer class="foot">
  <div><span class="brand">DEALSYNQ</span> &nbsp;&bull;&nbsp; Property Intelligence &mdash; single-property proof of concept</div>
  <div style="margin-top:6px">Data from public sources: Springfield WebGIS &amp; Assessor
    <span class="dot">&bull;</span> Springfield Zoning Ordinance <span class="dot">&bull;</span> OpenStreetMap
    <span class="dot">&bull;</span> FEMA <span class="dot">&bull;</span> EPA <span class="dot">&bull;</span> U.S. Census
    <span class="dot">&bull;</span> SEC EDGAR <span class="dot">&bull;</span> Registry of Deeds
    <span class="dot">&bull;</span> DuckDuckGo / Mojeek web search</div>
  <div style="margin-top:6px">Each section cites its own source. Figures are for evaluation, not a substitute for title, survey, or legal review.</div>
</footer>

<script>
const $=s=>document.querySelector(s), out=$("#out");
function money(n){return "$"+Math.round(n).toLocaleString()}
function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
function escA(s){return esc(s).replace(/"/g,"&quot;").replace(/'/g,"&#39;")}  // attribute-safe
function loadingHTML(msg){return '<div class="loading"><div class="spin"></div>'+msg+'</div>';}
// wrap every result table in a horizontally-scrollable container (mobile-safe)
function wrapTables(root){
  root.querySelectorAll("table").forEach(t=>{
    if(t.parentElement&&t.parentElement.classList.contains("tscroll"))return;
    const w=document.createElement("div"); w.className="tscroll";
    t.parentNode.insertBefore(w,t); w.appendChild(t);
  });
}

// Every search bumps GEN. Background work (OSM extras, deeds, research) captures the GEN
// it started under and bails if a newer search has begun — otherwise a slow job for the
// PREVIOUS address keeps polling and renders its results into the new address's card.
let GEN=0;
const stale=g=>g!==GEN;

async function run(){
  const q=$("#q").value.trim();
  if(!q){                                // blank/whitespace: say so, don't leave stale results up
    out.innerHTML='<div class="card empty" style="text-align:center"><b>Enter an address or owner name.</b><br>This demo covers Springfield, MA. Try <b>380 Cooley St</b>.</div>';
    return;
  }
  const gen=++GEN;
  out.setAttribute("aria-busy","true");
  out.innerHTML=loadingHTML("Resolving &hellip;");
  let d;
  try{ const r=await fetch("/api/search?q="+encodeURIComponent(q)); d=await r.json(); }
  catch(e){ if(!stale(gen)){ out.setAttribute("aria-busy","false"); out.innerHTML='<div class="card empty" style="text-align:center"><b>The lookup failed &mdash; check your connection and try again.</b></div>'; } return; }
  if(stale(gen)) return;                 // a newer search overtook this one
  out.setAttribute("aria-busy","false");
  if(!d.matched){
    // Headline depends on WHY there's no single match, so the user knows what to do next.
    const amb=d.ambiguous;
    let head, lead;
    if(amb==='street'){ head='&ldquo;'+esc(q)+'&rdquo; is a street, not one property.'; lead='Pick the parcel you mean:'; }
    else if(amb==='owner'){ head='Several owners match &ldquo;'+esc(q)+'&rdquo;.'; lead='Pick a property:'; }
    else if(amb==='too_broad'){ head=esc(d.error||'Too many matches.'); lead=''; }
    else if(amb==='number'||amb==='short'){ head=esc(d.error||'Please be more specific.'); lead=''; }
    else if(d.suggestions&&d.suggestions.length){ head='No parcel is recorded at that exact address.'; lead='It may be a secondary/entrance address &mdash; other parcels on that street:'; }
    else { head='No match for &ldquo;'+esc(q)+'&rdquo;.'; lead=''; }
    let sg='';
    if(d.suggestions&&d.suggestions.length){
      sg='<div style="margin-top:14px;text-align:left">'+(lead?'<div class="muted" style="margin-bottom:8px">'+lead+'</div>':'');
      d.suggestions.forEach(s=>{sg+='<button class="sugg" data-q="'+escA(s.address)+'"><b>'+esc(smartTitle(s.address))+'</b><span>'+esc(smartTitle(s.owner))+'</span></button>';});
      sg+='</div>';
    } else if(!amb){
      sg='<br>This demo covers Springfield, MA. Try <b>380 Cooley St</b>.';
    }
    out.innerHTML='<div class="card empty" style="text-align:center"><b>'+head+'</b>'+sg+'</div>';
    document.querySelectorAll(".sugg").forEach(b=>b.onclick=()=>{$("#q").value=b.dataset.q;run();});
    return;
  }
  render(d);
  if(d.extra_params) loadExtras(d.extra_params, gen);  // fire-and-forget, background, never blocks
}

// per-source copy for the Businesses Reported Here card — keeps the honesty caveats
// specific to whichever source actually answered (see businesses_at()'s source priority:
// Foursquare -> Yelp -> OpenStreetMap). NO source gets to claim "operating"/"current":
// every one of these is a third-party directory that can lag closures and rebrands, so
// the copy consistently says REPORTED, shows distances, and asks for verification.
const BIZ_SOURCE_LABELS={
  foursquare:{label:"reported by Foursquare &bull; third-party, possibly outdated", verb:"listed",
    found:'<b>Businesses reported at or near this parcel</b> by Foursquare. Third-party directory data &mdash; listings can lag closures and rebrands, so treat these as reported occupants, <b>not confirmed current operators</b>, and verify before relying on any one. Distances shown per listing.',
    empty:'No businesses are listed at or near this parcel on Foursquare &mdash; could be a residential property, vacant lot, or a business too new/small to be listed. This is a <b>coverage gap, not confirmed evidence the property is vacant</b>.'},
  yelp:{label:"reported by Yelp &bull; third-party, possibly outdated", verb:"listed",
    found:'<b>Businesses reported at or near this parcel</b> by Yelp. Third-party directory data &mdash; listings can lag closures and rebrands, so treat these as reported occupants, <b>not confirmed current operators</b>, and verify before relying on any one. Distances shown per listing.',
    empty:'No businesses are listed at or near this parcel on Yelp &mdash; could be a residential property, vacant lot, or a business too new/small to be listed. This is a <b>coverage gap, not confirmed evidence the property is vacant</b>.'},
  osm:{label:"reported by OpenStreetMap &bull; volunteer-mapped, possibly outdated", verb:"mapped",
    found:'<b>Businesses reported at or near this parcel</b> in OpenStreetMap. Volunteer-maintained &mdash; a name can lag a closure/rebrand by years: treat these as mapped occupants, <b>not confirmed current operators</b>, and verify before relying on any one. Distances shown per listing.',
    empty:'No businesses are mapped at or near this parcel in OpenStreetMap. OSM is volunteer-mapped, so smaller tenants are frequently absent &mdash; this is a <b>coverage gap, not evidence the property is vacant</b>.'},
};
function renderExtraCards(bizResult,st,failReason){
  let h='';
  // bizResult is {source:"foursquare"|"yelp"|"osm", items:[...]} from the server, or null
  // on failure — source tells us which one actually answered so every caveat below matches
  // reality instead of assuming a fixed source.
  const biz = bizResult ? bizResult.items : null;
  const meta = BIZ_SOURCE_LABELS[bizResult ? bizResult.source : "osm"] || BIZ_SOURCE_LABELS.osm;
  // ALWAYS render this section, for every address. An empty result is information too —
  // silently hiding the card just looks broken.
  h+='<div class="card"><h3 class="sec">Businesses Reported Here <span style="font-size:11px;color:var(--slate);font-weight:600">&bull; '+meta.label+'</span></h3>';
  if(biz&&biz.length){
    // the server flags results that only turned up after widening the search (OSM path) or
    // fell outside the real parcel boundary (any source) — those are NEAR the parcel, not
    // on it. Never claim a neighbour is a tenant.
    const onParcel=biz.filter(x=>!x.widened);
    const shown=onParcel.length?onParcel:biz;
    h+='<div class="bizwrap">';
    shown.forEach(x=>{
      // humanize "steak_house"/"italian;pizza" and use a LITERAL middot — never the HTML
      // entity "&bull;" here: this string is passed through esc(), which would turn it into
      // literal text "&bull;" and (with text-transform) render "&Bull;".
      const clean=s=>String(s||"").replace(/[_;]+/g," ").trim();
      const sub=[clean(x.type), clean(x.cuisine)].filter(Boolean).join(" · ");
      const dist=(x.distance_m!=null?(" · "+x.distance_m+"m"):"");
      h+='<div class="biz"><b>'+esc(x.name)+'</b><span>'+esc(sub+dist)+'</span></div>';
    });
    h+='</div>';
    h+= onParcel.length
      ? '<div class="muted">'+meta.found+'</div>'
      : '<div class="muted"><b>Nothing is '+meta.verb+' on this parcel itself</b> &mdash; these are the nearest '+meta.verb+' businesses (see distances).</div>';
  } else if(biz){   // reached the source, genuinely nothing nearby
    h+='<div class="note">'+meta.empty+'</div>';
  } else {          // null = the lookup failed / timed out
    h+='<div class="note">Couldn&rsquo;t reach the business-lookup source just now &mdash; try the search again in a moment. This is a <b>lookup failure, not a finding</b>.'
      +(failReason?(' <span style="color:var(--faint)">(reason: '+esc(failReason)+')</span>'):'')+'</div>';
  }
  h+='</div>';
  if(st&&st.footprint_sqft){
    h+='<div class="card"><h3 class="sec">Building Footprint &amp; Site <span style="font-size:11px;color:var(--slate);font-weight:600">&bull; '+esc(st.footprint_source||"OSM")+'</span></h3><div class="kpis">';
    h+=kpi(st.footprint_sqft.toLocaleString(),"Footprint SF");
    if(st.roof_area_sqft) h+=kpi(st.roof_area_sqft.toLocaleString(),"Roof SF (est)");
    if(st.estimated_height_ft) h+=kpi(st.estimated_height_ft+"'","Height (est)");
    if(st.existing_far!=null) h+=kpi(st.existing_far,"Existing FAR");
    if(st.estimated_parking_spaces) h+=kpi("~"+st.estimated_parking_spaces.toLocaleString(),"Parking (est)");
    if(st.estimated_solar_kw) h+=kpi("~"+st.estimated_solar_kw.toLocaleString()+" kW","Solar (est)");
    const fpsrc=(st.footprint_source||"").toLowerCase().indexOf("assessor")>=0
      ? 'Footprint is the <b>assessor&rsquo;s single-story building area</b> (no OSM polygon was mapped here)'
      : 'Footprint &amp; roof area are from <b>OpenStreetMap building polygons</b>';
    h+='</div><div class="muted">'+fpsrc+'; height, parking, solar and FAR are <b>estimates</b> derived from footprint + parcel size. Max-permitted FAR, LEED and Energy Star require the zoning ordinance and USGBC/EPA registries (not yet wired).</div></div>';
  }
  return h;
}

async function loadExtras(p, gen){
  const el=document.getElementById("extras");
  if(!el) return;   // user navigated away / searched again already
  const ctrl=new AbortController();
  const timer=setTimeout(()=>ctrl.abort(), 14000);  // hard cap — generous since this is
                                                    // background-only and never blocks the UI
  let reason=null;
  try{
    const qs=new URLSearchParams({apn:p.apn, lat:p.lat, lon:p.lon, land_sqft:p.land_sqft,
                                  building_sqft:p.building_sqft, stories:p.stories||""});
    const r=await fetch("/api/extra?"+qs.toString(), {signal:ctrl.signal});
    if(!r.ok){ reason="HTTP "+r.status; throw new Error(reason); }   // e.g. a 5xx error page
    const d=await r.json();
    if(stale(gen)) return;                      // results belong to a previous address
    const cur=document.getElementById("extras");
    if(cur) cur.innerHTML=renderExtraCards(d.businesses,d.site);
    return;
  }catch(e){
    // classify WHY it failed so the on-page message says something real instead of a
    // generic "couldn't reach it" every time — makes this diagnosable from a screenshot
    // alone, without needing DevTools.
    if(!reason) reason = e.name==="AbortError" ? "timed out after 14s"
      : (e.message && /failed to fetch|network/i.test(e.message)) ? "network error: "+e.message
      : (e.message || String(e));
    if(stale(gen)) return;
    const cur=document.getElementById("extras");
    if(cur) cur.innerHTML=renderExtraCards(null,null,reason);
  }finally{
    clearTimeout(timer);
  }
}

function render(d){
  const t=d.totals;
  let h='';
  const deep=d.deep;
  const name = deep ? deep.property_name : (smartTitle(d.owner) || "Property");
  const subtitle = deep ? (deep.anchor_address+"  &bull;  "+deep.property_subtype)
                        : (d.anchor_address+"  &bull;  "+(d.neighborhood||"Springfield, MA"));
  // Any mode where the shown parcel is NOT an exact assessor-address match for what the
  // user typed gets the amber "verify" badge — "LIVE LOOKUP" is reserved for exact matches.
  // (street-unique: the number didn't match the assessor's range; geocoded: resolved via a
  // third-party location estimate. Both are plausible, neither is authoritative.)
  const approx = d.mode==="geocoded" || d.mode==="street-unique";
  const badge = deep ? '<span class="badge" style="background:var(--verified)">DEEP PROFILE</span>'
              : approx ? '<span class="badge" style="background:var(--est)">APPROXIMATE PARCEL &mdash; VERIFY</span>'
                     : '<span class="badge" style="background:var(--strong)">LIVE LOOKUP</span>';

  // header + KPIs
  h+='<div class="card"><div class="prophead"><div><h2>'+esc(name)+'</h2><div class="sub">'+subtitle+'</div></div>'+badge+'</div>';
  // wrong-ZIP warning — surfaced, never silently resolved
  if(d.zip_warning) h+='<div class="note" style="margin:2px 0 10px">&#9888; '+esc(d.zip_warning)+'</div>';
  h+='<div class="kpis">';
  h+=kpi(t.parcels,"Parcels");
  // condos / air-rights parcels carry no land — show "n/a", never a misleading "0"
  h+=kpi((t.land_acres&&t.land_acres>0)?t.land_acres:"n/a","Land Acres");
  if(deep){h+=kpi("336,205","Building SF");h+=kpi(deep.tenants.occupancy_rate_by_sqft+"%","Occupancy");}
  h+=kpi(money(t.assessed),"Assessed");
  h+='</div>';
  // when an OWNER search resolved to one of several holdings, say which (transparency)
  if(d.mode==="owner" && d.match_count>1)
    h+='<div class="muted" style="margin-top:4px">Owner search &mdash; showing the highest-assessed of <b>'+d.match_count+'</b> parcels indexed under this name. Enter a full street address to pinpoint a specific one.</div>';
  // the typed house number didn't match this parcel's assessor range, but it's the ONLY
  // parcel on that street, so there was nothing else it could be (see search() comment) —
  // say so rather than silently presenting it as an exact address match.
  if(d.mode==="street-unique")
    h+='<div class="muted" style="margin-top:4px">The assessor records this property as <b>'+esc(smartTitle(d.anchor_address))+'</b> &mdash; the only parcel on this street, though the number you entered doesn&rsquo;t match its assessor range (common for landmarks whose public address differs from how the parcel is recorded).</div>';
  // geocoded fallback: NEVER present this with the confidence of a direct assessor match —
  // it's a third-party (OpenStreetMap) location estimate run through a point-in-parcel test,
  // not a hit against our own address/owner index. "nearest" is explicitly weaker than
  // "exact" (the point didn't fall inside any parcel's actual boundary, just close to one).
  if(d.geocode){
    const gc=d.geocode;
    const howNote = gc.how==="exact"
      ? "the geocoded point falls inside this parcel&rsquo;s boundary"
      : "the geocoded point didn&rsquo;t fall inside any parcel boundary &mdash; this is the nearest one";
    h+='<div class="note" style="margin:2px 0 10px"><b>&#9873; Approximate match &mdash; verify before relying on it.</b> '
      +'&ldquo;'+esc(gc.query)+'&rdquo; was located via OpenStreetMap geocoding (resolved to &ldquo;'+esc(gc.display_name)+'&rdquo;), '
      +'not matched directly against the assessor&rsquo;s address or owner records; '+howNote+'.</div>';
  }
  h+=srcbox(deep?"MCAP internal deep-profile research (see Data Confidence below)":"Springfield Assessor &mdash; city ArcGIS parcel system");
  h+='</div>';

  // ---- Businesses operating here + Building Footprint (OSM) ----
  // These are slow, best-effort, third-party (Overpass) lookups — fetched in the
  // BACKGROUND after everything else renders, so they never delay or block the page.
  // See loadExtras() below.
  h+='<div id="extras"></div>';

  // ---- Disposition signals ("likelihood of selling") — headline read, renders from
  // synchronous data, then refines when deeds/research background-load. ----
  if(d.disposition) h+='<div class="card" id="disposition"></div>';

  // ---- Zoning (ordinance detail for the property's district) ----
  if(d.zoning) h+=renderZoning(d.zoning);

  // ---- LIVE enrichment (any non-deep property) ----
  const e=d.enrichment;
  if(!deep && e){
    // building / property detail
    const b=(e.buildings&&e.buildings[0])||{};
    const rd=e.room_detail;
    const dt=b.detail||{}; const sys=e.systems||{};
    h+='<div class="card"><h3 class="sec">Property Detail <span style="font-size:11px;color:var(--slate);font-weight:600">&bull; live from assessor record card</span></h3><div class="kv">';
    if(e.use_class) h+=kv("Use class",esc(e.use_class));
    if(e.zoning) h+=kv("Zoning",esc(e.zoning));
    if(e.total_building_sqft) h+=kv("Building area",e.total_building_sqft.toLocaleString()+" SF"+(e.building_count>1?" ("+e.building_count+" buildings)":""));
    if(b.structure_type) h+=kv("Structure / style",esc(b.structure_type)+(b.grade?(" &bull; grade "+esc(b.grade)):""));
    if(b.year_built) h+=kv("Year built",b.year_built+(dt.eff_year_built?(" (eff. "+esc(dt.eff_year_built)+")"):""));
    if(dt.stories) h+=kv("Stories",esc(dt.stories));
    if(rd){let r=[];if(rd.bedrooms)r.push(rd.bedrooms+" bd");if(rd.full_baths)r.push(rd.full_baths+" ba");if(rd.rooms)r.push(rd.rooms+" rooms");if(r.length)h+=kv("Rooms",r.join(" &bull; "));}
    if(dt.exterior_walls) h+=kv("Exterior",esc(dt.exterior_walls));
    if(dt.roof) h+=kv("Roof",esc(dt.roof));
    if(dt.construction_type) h+=kv("Construction",esc(dt.construction_type));
    let heat=[];if(dt.heat_type)heat.push(esc(dt.heat_type));if(dt.fuel_type)heat.push(esc(dt.fuel_type));if(dt.cooling&&dt.cooling!="NONE")heat.push("A/C: "+esc(dt.cooling));
    if(heat.length) h+=kv("Heating / cooling",heat.join(" &bull; "));
    if(dt.basement) h+=kv("Basement",esc(dt.basement));
    if(dt.condition) h+=kv("Condition",esc(dt.condition));
    // building systems (commercial)
    let syst=[];if(sys.sprinkler)syst.push("Sprinklered");if(sys.elevator_count)syst.push(sys.elevator_count+" elevator"+(sys.elevator_count>1?"s":""));if(sys.loading_docks)syst.push(sys.loading_docks+" loading dock"+(sys.loading_docks>1?"s":""));if(sys.overhead_doors)syst.push(sys.overhead_doors+" OH door"+(sys.overhead_doors>1?"s":""));
    if(syst.length) h+=kv("Building systems",syst.join(" &bull; "));
    if(e.assessment&&e.assessment.total){const a=e.assessment;h+=kv("Assessed value","Total "+money(a.total)+(a.land&&a.building?(" &nbsp;(land "+money(a.land)+" + bldg "+money(a.building)+")"):""));}
    if(e.value_flag) h+=kv("Valuation method",esc(e.value_flag));
    h+=srcbox("Springfield Assessor &mdash; live record card lookup");
    h+='</div></div>';

    // sale history
    if(e.sales&&e.sales.length){
      h+='<div class="card"><h3 class="sec">Sale History</h3><table><tr><th>Date</th><th class="num">Price</th><th>Buyer</th></tr>';
      e.sales.forEach(s=>{h+='<tr><td>'+s.date+'</td><td class="num">'+(s.price?money(s.price):"&mdash;")+'</td><td>'+esc(titlecase(s.grantee||""))+'</td></tr>';});
      h+='</table>'+srcbox("Springfield Assessor &mdash; record card sale history")+'</div>';
    }
    // permits
    if(e.permits&&e.permits.length){
      h+='<div class="card"><h3 class="sec">Permit Activity ('+e.permits.length+' recent)</h3><table><tr><th>Date</th><th>Permit #</th><th class="num">Value</th><th>Purpose</th></tr>';
      e.permits.forEach(p=>{h+='<tr><td>'+p.date+'</td><td>'+esc(p.number||"")+'</td><td class="num">'+(p.price?money(p.price):"&mdash;")+'</td><td>'+esc(p.purpose||"")+'</td></tr>';});
      h+='</table>'+srcbox("Springfield Building Department &mdash; record card permit history")+'</div>';
    }
  } else if(!deep){
    h+='<div class="card note">Parcel + owner + assemblage resolved live. Record-card detail couldn&rsquo;t be fetched right now (the assessor site may be rate-limiting) &mdash; try again in a moment.</div>';
  }

  // ownership (deep = rich SEC-confirmed; live = from the assessor owner record)
  if(deep){
    const o=deep.ownership, pc=o.parent_chain;
    h+='<div class="card"><h3 class="sec">Ownership</h3><div class="kv">';
    h+=kv("Owning entity",esc(o.current_owner.name)+" ("+o.current_owner.jurisdiction+")");
    h+=kv("Parent",esc(pc.parent.name)+' <span class="pill">NASDAQ: PECO</span>');
    h+=kv("Mailing",esc(o.current_owner.mailing_address));
    h+=kv("Manager",esc(o.property_manager));
    h+='</div><div class="muted">Confirmed via SEC Exhibit 21.1 (federal filing).</div></div>';
  } else {
    h+=renderOwnership(d);
  }

  // assemblage table
  const asN=t.parcels;
  h+='<div class="card"><h3 class="sec">Parcel Assemblage ('+asN+')</h3>';
  h+='<div class="muted" style="margin:-4px 0 12px">'+(asN>1
      ? asN+' <b>contiguous</b> parcels under one owner &mdash; grouped by adjacency, so this is the actual property.'
      : 'A single parcel &mdash; no other parcel owned by this entity is contiguous with it.')+'</div>';
  h+='<table><tr><th>APN</th><th>Address</th><th class="num">Land SF</th><th class="num">Assessed</th><th>Zone</th></tr>';
  d.assemblage.forEach(p=>{h+='<tr><td>'+p.apn+'</td><td>'+esc(titlecase(p.address))+'</td><td class="num">'+Math.round(p.land_sqft).toLocaleString()+'</td><td class="num">'+money(p.assessed)+'</td><td>'+esc(p.zone)+'</td></tr>';});
  h+='<tr class="tot"><td colspan="2">TOTAL &mdash; '+asN+' parcel'+(asN>1?'s':'')+'</td><td class="num">'+Math.round(t.land_sqft).toLocaleString()+'</td><td class="num">'+money(t.assessed)+'</td><td></td></tr></table>';
  if(d.owner_other_parcels>0){
    h+='<div class="note" style="margin-top:12px"><b>'+esc(titlecase(d.owner))+'</b> separately owns <b>'+d.owner_other_parcels+'</b> other parcel'+(d.owner_other_parcels>1?'s':'')+' elsewhere in Springfield ('+d.owner_total_parcels+' total). Those are <b>different properties</b> &mdash; not part of this assemblage &mdash; because they aren&rsquo;t contiguous with it.</div>';
  }
  h+=srcbox("Springfield Assessor parcel geometry, grouped by our own adjacency clustering");
  h+='</div>';

  // deep: transactions + tenants + confidence
  if(deep){
    const tx=deep.transaction_history[0];
    h+='<div class="card"><h3 class="sec">Transaction &amp; Financing</h3><div class="kv">';
    h+=kv("Last sale",money(tx.price)+" &bull; "+tx.date+" &bull; "+esc(tx.seller?("from "+tx.seller):""));
    h+=kv("Structure",esc(tx.structure||""));
    h+=kv("Mortgage",'<b style="color:var(--verified)">None recorded</b> &mdash; all-cash acquisition');
    h+=kv("Est. annual tax",money(deep.tax.estimated_annual_tax)+" (FY2026 commercial rate)");
    h+=srcbox("Registry of Deeds transaction record + Springfield FY2026 commercial tax rate (deep profile)");
    h+='</div></div>';

    const te=deep.tenants;
    h+='<div class="card"><h3 class="sec">Tenants &mdash; '+te.space_count+' spaces &bull; '+te.occupancy_rate_by_sqft+'% occupied</h3><table><tr><th>Tenant</th><th class="num">SF</th><th>Status</th><th>Public</th></tr>';
    te.roster.forEach(x=>{const pub=x.ticker||"";const col=x.status=="occupied"?"var(--verified)":"var(--est)";
      h+='<tr><td>'+esc(x.tenant_name)+'</td><td class="num">'+x.sqft.toLocaleString()+'</td><td style="color:'+col+';font-weight:700">'+titlecase(x.status)+'</td><td style="color:var(--slate);font-size:12px">'+esc(pub)+'</td></tr>';});
    h+='</table>'+srcbox("Manually researched leasing roster (deep profile, not live-refreshed)")+'</div>';

    const cs=deep.confidence_summary;
    h+='<div class="card"><h3 class="sec">Data Confidence</h3><div class="tiers">';
    h+=tier("VERIFIED","var(--verified)",cs.verified_tier);
    h+=tier("STRONG","var(--strong)",cs.strong_tier);
    h+=tier("ESTIMATED","var(--est)",cs.estimated_tier);
    h+=tier("UNRESOLVED","var(--unres)",cs.unresolved);
    h+='</div></div>';
  }

  // ---- Deep web research (address -> every website). Cached+instant for the deep
  // property; a click-to-run background job for any other address. Placed last so it
  // never delays the core profile. ----
  // ---- Recorded documents (Registry of Deeds), keyed on the OWNER name. Works for any
  // address; browser-driven + paced, so it's click-to-run in the background. ----
  h+='<div class="card" id="deeds" data-owner="'+escA(d.owner||"")+'"></div>';

  const raddr = deep ? deep.anchor_address : d.anchor_address;
  const rname = deep ? deep.property_name : (d.owner||"");
  h+='<div class="card" id="research" data-addr="'+escA(raddr)+'" data-name="'+escA(rname)+'" data-deep="'+(deep?1:0)+'"></div>';

  out.innerHTML=h;
  wrapTables(out);
  // Reset ALL cross-card state on every new property. The disposition card augments itself
  // from deeds + research; if these aren't cleared, the PREVIOUS property's mortgage/lien and
  // marketplace-listing flag leak into the new property's signals. __propKey stamps which
  // property is current so a late-arriving async deeds/research response for a prior search
  // is ignored (see renderDeeds/renderResearch + dispAugmentFactors).
  window.__propKey = (d.owner||"")+"|"+(d.anchor_address||"");
  window.__disp = d.disposition || null;
  window.__lastDeeds = null;
  window.__lastDeedsKey = null;
  window.__research = null;
  window.__researchKey = null;
  renderDisposition();
  initDeeds();
  initResearch();
  window.scrollTo({top:0,behavior:"smooth"});
}

// ---------- Ownership (live properties — from the assessor owner record) ----------
function ownerEntity(name){
  const n=(name||"").toUpperCase();
  // Government / public and institutional owners are checked FIRST so a hospital or authority
  // that happens to be incorporated ("...MEDICAL CENTER INC") isn't mislabeled a corporation
  // — keeping this card consistent with the Disposition card's owner-posture read.
  if(/\bCITY OF\b|\bTOWN OF\b|COMMONWEALTH|UNITED STATES|\bCOUNTY\b|AUTHORITY|\bAUTH\b|COMMISSION|DEPARTMENT|HOUSING AUTH/.test(n)) return ["Gov","Government / public"];
  if(/\bHOSPITALS?\b|\bMEDICAL\b|\bHEALTH\b|\bCLINIC\b|UNIVERSITY|\bCOLLEGE\b|\bACADEMY\b|\bSCHOOL\b|\bCHURCH\b|\bTEMPLE\b|SYNAGOGUE|\bPARISH\b|DIOCESE|MINISTR|FOUNDATION|NON.?PROFIT|ASSOCIATION|\bSOCIETY\b|\bMUSEUM\b|\bLIBRARY\b|INSURANCE|MUTUAL LIFE|MASS ?MUTUAL/.test(n)) return ["Institutional","Institutional owner"];
  if(/\bLLC\b|\bL L C\b/.test(n)) return ["LLC","Investment entity (LLC)"];
  if(/\bLP\b|\bLLP\b|LIMITED PARTNERSHIP/.test(n)) return ["LP","Investment entity (LP)"];
  if(/\bINC\b|CORP|COMPANY/.test(n)) return ["Corp","Corporation"];
  if(/\bTRUST\b|\bTR\b|TRUSTEE|ESTATE|\bEST\b/.test(n)) return ["Trust","Trust / estate"];
  return ["Individual","Individual owner"];
}
// title-case that preserves acronyms/codes/state abbreviations (LLC, CVS, RI, A-CSF-27),
// while properly casing names + street types (MARY LOU -> Mary Lou, ST -> St)
const _KEEPUP=new Set(("LLC LLP LP PLLC INC CORP CO LTD PC NA USA US DBA CVS TD JP UBS BNY HSBC "
  +"AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
  +"NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split(" "));
// decode HTML entities that slip in from the registry index ("SAGON (&amp;O)" -> "(&O)")
// so they don't render as literal, title-cased "&Amp;". Handles double-encoding too.
function deent(s){
  let x=String(s||"");
  for(let i=0;i<3&&/&(amp|#38|quot|#34|#39|apos|lt|gt);/i.test(x);i++)
    x=x.replace(/&amp;/gi,"&").replace(/&#38;/g,"&").replace(/&quot;/gi,'"').replace(/&#34;/g,'"')
       .replace(/&#39;/g,"'").replace(/&apos;/gi,"'").replace(/&lt;/gi,"<").replace(/&gt;/gi,">");
  return x;
}
function smartTitle(s){
  return (s||"").split(/\s+/).map(w=>{
    if(!w) return w;
    if(/\d/.test(w)) return w;                                  // codes: 2001, A-CSF-27, 02895
    const bare=w.replace(/[^A-Za-z]/g,"").toUpperCase();
    if(_KEEPUP.has(bare)) return w.toUpperCase();               // acronym / state code
    if(bare.length===1) return w.toUpperCase();                 // initial: E
    return w.charAt(0).toUpperCase()+w.slice(1).toLowerCase();
  }).join(" ");
}
function renderOwnership(d){
  const ent=ownerEntity(d.owner);
  let h='<div class="card"><h3 class="sec">Ownership <span style="font-size:11px;color:var(--slate);font-weight:600">&bull; from the Springfield assessor</span></h3><div class="kv">';
  h+=kv("Owner",esc(smartTitle(d.owner)));
  h+=kv("Owner type",esc(ent[1]));
  if(d.owner_mailing) h+=kv("Mailing address",esc(smartTitle(d.owner_mailing)));
  const total=d.owner_total_parcels||d.totals.parcels, other=d.owner_other_parcels||0, here=d.totals.parcels;
  let foot=here+' parcel'+(here>1?'s':'')+' at this property';
  if(other>0) foot+='; owner holds '+total+' in Springfield total ('+other+' elsewhere &mdash; different properties)';
  h+=kv("Springfield holdings",foot);
  h+='</div>';
  if(d.owner_mailing && !/\bMA\b|MASSACHUSETTS/i.test(d.owner_mailing) && /[A-Z]{2}\s*\d{5}/.test(d.owner_mailing.toUpperCase()))
    h+='<div class="muted">Mailing address is <b>out of state</b> &mdash; an absentee / corporate owner (the entity behind the LLC).</div>';
  h+=srcbox("Springfield Assessor owner-of-record");
  h+='</div>';
  return h;
}

// ---------- Disposition signals ("likelihood of selling") ----------
// Renders the server's synchronous factors immediately, then augments with debt-maturity,
// active-listing and lien signals once Recorded Documents / Web Research load. Never a
// probability — a transparent, source-tagged, leading-indicator read.
function dispAugmentFactors(){
  const extra=[];
  // debt maturity — from the loaded deeds summary. Guard: only use deeds that were loaded
  // for the CURRENTLY displayed property (a stale value from a prior search must not leak in).
  const dd=(window.__lastDeedsKey===window.__propKey)?window.__lastDeeds:null;
  if(dd&&dd.summary){
    const lm=dd.summary.latest_mortgage;
    if(!dd.summary.total){
      // ZERO documents under this name is NOT evidence of anything — the registry indexes
      // by name, and individual owners are frequently recorded under a different format.
      // Absence of evidence, never "verified all-cash". A title search is the real answer.
      extra.push({key:"debt",label:"Debt",finding:"no documents found under this search name — mortgage status unknown (the registry indexes by name, and the owner may be recorded differently); a title search is required to confirm",direction:"neutral",weight:0,source:"Registry of Deeds (name search)",confidence:"unresolved"});
    } else if(!dd.summary.counts.mortgages){
      // documents DO exist under this name but none is a mortgage — meaningful, but still
      // not proof of all-cash: a mortgage could sit under a co-owner/prior name variant.
      extra.push({key:"debt",label:"Debt",finding:"documents exist under this name but no mortgage among them — consistent with an all-cash hold, though not proof (a mortgage could be recorded under a name variant); title search to confirm",direction:"neutral",weight:0,source:"Registry of Deeds (name search)",confidence:"moderate"});
    } else if(lm&&lm.age_years!=null){
      const a=lm.age_years;
      if(a<=3) extra.push({key:"debt",label:"Debt maturity",finding:"financed ~"+a+" yr ago — recently committed, unlikely to sell short-term",direction:"lowers",weight:-1,source:"Registry of Deeds",confidence:"strong"});
      else if(a>=5&&a<=11) extra.push({key:"debt",label:"Debt maturity",finding:"mortgage ~"+a+" yrs old — at/near a typical 5–10 yr balloon into the 2025–26 maturity wall: refinance-or-sell pressure",direction:"raises",weight:2,source:"Registry of Deeds (term inferred)",confidence:"inferred"});
      else extra.push({key:"debt",label:"Debt maturity",finding:"most recent mortgage ~"+a+" yrs old — likely already resolved",direction:"neutral",weight:0,source:"Registry of Deeds",confidence:"strong"});
    }
    const ll=dd.summary.latest_lien;
    if(dd.summary.counts.liens&&ll&&ll.age_years!=null&&ll.age_years<=10)
      extra.push({key:"distress",label:"Distress",finding:"recent lien ("+(ll.type||"lien")+", "+ll.age_years+" yr old) — possible forced-sale pressure",direction:"raises",weight:1,source:"Registry of Deeds",confidence:"strong"});
    // market activity — this owner as SELLER (grantor) on a deed under this name, almost
    // always a DIFFERENT property (see hampden_browser.summarize). A recent one is direct
    // evidence they're actively transacting right now, not an inference from entity type.
    const ls=dd.summary.latest_sale_as_seller;
    if(ls&&ls.age_years!=null){
      if(ls.age_years<=3) extra.push({key:"activity",label:"Market activity",finding:"sold a property as seller ~"+ls.age_years+" yr ago (book/page "+(ls.book_page||"")+") — actively transacting in the market under this name",direction:"raises",weight:2,source:"Registry of Deeds",confidence:"strong"});
      else extra.push({key:"activity",label:"Market activity",finding:"most recent sale as seller under this name was ~"+ls.age_years+" yrs ago — no recent transaction activity",direction:"neutral",weight:0,source:"Registry of Deeds",confidence:"strong"});
    }
  }
  // active listing — from the loaded research sources (same current-property guard)
  const rs=(window.__researchKey===window.__propKey)?window.__research:null;
  if(rs&&rs.sources){
    const listers=/loopnet|crexi|cityfeet|showcase|commercialcafe|commercialsearch|catylist|brevitas|ten-x/i;
    const hit=rs.sources.find(s=>listers.test(s.domain||""));
    // NB: a bare marketplace URL does NOT confirm a SALE — LoopNet/Crexi list for-lease too.
    // So this is a flag to verify, weight 0 (it does not by itself move the band).
    if(hit) extra.push({key:"market",label:"Marketplace listing",finding:"a CRE marketplace page was found ("+hit.domain+") — the property may be actively marketed, for sale or for lease. Verify the listing directly.",direction:"flag",weight:0,source:"Web research (unverified)",confidence:"reported"});
  }
  return extra;
}
function renderDisposition(){
  const el=document.getElementById("disposition"); const d=window.__disp;
  if(!el||!d){ if(el) el.remove(); return; }
  const factors=(d.factors||[]).concat(dispAugmentFactors());
  // band reflects the LEADING INDICATORS only (hold / debt / posture / vintage / distress).
  // A marketplace listing is a weight-0 "flag" shown separately — it must be verified and
  // must NOT by itself assert the property is on the market.
  const score=factors.reduce((a,f)=>a+(f.weight||0),0);
  const listing=factors.find(f=>f.key==="market");
  let cls,label;
  if(score>=3){cls="elev";label="Elevated";}
  else if(score>=1){cls="mod";label="Moderate";}
  else {cls="low";label="Low";}
  const drivers=factors.filter(f=>f.direction==="raises").map(f=>f.label.toLowerCase());
  let h='<div class="disphead"><h3 class="sec" style="margin:0">Disposition Signals '
    +'<span style="font-size:11px;color:var(--slate);font-weight:600">&bull; likelihood this owner sells</span></h3>'
    +'<span class="dispband '+cls+'">'+label+'</span></div>';
  h+='<div class="dispsum">'
    +(drivers.length?('Signal is <b>'+label.toLowerCase()+'</b>, driven by '+drivers.slice(0,3).join(', ')+'.')
      :'Signal is <b>'+label.toLowerCase()+'</b>.')
    +'</div>';
  // prominent, hedged listing flag (verify) — the single most actionable item when present
  if(listing) h+='<div class="note" style="margin:2px 0 10px"><b>&#9873; '+esc(listing.finding)+'</b></div>';
  const arw=d=>d==="raises"?['up','&uarr;']:d==="lowers"?['dn','&darr;']:d==="flag"?['nt','&#9873;']:['nt','&ndash;'];
  factors.filter(f=>f.key!=="market").forEach(f=>{const a=arw(f.direction);
    h+='<div class="dfac"><div class="arw '+a[0]+'">'+a[1]+'</div><div><div class="fl">'+esc(f.label)+'</div>'
      +'<div class="ff">'+esc(f.finding)+'</div>'
      +'<div class="fsrc">'+esc(f.source||"")+(f.confidence?(' &bull; '+esc(f.confidence)):'')+'</div></div></div>';
  });
  // note any pending signals not yet loaded
  const have=new Set(factors.map(f=>f.key));
  const pend=[]; if(!have.has("debt")&&!have.has("market")){pend.push("run Recorded Documents & Web Research");}
  else{ if(!have.has("market")) pend.push("run Deep Web Research for an active-listing check"); if(!have.has("debt")) pend.push("run Recorded Documents for the debt signal"); }
  if(pend.length) h+='<div class="dpend">More signals available &mdash; '+pend.join('; ')+'.</div>';
  h+='<div class="cite" style="margin-top:12px"><b>How to read this:</b> a transparent signal from public records, <b>not a probability or investment advice</b>. It weights <b>leading</b> indicators (hold period, debt maturity, owner posture) over <b>lagging</b> ones (vacancy, price cuts). Each factor shows its source and confidence.</div>';
  el.innerHTML=h;
}

// Split a compound assessor owner string into individual legal-entity names. See the
// caller (initDeeds) for why a naive split on every "&" is wrong.
function splitOwnerEntities(owner){
  const SUFFIX=/\b(LLC|INC|CORP|CO|LP|LTD|TRUST|TRUSTEE)\b/i;
  const tokens=(owner||"").split(/(\s+AND\s+|\s*&\s*)/i);
  const parts=[]; let buf="";
  tokens.forEach(t=>{
    if(/^\s+AND\s+$/i.test(t)){ if(buf.trim())parts.push(buf.trim()); buf=""; return; }
    if(/^\s*&\s*$/.test(t)){
      if(SUFFIX.test(buf)){ if(buf.trim())parts.push(buf.trim()); buf=""; }
      else buf+=" & ";
      return;
    }
    buf+=t;
  });
  if(buf.trim()) parts.push(buf.trim());
  return parts.filter(Boolean);
}

// ---------- Recorded documents (Registry of Deeds) ----------
function initDeeds(){
  const el=document.getElementById("deeds"); if(!el) return;
  const gen=GEN;
  const owner=el.dataset.owner;
  if(!owner){el.remove();return;}
  // The registry indexes by INDIVIDUAL party name. A compound assessor owner
  // ("A LLC & B LLC & C LLC") returns nothing if searched whole, so split it and search the
  // PRIMARY entity — and say so, rather than silently returning an empty (misleading) result.
  // NB: "&" is ambiguous — it can separate co-owners ("A LLC & B LLC") OR sit INSIDE one
  // owner's own legal name ("W & M Realty Inc"). Splitting on every "&" blindly turned
  // "W & M REALTY INC AND WIENER LOUIS TRUSTEE" into "W" + "M REALTY INC" + "WIENER LOUIS
  // TRUSTEE" — searching the registry for the single letter "W". Only accept an "&" as a
  // co-owner boundary when the text collected so far ALREADY contains a legal-entity suffix
  // (LLC/INC/CORP/etc) — i.e. it closed out a complete entity name already. " AND " (a full
  // word, never embedded in a business name) is always a safe separator.
  const parts=splitOwnerEntities(owner);
  const searchName=parts[0]||owner;
  const compound=parts.length>1;
  const compoundNote=compound
    ? '<div class="muted" style="margin:0 0 10px">This parcel is co-owned by <b>'+parts.length+'</b> entities. The registry indexes by individual name, so this searches the primary entity, <b>'+esc(smartTitle(searchName))+'</b>. Co-owners: '+esc(smartTitle(parts.slice(1).join(", ")))+'.</div>'
    : '';
  el.dataset.searchOwner=searchName;
  el.innerHTML='<h3 class="sec">Recorded Documents <span style="font-size:11px;color:var(--slate);font-weight:600">&bull; Hampden County Registry of Deeds</span></h3>'
    +'<div class="muted" style="margin:0 0 12px">Every deed, mortgage, discharge, lien, easement and lease recorded under <b>'+esc(smartTitle(searchName))+'</b> '
    +'&mdash; the debt &amp; encumbrance picture the assessor never publishes.</div>'
    +compoundNote
    +'<button class="rbtn" id="dgo">Look up recorded documents</button>'
    +'<span class="muted" id="dnote" style="margin-left:12px">~30s &bull; the registry is bot-protected, so this runs a real browser, paced</span>'
    +'<div id="dbody"></div>';
  document.getElementById("dgo").onclick=()=>runDeeds(searchName,GEN);
  // PEEK only — if a result is already cached server-side, show it; otherwise do NOTHING
  // (never auto-start a ~30s registry browser job just because a property was rendered).
  fetch("/api/deeds?peek=1&owner="+encodeURIComponent(searchName)).then(r=>r.json()).then(d=>{
    if(stale(gen)) return;                     // belongs to a previous address
    if(d.status==="done"&&d.cached) renderDeeds(d.result,true);
  }).catch(()=>{});
}

async function runDeeds(owner, gen){
  const btn=document.getElementById("dgo"), body=document.getElementById("dbody"), note=document.getElementById("dnote");
  if(btn) btn.disabled=true; if(note) note.innerHTML="";
  body.innerHTML=loadingHTML("Searching the registry &hellip; solving its bot-check in a real browser");
  let d=await (await fetch("/api/deeds?owner="+encodeURIComponent(owner))).json();
  if(stale(gen)) return;
  if(d.status==="done"){renderDeeds(d.result,d.cached);if(btn)btn.disabled=false;return;}
  if(d.status!=="running"){body.innerHTML='<div class="note">Could not start: '+esc(d.error||"unknown")+'</div>';if(btn)btn.disabled=false;return;}
  const job=d.job;
  while(true){
    await new Promise(s=>setTimeout(s,2000));
    if(stale(gen)) return;                     // user searched a new address — stop polling
    const sd=await (await fetch("/api/deeds/status?job="+job)).json();
    if(stale(gen)) return;
    if(sd.status==="done"){renderDeeds(sd.result,false);if(btn)btn.disabled=false;return;}
    if(sd.status==="error"){
      body.innerHTML='<div class="note"><b>Registry unavailable.</b> '+esc(sd.error||"")
        +'<br>This is a block, <b>not</b> a finding &mdash; it does not mean there are no recorded documents.</div>';
      if(btn)btn.disabled=false;return;
    }
    const el=document.getElementById("dbody");
    if(el&&el.querySelector(".loading")) el.querySelector(".loading").lastChild.textContent=
      " Searching the registry … "+(sd.elapsed||0)+"s";
  }
}

function dtypeClass(t){
  const u=(t||"").toUpperCase();
  if(u.includes("MORTGAGE")) return "mtg";
  if(u.includes("DEED")) return "deed";
  if(u.includes("LIEN")||u.includes("ATTACHMENT")||u.includes("EXECUTION")) return "lien";
  return "";
}

function renderDeeds(doc,cached){
  const body=document.getElementById("dbody"); if(!body) return;
  const btn=document.getElementById("dgo"), note=document.getElementById("dnote");
  if(btn) btn.style.display="none"; if(note) note.innerHTML="";
  const s=doc.summary, c=s.counts;
  // Recency is the honest signal, not "mortgages minus discharges": a discharge is usually
  // recorded under the LENDER's name, so its absence here is not evidence of open debt.
  const lm=s.latest_mortgage;
  // age of the most recent mortgage. Commercial loans typically run 5-10yr terms (balloon),
  // amortizing over 20-25yr at most, so age is the honest signal of whether it could still
  // be live. Only a genuinely recent one (<=8yr, within a typical term) warrants attention.
  let lmAge=(lm&&lm.age_years!=null)?lm.age_years:(lm&&lm.date?(new Date().getFullYear()-parseInt(String(lm.date).slice(-4))):null);
  const attention=(lmAge!=null&&lmAge<=8);
  let h='<div class="dsum">';
  h+='<div class="dpill"><b>'+s.total+'</b>documents</div>';
  if(c.deeds) h+='<div class="dpill"><b>'+c.deeds+'</b>deeds</div>';
  h+='<div class="dpill'+(c.mortgages?(attention?" hot":""):" clear")+'"><b>'+c.mortgages+'</b>mortgage'+(c.mortgages==1?"":"s")+'</div>';
  if(c.discharges) h+='<div class="dpill"><b>'+c.discharges+'</b>discharges</div>';
  // a lien is only a live concern if RECENT — an old (esp. >10yr) lien is very likely
  // released/expired, so don't red-flag it as active.
  const ll=s.latest_lien; const lienAge=ll&&ll.age_years!=null?ll.age_years:null;
  const lienHot=(lienAge!=null&&lienAge<=10);
  if(c.liens) h+='<div class="dpill'+(lienHot?" hot":"")+'"><b>'+c.liens+'</b>lien'+(c.liens==1?"":"s")+'</div>';
  if(c.leases) h+='<div class="dpill"><b>'+c.leases+'</b>lease'+(c.leases==1?"":"s")+'</div>';
  if(c.easements) h+='<div class="dpill"><b>'+c.easements+'</b>easement'+(c.easements==1?"":"s")+'</div>';
  h+='</div>';
  h+='<div class="muted" style="margin:6px 0 12px">';
  if(!s.total){
    // ZERO documents = the name search found nothing at all — status UNKNOWN, never a
    // green "no mortgage" claim (the owner may simply be indexed under a different name).
    h+='<b style="color:var(--unres)">No documents found under this search name &mdash; mortgage status unknown.</b> A title search is required to confirm; this is not evidence of a clean title or an all-cash purchase.';
  } else if(!c.mortgages){
    h+='<b>No mortgage among the documents recorded under this name</b> &mdash; consistent with an all-cash hold, though a mortgage could still sit under a name variant.';
  } else {
    // age-tiered, honest read — never call a decades-old loan "recent"
    let tier;
    if(lmAge==null) tier='';
    else if(lmAge<=8) tier=' &mdash; <b style="color:#B3261E">within a typical commercial loan term ('+lmAge+' yr'+(lmAge==1?"":"s")+' old)</b>, so it could still be the current financing.';
    else if(lmAge<=15) tier=' &mdash; <b>'+lmAge+' years old</b>, past a typical 5&ndash;10 yr commercial term, so likely refinanced or paid off.';
    else if(lmAge<=25) tier=' &mdash; <b>'+lmAge+' years old</b>, very likely long satisfied.';
    else tier=' &mdash; <b>'+lmAge+' years old</b>, older than any typical loan &mdash; almost certainly satisfied.';
    h+='<b style="color:'+(attention?"#B3261E":"var(--slate)")+'">Most recent mortgage: '+esc(lm.date||"")
      +(lm.lender?(' to '+esc(smartTitle(deent(lm.lender)))):'')+'</b>'+tier;
    if(s.mortgage_dates&&s.mortgage_dates.length>1)
      h+=' All recorded: '+s.mortgage_dates.map(esc).join(', ')+'.';
  }
  h+=' <span style="color:var(--slate)">A payoff can&rsquo;t be confirmed from a name search &mdash; discharges are usually recorded under the <b>lender&rsquo;s</b> name, so their absence here is <b>not</b> proof a loan is outstanding.</span></div>';

  // lien read — age-aware, so a decades-old (self-released) lien isn't shown as a live risk
  if(c.liens&&ll){
    h+='<div class="muted" style="margin:-4px 0 12px">';
    if(lienAge==null) h+='<b>'+c.liens+' lien'+(c.liens==1?"":"s")+'</b> recorded ('+esc(titlecase(ll.type||"lien"))+').';
    else if(lienHot) h+='<b style="color:#B3261E">Recent lien: '+esc(titlecase(ll.type||"lien"))+', '+esc(ll.date||"")+' ('+lienAge+' yr'+(lienAge==1?"":"s")+' old)</b> &mdash; worth verifying it&rsquo;s been resolved.';
    else h+='Newest lien ('+esc(titlecase(ll.type||"lien"))+') is <b>'+lienAge+' years old</b> &mdash; liens of this age are typically released or expired (federal tax liens self-release after 10 years), so very likely resolved.';
    h+='</div>';
  }

  // market activity — this owner as SELLER (grantor) on a deed under this name. Since the
  // registry indexes by name across the whole county, a grantor deed here is almost always a
  // DIFFERENT property than the one being viewed (this owner would show as grantee on THIS
  // property's own acquisition deed, not grantor) — direct evidence of active selling.
  const ls=s.latest_sale_as_seller, saleAge=ls&&ls.age_years!=null?ls.age_years:null;
  if(s.sales_as_seller_count&&ls){
    h+='<div class="muted" style="margin:-4px 0 12px">';
    if(saleAge!=null&&saleAge<=3) h+='<b style="color:#B3261E">Sold a property as seller '+esc(ls.date||"")+' ('+saleAge+' yr'+(saleAge==1?"":"s")+' ago)</b> to '+esc(smartTitle(deent(ls.buyer||"")))+' &mdash; recorded under this name, so almost certainly a different property from this one. Recent activity like this is a real signal this owner is actively selling.';
    else h+='Most recent sale as seller under this name was <b>'+(saleAge!=null?saleAge+' years ago':esc(ls.date||""))+'</b> ('+esc(ls.date||"")+') &mdash; no recent transaction activity.';
    h+='</div>';
  }

  if(doc.records&&doc.records.length){
    h+='<table><tr><th>Recorded</th><th>Document</th><th>Book/Page</th><th>Counterparty</th></tr>';
    // newest first — parse MM-DD-YYYY into a sortable YYYYMMDD key; undated rows sink last
    const dkey=s=>{const m=/(\d{2})-(\d{2})-(\d{4})/.exec(s||"");return m?(+m[3])*10000+(+m[1])*100+(+m[2]):-1;};
    const rows=doc.records.slice().sort((a,b)=>dkey(b.date_received)-dkey(a.date_received));
    rows.forEach(r=>{
      const role=r.party_role==="grantee"?"&larr;":"&rarr;";
      // NB: keep the em-dash entity OUT of esc()/smartTitle() — they'd mangle it to "&Mdash;".
      // deent() first so a raw "&amp;" doesn't render as literal "&Amp;".
      const party=r.reverse_party ? esc(smartTitle(deent(r.reverse_party))) : "&mdash;";
      h+='<tr><td>'+esc(r.date_received||"")+'</td>'
        +'<td><span class="dtype '+dtypeClass(r.document_type)+'">'+esc(r.document_type||"")+'</span></td>'
        +'<td>'+esc(r.book_page||"")+'</td>'
        +'<td>'+(r.reverse_party?role+' ':'')+party+'</td></tr>';
    });
    h+='</table>';
  } else {
    h+='<div class="note">No documents indexed under this exact name. The registry indexes by <b>name, not address</b> &mdash; individual owners are often recorded under a different name format, so this is not proof of a clean title.</div>';
  }
  h+='<div class="cite"><b>Source:</b> Hampden County Registry of Deeds &mdash; public name index'
    +(cached?" (cached)":"")+', retrieved '+esc(doc.fetched_at||"")+'. '
    +'Indexed by <b>owner name</b>, so these are documents recorded under &ldquo;'+esc(doc.owner)+'&rdquo; in Hampden County &mdash; for a single-property LLC that is this property’s record; for an individual owner it may span other properties.</div>';
  body.innerHTML=h;
  wrapTables(body);
  window.__lastDeeds=doc; window.__lastDeedsKey=window.__propKey;
  renderDisposition();   // refine the disposition read with debt/lien
}

// ---------- Zoning ----------
function dimLabel(k){const M={min_lot_sf:"Min lot",min_lot_acres:"Min lot",min_lot_sf_sf_dwelling:"Min lot / SF home",
  min_lot_sf_per_apt_unit:"Min lot / apt unit",min_frontage_ft:"Min frontage",front_yard_min_ft:"Front yard",
  side_yard_abut_residential_ft:"Side (abuts res.)",side_yard_abut_nonresidential_ft:"Side (non-res.)",side_yard_min_ft:"Side yard",
  rear_yard_abut_residential_ft:"Rear (abuts res.)",rear_yard_abut_nonresidential_ft:"Rear (non-res.)",rear_yard_min_ft:"Rear yard",
  max_stories:"Max stories",max_height_ft:"Max height",max_building_coverage_pct:"Max coverage",
  max_building_coverage_pct_residential:"Max cov. (res.)",max_residential_density_du_per_acre:"Max density"};return M[k]||k;}
function dimUnit(k,v){if(k.endsWith("_pct")||k.endsWith("_pct_residential"))return v+"%";
  if(k.endsWith("_ft"))return v+"'";if(k.endsWith("_acres"))return v+" ac";if(k.endsWith("_du_per_acre"))return v+" du/ac";
  if(k.startsWith("min_lot_sf"))return Number(v).toLocaleString()+" sf";if(k==="max_stories")return v;return v;}
function renderZoning(z){
  let h='<div class="card"><h3 class="sec">Zoning &mdash; '+esc(z.district_name)+'<span class="zbadge">'+esc(z.raw_zone||z.district_key)+'</span>'
    +'<span style="font-size:11px;color:var(--slate);font-weight:600"> &bull; from the Springfield Zoning Ordinance</span></h3>';
  h+='<div class="muted" style="margin-top:0">'+esc(z.purpose)+'</div>';
  if(z.split_note) h+='<div class="note">'+esc(z.split_note)+'</div>';
  const dim=z.dimensional||{};const keys=Object.keys(dim);
  if(keys.length){h+='<div class="kpis" style="margin-top:14px">';
    keys.forEach(k=>{h+=kpi(dimUnit(k,dim[k]),dimLabel(k));});h+='</div>';}
  const u=z.uses||{};
  const grp=(label,cls,items,showtier)=>{if(!items||!items.length)return"";
    let s='<div class="zgrp"><div class="lbl" style="color:'+(cls=="p"?"var(--verified)":cls=="s"?"#8a5d0f":"var(--slate)")+'">'+label+' ('+items.length+')</div><div class="uchips">';
    items.forEach(it=>{s+='<span class="uchip '+cls+'">'+esc(it.use)+(showtier&&it.code&&it.code!="T"?'<span class="t">'+esc(it.code)+'</span>':"")+'</span>';});
    return s+'</div></div>';};
  h+=grp("Permitted by right","p",u.permitted,false);
  h+=grp("By special permit / site plan review","s",u.special_permit,true);
  h+=grp("Not allowed","n",u.prohibited,false);
  h+='<div class="cite"><b>Source:</b> '+esc(z.source)+', '+esc(z.ordinance_current_as_of)
    +'. Tiers: 1 = site-plan review, 2/3 = special permit (by scale). '
    +'<a class="src" href="'+esc(z.source_url)+'" target="_blank" rel="noopener">View ordinance &rarr;</a></div>';
  return h+'</div>';
}

// ---------- Deep web research UI ----------
function initResearch(){
  const el=document.getElementById("research"); if(!el) return;
  const gen=GEN;
  const addr=el.dataset.addr, name=el.dataset.name, deep=el.dataset.deep==="1";
  el.innerHTML='<h3 class="sec">Deep Web Research <span style="font-size:11px;color:var(--slate);font-weight:600">&bull; every website that mentions this property</span></h3>'
    +'<div class="muted" style="margin:0 0 12px">Runs ~30&ndash;70 keyword searches for this address across the DealSynq data-source taxonomy '
    +'(sale &amp; listing, tenants, ownership, permits, zoning, environmental, legal, tax, news&hellip;) through DuckDuckGo &amp; Mojeek '
    +'&mdash; not Google, which blocks scrapers &mdash; then dedupes them into one ranked list of sources.</div>'
    +'<button class="rbtn" id="rgo">'+(deep?"Load research (cached)":"Run web research")+'</button>'
    +'<span class="muted" id="rnote" style="margin-left:12px">'+(deep?"instant &bull; pre-built for this flagship property":"up to ~90s live &bull; paced to avoid rate-limits; returns partial results if it runs long")+'</span>'
    +'<div id="rbody"></div>';
  document.getElementById("rgo").onclick=()=>runResearch(addr,name,GEN);
  if(deep) runResearch(addr,name,gen);   // flagship: auto-load the cached result
}

async function runResearch(addr,name,gen){
  const btn=document.getElementById("rgo"), body=document.getElementById("rbody"), note=document.getElementById("rnote");
  if(btn){btn.disabled=true;} if(note) note.innerHTML="";
  body.innerHTML='<div class="prog"><i id="rbar"></i></div><div class="muted" id="rstat">Starting&hellip;</div>';
  let r=await fetch("/api/research?q="+encodeURIComponent(addr)+"&name="+encodeURIComponent(name||""));
  let d=await r.json();
  if(stale(gen)) return;
  if(d.status==="done"){renderResearch(d.result,d.cached);if(btn)btn.disabled=false;return;}
  if(d.status!=="running"){body.innerHTML='<div class="note">Could not start research: '+esc(d.error||"unknown")+'</div>';if(btn)btn.disabled=false;return;}
  // poll
  const job=d.job;
  while(true){
    await new Promise(s=>setTimeout(s,1600));
    if(stale(gen)) return;                     // user searched a new address — stop polling
    const sr=await fetch("/api/research/status?job="+job); const sd=await sr.json();
    if(stale(gen)) return;
    const bar=document.getElementById("rbar"), stat=document.getElementById("rstat");
    if(sd.total&&bar) bar.style.width=Math.round(100*sd.done/sd.total)+"%";
    if(stat) stat.innerHTML="Searching &hellip; "+sd.done+" / "+sd.total+" queries"+(sd.last_query?(' <span class="rtag">'+esc(sd.last_query)+'</span>'):"");
    if(sd.status==="done"){renderResearch(sd.result,false);if(btn)btn.disabled=false;return;}
    if(sd.status==="error"){body.innerHTML='<div class="note">Research failed: '+esc(sd.error||"")+'</div>';if(btn)btn.disabled=false;return;}
  }
}

function renderResearch(doc,cached){
  const body=document.getElementById("rbody"); if(!body) return;
  const eng=Object.entries(doc.engines_used||{}).map(([k,v])=>k+" "+v).join(" &bull; ");
  let h='<div class="kpis" style="margin:14px 0 4px">';
  // honest count: queries actually RUN, not the total planned (they can differ when the
  // crawl stops early on its time budget)
  const qrun=(doc.queries_run!=null?doc.queries_run:doc.query_count);
  h+=kpi(qrun+(doc.query_count&&qrun<doc.query_count?(" / "+doc.query_count):""),"Queries Run");
  h+=kpi(doc.unique_url_count,"Unique Sites");
  h+=kpi(doc.unique_domain_count,"Domains");
  h+=kpi(doc.elapsed_seconds+"s",cached?"Run Time (cached)":"Run Time");
  h+='</div>';
  if(doc.partial) h+='<div class="note" style="margin:2px 0 8px">&#9888; <b>Partial results</b> &mdash; the sweep hit its time budget and stopped early ('+qrun+' of '+doc.query_count+' queries). What&rsquo;s shown is real; it&rsquo;s just not the complete sweep.</div>';
  // category filter chips — real <button>s (keyboard-focusable, Enter/Space activate,
  // aria-pressed announces the active filter to screen readers). role=group + label names
  // the set. Previously non-interactive <span>s: mouse-only, invisible to assistive tech.
  const cats=Object.keys(doc.categories||{});
  h+='<div class="rchips" id="rchips" role="group" aria-label="Filter research sources by category">';
  h+='<button type="button" class="rchip on" data-cat="__all" aria-pressed="true">All <b>'+doc.unique_url_count+'</b></button>';
  cats.forEach(c=>{h+='<button type="button" class="rchip" data-cat="'+esc(c)+'" aria-pressed="false">'+esc(c)+' <b>'+doc.categories[c].length+'</b></button>';});
  h+='</div>';
  // source list (all, ranked) — filtered client-side by chip
  h+='<div id="rlist"></div>';
  h+='<div class="cite"><b>Source:</b> '+esc(doc.engine)+(eng?(" ("+eng+")"):"")+'. '
    +doc.query_count+' automated keyword searches, run '+esc((doc.generated_at||"").slice(0,10))+'. '
    +'This is a first-pass automated web sweep &mdash; a discovery list of where the data lives, not yet verified or extracted.</div>';
  body.innerHTML=h;
  window.__research=doc; window.__researchKey=window.__propKey;
  renderDisposition();   // refine the disposition read with an active-listing check
  drawSources("__all");
  document.querySelectorAll("#rchips .rchip").forEach(c=>c.onclick=()=>{
    document.querySelectorAll("#rchips .rchip").forEach(x=>{x.classList.remove("on");x.setAttribute("aria-pressed","false");});
    c.classList.add("on"); c.setAttribute("aria-pressed","true"); drawSources(c.dataset.cat);
  });
}

function drawSources(cat){
  const doc=window.__research, list=document.getElementById("rlist"); if(!doc||!list) return;
  let items=doc.sources;
  if(cat!=="__all") items=items.filter(s=>s.categories.indexOf(cat)>=0);
  items=items.slice(0,60);
  let h='';
  items.forEach(s=>{
    h+='<div class="rsrc"><a href="'+esc(s.url)+'" target="_blank" rel="noopener">'+esc(s.title||s.domain)+'</a>';
    h+='<span class="rtag">'+esc(s.domain)+'</span>';
    if(s.hits>1) h+='<span class="rtag">'+s.hits+' queries</span>';
    // show up to 3 category tags, but always surface the active-filter category
    let tags=s.categories.slice();
    if(cat!=="__all"){tags=[cat].concat(tags.filter(c=>c!==cat));}
    tags.slice(0,3).forEach(c=>{h+='<span class="rtag">'+esc(c)+'</span>';});
    if(s.snippet) h+='<div class="snip">'+esc(s.snippet.slice(0,200))+'</div>';
    h+='</div>';
  });
  if(!items.length) h='<div class="muted" style="padding:14px 0">No sites in this category.</div>';
  list.innerHTML=h;
}
function kpi(n,l){return '<div class="kpi"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>'}
function kv(k,v){return '<div class="k">'+k+'</div><div>'+v+'</div>'}
// small uniform "Source: ..." line appended to every card — text is always a static
// string we author (never raw user/API data), so it's written directly, no esc() needed.
function srcbox(text){return '<div class="srcbox">Source &bull; <b>'+text+'</b></div>'}
function tier(name,col,items){if(!items||!items.length)return"";
  return '<div class="row"><div class="tlabel" style="background:'+col+'">'+name+'</div><div>'+items.map(esc).join(" &bull; ")+'</div></div>';}
function titlecase(s){return (s||"").toLowerCase().replace(/\b\w/g,c=>c.toUpperCase());}

$("#go").onclick=run;
$("#q").addEventListener("keydown",e=>{if(e.key=="Enter")run();});
document.querySelectorAll(".ex").forEach(b=>b.onclick=()=>{$("#q").value=b.dataset.q;run();});
</script></body></html>"""


if __name__ == "__main__":
    load()
    where = "localhost" if HOST in ("127.0.0.1", "localhost") else HOST
    print(f"\n  DealSynq Property Intelligence running at  http://{where}:{PORT}/")
    print("  Try: 380 Cooley St\n")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
