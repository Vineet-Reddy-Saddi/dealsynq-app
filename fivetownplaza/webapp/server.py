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
import json
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
from springfield.footprint import site_metrics  # noqa: E402  OSM footprint + aerial estimates
from research.keyword_crawler import crawl as research_crawl, generate_queries, load_proxies  # noqa: E402
from springfield.zoning import lookup as zoning_lookup  # noqa: E402  ordinance detail per zoning code
from deeds.hampden_browser import (fetch_records as deeds_fetch,  # noqa: E402
                                   summarize as deeds_summarize)  # registry of deeds (browser)
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

_COORD = re.compile(r"-?\d{1,3}\.\d+")


def _centroid(geojson):
    """Cheap approximate centroid: average of all lon/lat vertex pairs (good enough for
    a ~70m POI radius query). Returns (lat, lon) or None."""
    nums = _COORD.findall(geojson or "")
    if len(nums) < 2:
        return None
    lons = [float(nums[i]) for i in range(0, len(nums) - 1, 2)]
    lats = [float(nums[i]) for i in range(1, len(nums), 2)]
    if not lons or not lats:
        return None
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def _num(s):
    try:
        return float(str(s).replace(",", "").strip() or 0)
    except Exception:
        return 0.0


# Canonicalize street-type words so "Street"/"St."/"ST" all match, etc.
SUFFIX = {
    "STREET": "ST", "STR": "ST", "AVENUE": "AVE", "AV": "AVE", "ROAD": "RD",
    "DRIVE": "DR", "DRV": "DR", "LANE": "LN", "COURT": "CT", "PLACE": "PL",
    "BOULEVARD": "BLVD", "BLVD": "BLVD", "CIRCLE": "CIR", "TERRACE": "TER", "TERR": "TER",
    "HIGHWAY": "HWY", "PARKWAY": "PKWY", "SQUARE": "SQ", "TRAIL": "TRL",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}
_STRIP = re.compile(r"\b(SPRINGFIELD|MASSACHUSETTS|MASS|MA|USA)\b")
# unit / suite / apt / floor designators — strip these and whatever follows them
_UNIT = re.compile(r"\b(UNIT|STE|SUITE|APT|APARTMENT|FL|FLR|FLOOR|BLDG|BUILDING|RM|ROOM|LOT|NO)\b\.?\s*[A-Z0-9-]*")


def normalize_addr(s):
    """Normalize an address so typos of format all collapse to one canonical string:
    uppercase, drop punctuation, strip city/state/zip AND unit/suite, canonical suffixes."""
    s = (s or "").upper()
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(r"#\s*[A-Z0-9-]+", " ", s)   # "#820"
    s = _UNIT.sub(" ", s)                    # "UNIT 820", "STE 100", ...
    s = _STRIP.sub(" ", s)
    toks = [t for t in s.split() if not re.fullmatch(r"\d{5}(-\d{4})?", t)]  # drop zip
    toks = [SUFFIX.get(t, t) for t in toks]
    return " ".join(toks).strip()


def street_of(norm):
    """Return the street part of a normalized address (drop the leading house number)."""
    toks = norm.split()
    return " ".join(toks[1:]) if toks and re.match(r"^\d", toks[0]) else norm


def load():
    print("Loading Springfield assessor data ...")
    with open(CSV_PATH, encoding="utf-8", errors="ignore") as f:
        for row in csv.DictReader(f):
            addr = (row["assessor_Parcel_Address"] or "").strip()
            cen = _centroid(row.get("geometry_geojson", ""))
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
                "lat": cen[0] if cen else None,
                "lon": cen[1] if cen else None,
            }
            PARCELS.append(rec)
            BY_OWNER.setdefault(rec["owner"].upper(), []).append(rec)
    print(f"  {len(PARCELS):,} parcels, {len(BY_OWNER):,} distinct owners loaded.")


PROFILE = {}
if os.path.exists(PROFILE_PATH):
    PROFILE = json.load(open(PROFILE_PATH, encoding="utf-8"))
DEEP_OWNER = (PROFILE.get("ownership", {}).get("current_owner", {}).get("name", "") or "").upper()


# ---- Search --------------------------------------------------------------
def search(q):
    raw = (q or "").strip()
    if not raw:
        return {"matched": False, "error": "empty query"}
    nq = normalize_addr(raw)
    mode = "address"

    # Score each parcel against the normalized query:
    #   3 = exact address, 2 = address starts with query, 1 = query is a substring.
    scored = []
    if nq:
        for p in PARCELS:
            na = p["norm"]
            if not na:
                continue
            if na == nq:
                scored.append((3, p))
            elif na.startswith(nq + " ") or na.startswith(nq):
                scored.append((2, p))
            elif nq in na:
                scored.append((1, p))

    if scored:
        best = max(s for s, _ in scored)
        hits = [p for s, p in scored if s == best]  # keep only the best tier
    else:
        # fall back to owner-name search
        uq = raw.upper()
        hits = [p for p in PARCELS if uq in p["owner"].upper()]
        mode = "owner"

    if not hits:
        # No parcel at this address (often a secondary/entrance address). Offer nearby
        # addresses on the SAME street so the user can pick the real parcel.
        st = street_of(nq)
        suggestions = []
        if st:
            same = [p for p in PARCELS if p["norm"].endswith(st) or (" " + st + " ") in (" " + p["norm"] + " ")]
            same = sorted(same, key=lambda p: p["assessed"], reverse=True)[:8]
            suggestions = [{"address": p["address"], "owner": p["owner"]} for p in same]
        return {"matched": False, "query": raw, "street": st, "suggestions": suggestions}

    # pick the highest-assessed matching parcel as the anchor, resolve its owner
    anchor = max(hits, key=lambda p: p["assessed"])
    owner = anchor["owner"]
    assemblage = sorted(BY_OWNER.get(owner.upper(), [anchor]),
                        key=lambda p: p["assessed"], reverse=True)

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
        enrichment = enrich(anchor["apn"])

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
        "match_count": len(hits),
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
                z["split_note"] = ('This assemblage spans multiple zoning districts ("Split"); '
                                   "showing " + z["district_name"] + ", its primary commercial district.")
            return z
    return None


SITE_CACHE = {}
BIZ_CACHE = {}


def site_at(apn, lat, lon, land_sqft, building_sqft, stories):
    """OSM footprint + aerial-derived metrics. Cached; failures are NOT cached (so a
    slow/overloaded Overpass mirror gets retried next time, not stuck permanently)."""
    if apn in SITE_CACHE:
        return SITE_CACHE[apn]
    try:
        res = site_metrics(lat, lon, land_sqft=land_sqft,
                           assessor_building_sqft=building_sqft, stories=stories)
    except Exception as e:
        print(f"  [site {apn}] failed/slow: {e}")
        return None
    SITE_CACHE[apn] = res
    return res


def businesses_at(apn, lat, lon, land_sqft):
    """Named businesses operating at/near this parcel (OSM). Cached; failures are not.

    OSM is volunteer-mapped, so plenty of parcels have nothing on them. When the tight
    (on-parcel) radius comes back empty we widen once to catch the immediate surroundings —
    each result carries distance_m, so the page can be honest about what's actually ON the
    parcel versus merely nearby."""
    if apn in BIZ_CACHE:
        return BIZ_CACHE[apn]
    try:
        # scale search radius to parcel size (small pad/house = tight; big plaza = wide)
        radius = 60 if land_sqft < 20000 else 100 if land_sqft < 80000 else 170
        res = find_businesses(lat, lon, radius=radius)
        if not res:
            res = find_businesses(lat, lon, radius=300, timeout=7)  # widen once
            for r in res:      # flag these: they are NEAR the parcel, not on it
                r["widened"] = True
    except Exception as e:
        print(f"  [businesses {apn}] failed/slow: {e}")
        return None
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
LIVE_MAX_QUERIES = 34      # cap for on-demand live runs (full cached runs are larger)
LIVE_PER_QUERY = 12
LIVE_PACE = (2.5, 4.5)

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
                             max_queries=LIVE_MAX_QUERIES)
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


def start_deeds(owner):
    """Cached result, an in-flight job, or a freshly started one. Never blocks."""
    key = _norm_owner(owner)
    if not key:
        return {"status": "error", "error": "no owner name"}
    if key in DEEDS_DONE:
        return {"status": "done", "cached": True, "result": DEEDS_DONE[key]}
    with _DJOBS_LOCK:
        for jid, j in DEEDS_JOBS.items():
            if j["key"] == key and j["status"] == "running":
                return {"status": "running", "job": jid}
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
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
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
            # Both OSM lookups run in PARALLEL under a HARD server-side deadline. On some
            # hosts (a cloud datacenter IP) Overpass responds very slowly or hangs in a way
            # its own socket timeout doesn't catch, which used to make this endpoint block
            # for 60s+. We bound the whole thing to ~8s and shutdown(wait=False) so a hung
            # OSM thread can never hold up the response; the frontend treats null as "no data".
            ex = ThreadPoolExecutor(max_workers=2)
            f_biz = ex.submit(businesses_at, apn, lat, lon, land_sqft)
            f_site = ex.submit(site_at, apn, lat, lon, land_sqft, building_sqft, stories)
            deadline = time.time() + 8.0

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
            owner = parse_qs(u.query).get("owner", [""])[0]
            self._send(200, json.dumps(start_deeds(owner)))
        elif u.path == "/api/deeds/status":
            job = parse_qs(u.query).get("job", [""])[0]
            self._send(200, json.dumps(deeds_status(job)))
        else:
            self._send(404, json.dumps({"error": "not found"}))


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0E2C49">
<title>DealSynq — Property Intelligence</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='22' fill='%230E2C49'/%3E%3Ctext x='50' y='70' font-size='62' font-family='Georgia,serif' font-weight='bold' fill='%23CFA24C' text-anchor='middle'%3ED%3C/text%3E%3C/svg%3E">
<style>
  :root{--navy:#12304C;--blue:#1F4E78;--gold:#B98A2E;--ink:#1B2733;--slate:#5A6B7B;
        --mist:#F1F5F9;--line:#D8E0E8;--verified:#1E7A46;--strong:#1F6FB2;--est:#B07A16;--unres:#8A97A5;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:#EEF2F6}
  .hero{background:linear-gradient(160deg,#12304C,#1F4E78);color:#fff;padding:46px 20px 54px;text-align:center;
        border-bottom:4px solid var(--gold)}
  .hero .kick{color:var(--gold);font-weight:700;font-size:12px;letter-spacing:2px;text-transform:uppercase}
  .hero h1{font-size:34px;margin:8px 0 4px;font-weight:800}
  .hero p{color:#C7D6E5;font-size:15px}
  .searchwrap{max-width:680px;margin:26px auto 0;display:flex;gap:8px}
  #q{flex:1;padding:15px 18px;border:none;border-radius:10px;font-size:16px;outline:none;box-shadow:0 6px 24px rgba(0,0,0,.18)}
  #go{padding:15px 26px;border:none;border-radius:10px;background:var(--gold);color:#fff;font-weight:700;font-size:15px;cursor:pointer}
  #go:hover{filter:brightness(1.08)}
  .hint{color:#9FB6CC;font-size:12.5px;margin-top:14px}
  .hint b{color:#fff}
  .examples{max-width:720px;margin:12px auto 0;display:flex;gap:10px;flex-wrap:wrap;justify-content:center}
  .ex{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.18);border-radius:10px;
      padding:9px 14px;cursor:pointer;color:#fff;text-align:left;transition:.15s;line-height:1.25}
  .ex:hover{background:rgba(185,138,46,.28);border-color:var(--gold)}
  .ex b{display:block;font-size:13.5px}
  .ex span{display:block;font-size:10.5px;color:#9FB6CC;margin-top:1px}
  .wrap{max-width:980px;margin:-28px auto 60px;padding:0 20px}
  .card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin-bottom:18px;
        box-shadow:0 4px 18px rgba(20,48,76,.06)}
  .prophead{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px}
  .prophead h2{font-size:24px;color:var(--navy)}
  .prophead .sub{color:var(--slate);font-size:14px;margin-top:3px}
  .badge{display:inline-block;padding:4px 11px;border-radius:20px;font-size:11px;font-weight:700;color:#fff}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin-top:18px}
  .kpi{background:var(--mist);border-radius:10px;border-top:3px solid var(--gold);padding:14px 8px;text-align:center}
  .kpi .n{font-size:20px;font-weight:800;color:var(--navy)}
  .kpi .l{font-size:10.5px;font-weight:700;color:var(--slate);text-transform:uppercase;letter-spacing:.5px;margin-top:3px}
  h3.sec{color:var(--navy);font-size:16px;margin:6px 0 10px;padding-bottom:7px;border-bottom:2px solid var(--gold)}
  table{width:100%;border-collapse:collapse;font-size:13.5px}
  th{background:var(--navy);color:#fff;text-align:left;padding:8px 10px;font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
  td{padding:8px 10px;border-bottom:1px solid var(--line)}
  tr:nth-child(even) td{background:var(--mist)}
  .num{text-align:right}
  .tot td{font-weight:700;background:#E7EDF3!important;border-top:2px solid var(--navy)}
  .kv{display:grid;grid-template-columns:180px 1fr;gap:6px 14px;font-size:14px}
  .kv .k{color:var(--slate);font-weight:600}
  .pill{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700;color:#fff;background:var(--strong)}
  .tiers .row{display:flex;gap:12px;padding:9px 0;border-bottom:1px solid var(--line);align-items:flex-start}
  .tiers .row:last-child{border:none}
  .tlabel{width:96px;flex:none;text-align:center;padding:4px 0;border-radius:6px;color:#fff;font-size:11px;font-weight:700}
  .muted{color:var(--slate);font-size:12.5px;margin-top:8px}
  .empty{text-align:center;color:var(--slate);padding:40px 10px}
  .note{background:#FFF7E6;border-left:3px solid var(--gold);padding:12px 14px;border-radius:8px;font-size:13px;color:#5a4a1f;margin-top:14px}
  .loading{text-align:center;color:var(--slate);padding:30px}
  a.src{color:var(--blue);font-size:12px;text-decoration:none}
  .sugg{display:block;width:100%;text-align:left;background:var(--mist);border:1px solid var(--line);
        border-radius:9px;padding:9px 13px;margin-bottom:7px;cursor:pointer;transition:.12s}
  .sugg:hover{border-color:var(--gold);background:#fff}
  .sugg b{display:block;font-size:14px;color:var(--navy)}
  .sugg span{display:block;font-size:11.5px;color:var(--slate);margin-top:1px}
  .bizwrap{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:9px}
  .biz{background:var(--mist);border:1px solid var(--line);border-left:3px solid var(--verified);
       border-radius:8px;padding:9px 12px}
  .biz b{display:block;font-size:13.5px;color:var(--navy)}
  .biz span{display:block;font-size:11px;color:var(--slate);margin-top:2px;text-transform:capitalize}
  .rbtn{background:var(--navy);color:#fff;border:none;border-radius:9px;padding:11px 18px;font-size:14px;
        font-weight:700;cursor:pointer}
  .rbtn:hover{filter:brightness(1.12)} .rbtn:disabled{opacity:.55;cursor:default}
  .prog{height:8px;background:var(--line);border-radius:5px;overflow:hidden;margin:12px 0 6px}
  .prog>i{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--gold));width:0;transition:width .3s}
  .rchips{display:flex;flex-wrap:wrap;gap:7px;margin:4px 0 2px}
  .rchip{background:var(--mist);border:1px solid var(--line);border-radius:20px;padding:4px 12px;font-size:12px;
         color:var(--navy);font-weight:600;cursor:pointer;transition:.12s}
  .rchip:hover,.rchip.on{border-color:var(--gold);background:#FFF7E6}
  .rchip b{color:var(--gold)}
  .rcat{margin-top:14px} .rcat h4{font-size:13px;color:var(--navy);margin:0 0 6px;text-transform:uppercase;letter-spacing:.5px}
  .rsrc{padding:8px 0;border-bottom:1px solid var(--line)}
  .rsrc a{color:var(--blue);font-size:13.5px;font-weight:600;text-decoration:none} .rsrc a:hover{text-decoration:underline}
  .rsrc .dom{color:var(--slate);font-size:11.5px} .rsrc .snip{color:#42505e;font-size:12px;margin-top:2px}
  .rtag{display:inline-block;background:var(--mist);border-radius:5px;padding:0 6px;font-size:10.5px;color:var(--slate);margin-left:6px}
  .cite{background:var(--mist);border-left:3px solid var(--blue);padding:9px 12px;border-radius:7px;
        font-size:11.5px;color:var(--slate);margin-top:14px}
  .zgrp{margin-top:12px} .zgrp .lbl{font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
  .uchips{display:flex;flex-wrap:wrap;gap:6px}
  .uchip{font-size:12px;padding:3px 10px;border-radius:14px;font-weight:600;border:1px solid}
  .uchip.p{background:#E7F5EC;border-color:#B7E0C4;color:var(--verified)}
  .uchip.s{background:#FDF3E0;border-color:#F0D9A8;color:#8a5d0f}
  .uchip.n{background:#F1F4F7;border-color:var(--line);color:var(--slate)}
  .uchip .t{opacity:.7;font-size:10px;margin-left:3px}
  .zbadge{display:inline-block;background:var(--gold);color:#fff;font-weight:800;font-size:12px;padding:3px 10px;border-radius:6px;margin-left:8px}

  /* ============================ polish layer ============================ */
  :root{--navy:#0E2C49;--blue:#1F4E78;--gold:#B98A2E;--gold2:#CFA24C;--mist:#F2F6FA;--line:#E2E8F0;
        --sh-sm:0 1px 2px rgba(16,40,64,.07);--sh-md:0 6px 22px rgba(14,44,73,.09),0 2px 6px rgba(14,44,73,.05);
        --sh-lg:0 20px 46px rgba(14,44,73,.20);}
  html{-webkit-text-size-adjust:100%}
  body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
       -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;background:#EAEFF4}
  ::selection{background:rgba(207,162,76,.22)}
  button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--gold2);outline-offset:2px}

  .hero{padding:54px 20px 74px;position:relative;overflow:hidden;border-bottom:3px solid var(--gold);
        background:radial-gradient(1100px 380px at 50% -140px,rgba(207,162,76,.20),transparent),
                   linear-gradient(165deg,#0E2C49,#123a5e 55%,#1F4E78)}
  .hero::before{content:"";position:absolute;inset:0;pointer-events:none;opacity:.6;
        background-image:radial-gradient(rgba(255,255,255,.05) 1px,transparent 1px);background-size:22px 22px}
  .hero>*{position:relative}
  .hero .kick{letter-spacing:2.5px;font-size:11.5px;color:var(--gold2)}
  .hero h1{font-size:36px;line-height:1.1;letter-spacing:-.6px;margin:12px auto 8px;max-width:660px}
  .hero p{color:#BBD0E4;font-size:15.5px;max-width:560px;margin:0 auto}
  .searchwrap{gap:10px;max-width:640px}
  .searchbox{flex:1;position:relative;display:flex;align-items:center}
  .searchbox svg{position:absolute;left:16px;width:18px;height:18px;color:#8aa2b8;pointer-events:none}
  .searchbox #q{flex:1;padding:16px 18px 16px 46px;border-radius:12px;box-shadow:var(--sh-lg)}
  #go{border-radius:12px;padding:0 28px;transition:.15s;box-shadow:var(--sh-md);
      background:linear-gradient(180deg,var(--gold2),var(--gold))}
  #go:hover{filter:brightness(1.06);transform:translateY(-1px)}
  #go:active{transform:translateY(0)}
  .ex{border-radius:11px;transition:.16s}
  .ex:hover{transform:translateY(-2px);background:rgba(207,162,76,.22);border-color:var(--gold2)}

  .wrap{max-width:1000px;margin:-40px auto 24px}
  .card{border-radius:16px;padding:24px 26px;box-shadow:var(--sh-md);
        animation:rise .45s cubic-bezier(.2,.7,.3,1) both}
  @keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  .card:nth-child(2){animation-delay:.05s}.card:nth-child(3){animation-delay:.1s}
  .card:nth-child(4){animation-delay:.14s}.card:nth-child(n+5){animation-delay:.17s}

  .prophead h2{font-size:26px;letter-spacing:-.4px;line-height:1.15}
  .prophead .sub{margin-top:5px}
  .badge{display:inline-flex;align-items:center;gap:6px;padding:6px 13px;box-shadow:var(--sh-sm);letter-spacing:.4px}
  .badge::before{content:"";width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,.85)}

  .kpis{gap:12px;margin-top:20px}
  .kpi{border-top:none;border:1px solid var(--line);border-radius:12px;padding:16px 10px;position:relative;
       overflow:hidden;transition:.16s;background:linear-gradient(180deg,#fff,var(--mist))}
  .kpi::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;
       background:linear-gradient(90deg,var(--gold2),var(--gold))}
  .kpi:hover{transform:translateY(-2px);box-shadow:var(--sh-sm)}
  .kpi .n{font-size:22px;letter-spacing:-.5px}
  .kpi .l{letter-spacing:.6px;margin-top:5px}

  h3.sec{border-bottom:none;padding:0 0 0 13px;margin:2px 0 15px;font-size:15px;font-weight:800;
         position:relative;display:flex;align-items:center;flex-wrap:wrap;gap:8px;min-height:18px}
  h3.sec::before{content:"";position:absolute;left:0;top:2px;bottom:2px;width:4px;border-radius:3px;
         background:linear-gradient(180deg,var(--gold2),var(--gold))}

  .tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--line);border-radius:10px}
  th{padding:10px 12px;font-size:11px;letter-spacing:.5px;white-space:nowrap}
  td{padding:9px 12px}
  .num{font-variant-numeric:tabular-nums}
  .tot td{font-weight:800}
  .kv{grid-template-columns:188px 1fr;gap:9px 16px}
  .note{border-radius:10px;padding:12px 15px}
  .cite{border-radius:9px}.biz{border-radius:10px}.rbtn{border-radius:10px}

  .loading{padding:46px 10px;font-size:14px}
  .spin{width:26px;height:26px;border:3px solid var(--line);border-top-color:var(--gold);border-radius:50%;
        margin:0 auto 14px;animation:sp .7s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}

  .foot{max-width:1000px;margin:0 auto;padding:6px 20px 46px;color:#8896a4;font-size:11.5px;text-align:center;line-height:1.75}
  .foot .brand{font-weight:800;color:var(--navy);letter-spacing:.5px}
  .foot a{color:#7C8A98}
  .foot .dot{margin:0 7px;opacity:.5}

  @media(max-width:680px){
    .hero{padding:38px 16px 56px}
    .hero h1{font-size:26px}
    .hero p{font-size:14px}
    .searchwrap{flex-direction:column}
    #go{padding:14px}
    .wrap{padding:0 14px;margin-top:-32px}
    .card{padding:18px 16px}
    .prophead h2{font-size:21px}
    .kv{grid-template-columns:1fr;gap:1px 0}
    .kv .k{margin-top:9px}
    .tscroll table{min-width:540px}
    h3.sec{font-size:14px}
  }
  @media(prefers-reduced-motion:reduce){.card{animation:none}.spin{animation-duration:1.5s}}

  /* recorded documents (registry of deeds) */
  .dsum{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 4px}
  .dpill{border:1px solid var(--line);border-radius:9px;padding:7px 12px;background:var(--mist);
         font-size:12px;color:var(--slate);font-weight:600}
  .dpill b{color:var(--navy);font-size:15px;font-weight:800;margin-right:5px;font-variant-numeric:tabular-nums}
  .dpill.hot{background:#FDECEC;border-color:#F3C9C9;color:#8d2a2a}
  .dpill.hot b{color:#B3261E}
  .dpill.clear{background:#E7F5EC;border-color:#B7E0C4;color:#1E7A46}
  .dpill.clear b{color:#1E7A46}
  .dtype{display:inline-block;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:700;
         background:var(--mist);color:var(--slate)}
  .dtype.mtg{background:#FDF3E0;color:#8a5d0f}
  .dtype.deed{background:#E8F0F8;color:var(--blue)}
  .dtype.lien{background:#FDECEC;color:#B3261E}

  /* ==================================================================== */
  /*  DESIGN v3 — definitive layer. Redefines tokens (recolors the whole   */
  /*  app) then restyles components into one institutional system.         */
  /* ==================================================================== */
  :root{
    --navy:#0B2740; --navy2:#12395B; --blue:#235B8C; --gold:#B4863B; --gold2:#D6AD5C;
    --ink:#17232F; --slate:#5A6773; --faint:#8593A2;
    --paper:#ECEFF4; --card:#FFFFFF; --mist:#F4F7FB; --mist2:#EBF1F7; --line:#E3E9F1;
    --verified:#1E7A46; --strong:#235B8C; --est:#A9762A; --unres:#93A0AC;
    --sh-sm:0 1px 2px rgba(11,39,64,.06);
    --sh-md:0 10px 30px rgba(11,39,64,.08),0 2px 7px rgba(11,39,64,.05);
    --sh-lg:0 26px 56px rgba(11,39,64,.24);
    --serif:'Iowan Old Style','Palatino Linotype',Palatino,Georgia,'Times New Roman',serif;
    --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
  }
  body{font-family:var(--sans);background:var(--paper);color:var(--ink);font-size:15px;line-height:1.55}

  /* --- hero --- */
  .hero{padding:60px 20px 82px;border-bottom:none;
        background:radial-gradient(1200px 420px at 50% -160px,rgba(214,173,92,.22),transparent),
                   linear-gradient(168deg,#0A2238 0%,#12395B 60%,#1C4E79 100%)}
  .hero::after{content:"";position:absolute;left:0;right:0;bottom:0;height:3px;
        background:linear-gradient(90deg,transparent,var(--gold2),var(--gold),transparent)}
  .hero .kick{color:var(--gold2);letter-spacing:3px;font-size:11px;font-weight:700}
  .hero h1{font-family:var(--serif);font-weight:600;font-size:41px;line-height:1.08;
           letter-spacing:-.3px;margin:14px auto 10px;max-width:660px;text-wrap:balance}
  .hero p{color:#BCD0E3;font-size:15.5px;max-width:548px;line-height:1.6}
  .searchbox svg{color:#93aabd}
  .searchbox #q{padding:17px 18px 17px 48px;border-radius:13px;font-size:16px;font-family:var(--sans);
                box-shadow:var(--sh-lg)}
  #go{border-radius:13px;font-family:var(--sans);font-weight:700;letter-spacing:.2px;
      background:linear-gradient(180deg,var(--gold2),var(--gold));box-shadow:0 8px 22px rgba(180,134,59,.32)}
  .hint{color:#93aabf}
  .ex{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);border-radius:12px;padding:10px 15px}
  .ex:hover{background:rgba(214,173,92,.2);border-color:var(--gold2)}
  .ex b{font-size:13.5px;font-weight:700} .ex span{color:#a6bace}

  /* --- layout & cards --- */
  .wrap{max-width:1020px;margin:-46px auto 26px;padding:0 22px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:26px 28px;
        margin-bottom:18px;box-shadow:var(--sh-md)}
  .card:hover{box-shadow:var(--sh-lg)}
  .card{transition:box-shadow .25s ease}

  /* --- property header --- */
  .prophead{align-items:flex-start;gap:16px}
  .prophead h2{font-family:var(--serif);font-weight:600;font-size:29px;line-height:1.12;
               letter-spacing:-.3px;color:var(--navy)}
  .prophead .sub{color:var(--slate);font-size:13.5px;margin-top:6px;letter-spacing:.1px}
  .badge{padding:7px 14px;border-radius:30px;font-size:10.5px;letter-spacing:.7px;box-shadow:var(--sh-sm)}

  /* --- KPI stat tiles (cleaner: no per-tile gold bar) --- */
  .kpis{gap:14px;margin-top:22px}
  .kpi{background:linear-gradient(180deg,#fff,var(--mist));border:1px solid var(--line);border-radius:13px;
       padding:19px 12px 17px;position:relative;overflow:hidden;transition:.18s ease}
  .kpi::before{content:"";display:block;position:absolute;top:0;left:0;right:0;height:3px;
       background:linear-gradient(90deg,var(--gold2),var(--gold));opacity:.9}
  .kpi::after{display:none}
  .kpi:hover{transform:translateY(-2px);box-shadow:var(--sh-md);border-color:#D3DEE9}
  .kpi .n{font-size:25px;font-weight:800;color:var(--navy);letter-spacing:-.6px;font-variant-numeric:tabular-nums}
  .kpi .l{font-size:10px;letter-spacing:.8px;margin-top:8px;color:var(--slate)}

  /* --- section headers: uppercase eyebrow + short gold rule --- */
  h3.sec{font-family:var(--sans);text-transform:uppercase;font-size:12px;font-weight:800;
         letter-spacing:1.4px;color:var(--navy);padding:0 0 0 16px;margin:2px 0 16px}
  h3.sec::before{width:8px;height:8px;top:50%;bottom:auto;border-radius:2px;
         transform:translateY(-50%) rotate(45deg);left:1px;background:var(--gold)}
  h3.sec span{text-transform:none;letter-spacing:0;font-weight:600}

  /* --- tables --- */
  .tscroll{border:1px solid var(--line);border-radius:12px;box-shadow:var(--sh-sm)}
  table{font-size:13.5px}
  th{background:linear-gradient(180deg,#123a5c,#0E2E49);color:#EAF1F8;padding:11px 14px;
     font-size:10.5px;letter-spacing:.7px;font-weight:700;border-bottom:none}
  td{padding:11px 14px;border-bottom:1px solid var(--mist2)}
  tr:nth-child(even) td{background:#FAFCFE}
  tr:last-child td{border-bottom:none}
  .tot td{background:var(--mist2)!important;border-top:2px solid var(--gold)}
  .num{font-variant-numeric:tabular-nums}

  /* --- key/value --- */
  .kv{grid-template-columns:190px 1fr;gap:11px 18px;font-size:14px}
  .kv .k{color:var(--slate);font-weight:600;font-size:13px}

  /* --- pills / chips unified --- */
  .pill{border-radius:30px;padding:3px 10px;background:var(--strong)}
  .uchip{border-radius:30px;padding:4px 12px;font-size:12px}
  .rchip{border-radius:30px;background:var(--mist);border-color:var(--line)}
  .rchip.on,.rchip:hover{background:#FBF4E4;border-color:var(--gold2)}
  .dpill{border-radius:11px;padding:8px 13px}
  .zbadge{border-radius:7px;background:linear-gradient(180deg,var(--gold2),var(--gold));font-size:11.5px}

  /* --- businesses --- */
  .bizwrap{gap:11px}
  .biz{background:var(--mist);border:1px solid var(--line);border-left:3px solid var(--gold);border-radius:11px;padding:11px 13px}
  .biz b{color:var(--navy);font-size:13.5px}

  /* --- buttons / progress / notes --- */
  .rbtn{background:var(--navy);border-radius:11px;padding:12px 20px;font-weight:700;letter-spacing:.2px;transition:.16s}
  .rbtn:hover{background:var(--navy2);transform:translateY(-1px)}
  .prog{height:7px;border-radius:6px;background:var(--mist2)}
  .prog>i{background:linear-gradient(90deg,var(--blue),var(--gold2))}
  .note{background:#FBF6EA;border-left:3px solid var(--gold);border-radius:11px;color:#5f4d24}
  .cite{background:var(--mist);border-left:3px solid var(--blue);border-radius:10px}
  .muted{color:var(--slate)}
  .spin{border-top-color:var(--gold)}
  a.src,.rsrc a{color:var(--blue)}

  /* --- footer --- */
  .foot{padding:14px 22px 52px;color:var(--faint);font-size:11.5px}
  .foot .brand{font-family:var(--serif);font-weight:700;color:var(--navy);letter-spacing:.5px;font-size:13px}

  @media(max-width:680px){
    .hero{padding:42px 16px 60px} .hero h1{font-size:28px}
    .wrap{margin-top:-36px;padding:0 14px} .card{padding:19px 17px}
    .prophead h2{font-size:23px} .kpi .n{font-size:21px}
    .kv{grid-template-columns:1fr;gap:2px 0} .kv .k{margin-top:10px}
  }
</style></head><body>
<div class="hero">
  <div class="kick">DealSynq &bull; Property Intelligence</div>
  <h1>Every public record on a property, in one search.</h1>
  <p>Ownership &amp; assemblage, zoning, recorded deeds &amp; mortgages, tenants, and a deep web sweep &mdash; assembled live from public sources.</p>
  <div class="searchwrap">
    <div class="searchbox">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
      <input id="q" placeholder="e.g. 380 Cooley St  &mdash;  or an owner name" autofocus>
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
</div>
<div class="wrap" id="out"></div>
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
  const q=$("#q").value.trim(); if(!q)return;
  const gen=++GEN;
  out.innerHTML=loadingHTML("Resolving &hellip;");
  const r=await fetch("/api/search?q="+encodeURIComponent(q));
  const d=await r.json();
  if(stale(gen)) return;                 // a newer search overtook this one
  if(!d.matched){
    let sg='';
    if(d.suggestions&&d.suggestions.length){
      sg='<div style="margin-top:14px;text-align:left"><div class="muted" style="margin-bottom:8px">No parcel is recorded at that exact address &mdash; it may be a secondary/entrance address. Other parcels on that street:</div>';
      d.suggestions.forEach(s=>{sg+='<button class="sugg" data-q="'+escA(s.address)+'"><b>'+esc(titlecase(s.address))+'</b><span>'+esc(s.owner)+'</span></button>';});
      sg+='</div>';
    } else {
      sg='<br>This demo covers Springfield, MA. Try <b>380 Cooley St</b>.';
    }
    out.innerHTML='<div class="card empty" style="text-align:center"><b>No exact match for &ldquo;'+esc(q)+'&rdquo;.</b>'+sg+'</div>';
    document.querySelectorAll(".sugg").forEach(b=>b.onclick=()=>{$("#q").value=b.dataset.q;run();});
    return;
  }
  render(d);
  if(d.extra_params) loadExtras(d.extra_params, gen);  // fire-and-forget, background, never blocks
}

function renderExtraCards(biz,st){
  let h='';
  // ALWAYS render this section, for every address. An empty result is information too —
  // silently hiding the card just looks broken.
  h+='<div class="card"><h3 class="sec">Businesses Operating Here <span style="font-size:11px;color:var(--slate);font-weight:600">&bull; live from OpenStreetMap</span></h3>';
  if(biz&&biz.length){
    // the server flags results that only turned up after widening the search — those are
    // NEAR the parcel, not on it. Never claim a neighbour is a tenant.
    const onParcel=biz.filter(x=>!x.widened);
    const shown=onParcel.length?onParcel:biz;
    h+='<div class="bizwrap">';
    shown.forEach(x=>{
      const sub=[x.type&&x.type.replace(/_/g," "), x.cuisine].filter(Boolean).join(" &bull; ");
      h+='<div class="biz"><b>'+esc(x.name)+'</b><span>'+esc(sub)+(x.distance_m!=null?(" &bull; "+x.distance_m+"m"):"")+'</span></div>';
    });
    h+='</div>';
    h+= onParcel.length
      ? '<div class="muted">Actual operating businesses at this location &mdash; the store brands the assessor&rsquo;s use-code doesn&rsquo;t name.</div>'
      : '<div class="muted"><b>Nothing is mapped on this parcel itself</b> &mdash; these are the nearest mapped businesses (see distances). OpenStreetMap is volunteer-mapped, so smaller tenants are often missing.</div>';
  } else if(biz){   // reached OSM, genuinely nothing nearby
    h+='<div class="note">No businesses are mapped at or near this parcel in OpenStreetMap. OSM is volunteer-mapped, so smaller tenants are frequently absent &mdash; this is a <b>coverage gap, not evidence the property is vacant</b>. A paid source (e.g. Google Places) would cover more.</div>';
  } else {          // null = the lookup failed / timed out
    h+='<div class="note">Couldn&rsquo;t reach OpenStreetMap just now &mdash; its public servers are frequently overloaded. This is a <b>lookup failure, not a finding</b>; try the search again in a moment.</div>';
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
    h+='</div><div class="muted">Footprint &amp; roof area from OpenStreetMap building polygons; height, parking, solar and FAR are <b>estimates</b> derived from footprint + parcel size. Max-permitted FAR, LEED and Energy Star require the zoning ordinance and USGBC/EPA registries (not yet wired).</div></div>';
  }
  return h;
}

async function loadExtras(p, gen){
  const el=document.getElementById("extras");
  if(!el) return;   // user navigated away / searched again already
  const ctrl=new AbortController();
  const timer=setTimeout(()=>ctrl.abort(), 14000);  // hard cap — generous since this is
                                                    // background-only and never blocks the UI
  try{
    const qs=new URLSearchParams({apn:p.apn, lat:p.lat, lon:p.lon, land_sqft:p.land_sqft,
                                  building_sqft:p.building_sqft, stories:p.stories||""});
    const r=await fetch("/api/extra?"+qs.toString(), {signal:ctrl.signal});
    const d=await r.json();
    if(stale(gen)) return;                      // results belong to a previous address
    const cur=document.getElementById("extras");
    if(cur) cur.innerHTML=renderExtraCards(d.businesses,d.site);
  }catch(e){
    // Slow/unreachable third-party server (OpenStreetMap) — say so rather than vanish.
    if(stale(gen)) return;
    const cur=document.getElementById("extras");
    if(cur) cur.innerHTML=renderExtraCards(null,null);
  }finally{
    clearTimeout(timer);
  }
}

function render(d){
  const t=d.totals;
  let h='';
  const deep=d.deep;
  const name = deep ? deep.property_name : (d.owner || "Property");
  const subtitle = deep ? (deep.anchor_address+"  &bull;  "+deep.property_subtype)
                        : (d.anchor_address+"  &bull;  "+(d.neighborhood||"Springfield, MA"));
  const badge = deep ? '<span class="badge" style="background:var(--verified)">DEEP PROFILE</span>'
                     : '<span class="badge" style="background:var(--strong)">LIVE LOOKUP</span>';

  // header + KPIs
  h+='<div class="card"><div class="prophead"><div><h2>'+esc(name)+'</h2><div class="sub">'+subtitle+'</div></div>'+badge+'</div>';
  h+='<div class="kpis">';
  h+=kpi(t.parcels,"Parcels");
  h+=kpi(t.land_acres,"Land Acres");
  if(deep){h+=kpi("336,205","Building SF");h+=kpi(deep.tenants.occupancy_rate_by_sqft+"%","Occupancy");}
  h+=kpi(money(t.assessed),"Assessed");
  h+='</div>';
  h+='</div>';

  // ---- Businesses operating here + Building Footprint (OSM) ----
  // These are slow, best-effort, third-party (Overpass) lookups — fetched in the
  // BACKGROUND after everything else renders, so they never delay or block the page.
  // See loadExtras() below.
  h+='<div id="extras"></div>';

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
    h+='</div></div>';

    // sale history
    if(e.sales&&e.sales.length){
      h+='<div class="card"><h3 class="sec">Sale History</h3><table><tr><th>Date</th><th class="num">Price</th><th>Buyer</th></tr>';
      e.sales.forEach(s=>{h+='<tr><td>'+s.date+'</td><td class="num">'+(s.price?money(s.price):"&mdash;")+'</td><td>'+esc(titlecase(s.grantee||""))+'</td></tr>';});
      h+='</table></div>';
    }
    // permits
    if(e.permits&&e.permits.length){
      h+='<div class="card"><h3 class="sec">Permit Activity ('+e.permits.length+' recent)</h3><table><tr><th>Date</th><th>Permit #</th><th class="num">Value</th><th>Purpose</th></tr>';
      e.permits.forEach(p=>{h+='<tr><td>'+p.date+'</td><td>'+esc(p.number||"")+'</td><td class="num">'+(p.price?money(p.price):"&mdash;")+'</td><td>'+esc(p.purpose||"")+'</td></tr>';});
      h+='</table></div>';
    }
  } else if(!deep){
    h+='<div class="card note">Parcel + owner + assemblage resolved live. Record-card detail couldn&rsquo;t be fetched right now (the assessor site may be rate-limiting) &mdash; try again in a moment.</div>';
  }

  // ownership (deep)
  if(deep){
    const o=deep.ownership, pc=o.parent_chain;
    h+='<div class="card"><h3 class="sec">Ownership</h3><div class="kv">';
    h+=kv("Owning entity",esc(o.current_owner.name)+" ("+o.current_owner.jurisdiction+")");
    h+=kv("Parent",esc(pc.parent.name)+' <span class="pill">NASDAQ: PECO</span>');
    h+=kv("Mailing",esc(o.current_owner.mailing_address));
    h+=kv("Manager",esc(o.property_manager));
    h+='</div><div class="muted">Confirmed via SEC Exhibit 21.1 (federal filing).</div></div>';
  }

  // assemblage table
  h+='<div class="card"><h3 class="sec">Parcel Assemblage ('+t.parcels+')</h3><table><tr><th>APN</th><th>Address</th><th class="num">Land SF</th><th class="num">Assessed</th><th>Zone</th></tr>';
  d.assemblage.forEach(p=>{h+='<tr><td>'+p.apn+'</td><td>'+esc(titlecase(p.address))+'</td><td class="num">'+Math.round(p.land_sqft).toLocaleString()+'</td><td class="num">'+money(p.assessed)+'</td><td>'+esc(p.zone)+'</td></tr>';});
  h+='<tr class="tot"><td colspan="2">TOTAL &mdash; '+t.parcels+' parcels</td><td class="num">'+Math.round(t.land_sqft).toLocaleString()+'</td><td class="num">'+money(t.assessed)+'</td><td></td></tr></table></div>';

  // deep: transactions + tenants + confidence
  if(deep){
    const tx=deep.transaction_history[0];
    h+='<div class="card"><h3 class="sec">Transaction &amp; Financing</h3><div class="kv">';
    h+=kv("Last sale",money(tx.price)+" &bull; "+tx.date+" &bull; "+esc(tx.seller?("from "+tx.seller):""));
    h+=kv("Structure",esc(tx.structure||""));
    h+=kv("Mortgage",'<b style="color:var(--verified)">None recorded</b> &mdash; all-cash acquisition');
    h+=kv("Est. annual tax",money(deep.tax.estimated_annual_tax)+" (FY2026 commercial rate)");
    h+='</div></div>';

    const te=deep.tenants;
    h+='<div class="card"><h3 class="sec">Tenants &mdash; '+te.space_count+' spaces &bull; '+te.occupancy_rate_by_sqft+'% occupied</h3><table><tr><th>Tenant</th><th class="num">SF</th><th>Status</th><th>Public</th></tr>';
    te.roster.forEach(x=>{const pub=x.ticker||"";const col=x.status=="occupied"?"var(--verified)":"var(--est)";
      h+='<tr><td>'+esc(x.tenant_name)+'</td><td class="num">'+x.sqft.toLocaleString()+'</td><td style="color:'+col+';font-weight:700">'+titlecase(x.status)+'</td><td style="color:var(--slate);font-size:12px">'+esc(pub)+'</td></tr>';});
    h+='</table></div>';

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
  initDeeds();
  initResearch();
  window.scrollTo({top:0,behavior:"smooth"});
}

// ---------- Recorded documents (Registry of Deeds) ----------
function initDeeds(){
  const el=document.getElementById("deeds"); if(!el) return;
  const gen=GEN;
  const owner=el.dataset.owner;
  if(!owner){el.remove();return;}
  el.innerHTML='<h3 class="sec">Recorded Documents <span style="font-size:11px;color:var(--slate);font-weight:600">&bull; Hampden County Registry of Deeds</span></h3>'
    +'<div class="muted" style="margin:0 0 12px">Every deed, mortgage, discharge, lien, easement and lease recorded under <b>'+esc(owner)+'</b> '
    +'&mdash; the debt &amp; encumbrance picture the assessor never publishes.</div>'
    +'<button class="rbtn" id="dgo">Look up recorded documents</button>'
    +'<span class="muted" id="dnote" style="margin-left:12px">~30s &bull; the registry is bot-protected, so this runs a real browser, paced</span>'
    +'<div id="dbody"></div>';
  document.getElementById("dgo").onclick=()=>runDeeds(owner,GEN);
  // if it's already cached server-side, load it immediately (no click, no wait)
  fetch("/api/deeds?owner="+encodeURIComponent(owner)).then(r=>r.json()).then(d=>{
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
  const lmYear=lm&&lm.date?parseInt(String(lm.date).slice(-4)):null;
  const recent=lmYear&&((new Date().getFullYear()-lmYear)<=30);
  let h='<div class="dsum">';
  h+='<div class="dpill"><b>'+s.total+'</b>documents</div>';
  if(c.deeds) h+='<div class="dpill"><b>'+c.deeds+'</b>deeds</div>';
  h+='<div class="dpill'+(c.mortgages?(recent?" hot":""):" clear")+'"><b>'+c.mortgages+'</b>mortgage'+(c.mortgages==1?"":"s")+'</div>';
  if(c.discharges) h+='<div class="dpill"><b>'+c.discharges+'</b>discharges</div>';
  if(c.liens) h+='<div class="dpill hot"><b>'+c.liens+'</b>liens</div>';
  if(c.leases) h+='<div class="dpill"><b>'+c.leases+'</b>leases</div>';
  if(c.easements) h+='<div class="dpill"><b>'+c.easements+'</b>easements</div>';
  h+='</div>';
  h+='<div class="muted" style="margin:6px 0 12px">';
  if(!c.mortgages){
    h+='<b style="color:var(--verified)">No mortgage recorded under this name.</b>';
  } else {
    h+='<b style="color:'+(recent?"#B3261E":"var(--slate)")+'">Most recent mortgage: '+esc(lm.date||"")
      +(lm.lender?(' to '+esc(titlecase(lm.lender))):'')+'</b>'
      +(recent?' &mdash; recent enough to warrant a payoff check.'
             :' &mdash; old enough that it is very likely long satisfied.');
    if(s.mortgage_dates&&s.mortgage_dates.length>1)
      h+=' All recorded: '+s.mortgage_dates.map(esc).join(', ')+'.';
  }
  h+=' <span style="color:var(--slate)">Discharges are usually recorded under the <b>lender\'s</b> name, so their absence here is <b>not</b> proof a loan is outstanding &mdash; confirming a payoff means reading the documents.</span></div>';

  if(doc.records&&doc.records.length){
    h+='<table><tr><th>Recorded</th><th>Document</th><th>Book/Page</th><th>Counterparty</th></tr>';
    doc.records.forEach(r=>{
      const role=r.party_role==="grantee"?"&larr;":"&rarr;";
      // NB: keep the em-dash entity OUT of esc()/titlecase() — they'd mangle it to "&Mdash;"
      const party=r.reverse_party ? esc(titlecase(r.reverse_party)) : "&mdash;";
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
    +'<span class="muted" id="rnote" style="margin-left:12px">'+(deep?"instant &bull; pre-built for this flagship property":"~60&ndash;120s live &bull; paced to avoid rate-limits")+'</span>'
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
  h+=kpi(doc.query_count,"Queries Run");
  h+=kpi(doc.unique_url_count,"Unique Sites");
  h+=kpi(doc.unique_domain_count,"Domains");
  h+=kpi(doc.elapsed_seconds+"s",cached?"Run Time (cached)":"Run Time");
  h+='</div>';
  // category filter chips
  const cats=Object.keys(doc.categories||{});
  h+='<div class="rchips" id="rchips"><span class="rchip on" data-cat="__all">All <b>'+doc.unique_url_count+'</b></span>';
  cats.forEach(c=>{h+='<span class="rchip" data-cat="'+esc(c)+'">'+esc(c)+' <b>'+doc.categories[c].length+'</b></span>';});
  h+='</div>';
  // source list (all, ranked) — filtered client-side by chip
  h+='<div id="rlist"></div>';
  h+='<div class="cite"><b>Source:</b> '+esc(doc.engine)+(eng?(" ("+eng+")"):"")+'. '
    +doc.query_count+' automated keyword searches, run '+esc((doc.generated_at||"").slice(0,10))+'. '
    +'This is a first-pass automated web sweep &mdash; a discovery list of where the data lives, not yet verified or extracted.</div>';
  body.innerHTML=h;
  window.__research=doc;
  drawSources("__all");
  document.querySelectorAll("#rchips .rchip").forEach(c=>c.onclick=()=>{
    document.querySelectorAll("#rchips .rchip").forEach(x=>x.classList.remove("on"));
    c.classList.add("on"); drawSources(c.dataset.cat);
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
