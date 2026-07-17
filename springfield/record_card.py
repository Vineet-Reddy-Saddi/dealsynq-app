"""
Springfield assessor record-card scraper — GENERAL: given any Springfield parcel APN,
returns the full record-card detail (use class, zoning, assessment split, building info,
building square footage, sale/ownership history, permit history).

Same source we used to enrich Five Town Plaza; works for ANY parcel, not just that one.

    from springfield.record_card import fetch_parcel
    data = fetch_parcel("031700053")

CLI:  python springfield/record_card.py 031700251
"""
import re
import sys
import urllib.parse
import urllib.request

BASE = "https://www.springfield-ma.gov/finance/assessors-search/assessors.php"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def _clean(body):
    t = re.sub(r"<script.*?</script>", "", body, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", "", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", "|", t)
    t = re.sub(r"\|+", " | ", t)
    t = t.replace("&nbsp;", " ")
    t = re.sub(r" +", " ", t)
    out = []
    for ln in t.split("\n"):
        ln = re.sub(r"^[\s|]+|[\s|]+$", "", ln)
        if ln.strip():
            out.append(ln)
    return out


def _get(apn, card=None):
    if card is None:
        req = urllib.request.Request(f"{BASE}?parcel={apn}", headers=HEADERS)
    else:
        data = urllib.parse.urlencode({"parcel": apn, "card_num": str(card), "submit": "Go"}).encode()
        req = urllib.request.Request(BASE, data=data, headers={**HEADERS, "Referer": f"{BASE}?parcel={apn}"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")


def _clean_val(v):
    return re.sub(r"^[|\s]+", "", v or "").strip() if v else v


def _field(lines, label):
    for i, l in enumerate(lines):
        if l.startswith(label):
            m = re.search(re.escape(label) + r"\s*(.*)", l)
            v = (m.group(1).strip() if m else "")
            if not v and i + 1 < len(lines):
                nxt = lines[i + 1]
                # only fall through to the next line if it's a value, not another label
                if not re.match(r"^[A-Za-z][A-Za-z /&#]+:", nxt):
                    v = nxt
            return _clean_val(v)
    return None


def _building_sqft(lines):
    idxs = [i for i, l in enumerate(lines) if "Interior / Exterior Information" in l]
    if not idxs:
        return 0
    seg = lines[idxs[0]:idxs[0] + 90]
    if "Perim" not in seg:
        return 0
    rows = seg[seg.index("Perim") + 1:]
    tot, i = 0, 0
    while i + 5 < len(rows):
        if not rows[i].isdigit() or len(rows[i]) > 2:
            break
        a = rows[i + 4].replace(",", "")
        if a.isdigit():
            tot += int(a)
        i += 6
    return tot


def _structure_type(lines):
    # commercial uses "Structure Type:", residential uses "Style:"
    return _field(lines, "Structure Type:") or _field(lines, "Style:")


def _grade(lines):
    return _field(lines, "Grade:")


def _year_built(lines):
    v = _field(lines, "Year Built/Eff Year:") or _field(lines, "Year Built:")
    if not v:
        return None
    m = re.search(r"(\d{4})", v)
    return m.group(1) if m else None


def _living_area(lines):
    """Residential floor area (commercial parcels won't have this label)."""
    v = _field(lines, "Total Living Area:")
    if v:
        m = re.search(r"([\d,]+)", v)
        if m:
            return int(m.group(1).replace(",", ""))
    return 0


def _room_detail(lines):
    out = {}
    for label, key in [("Bedrooms:", "bedrooms"), ("Total Rooms:", "rooms"),
                       ("Full Baths:", "full_baths"), ("Half Baths:", "half_baths")]:
        v = _field(lines, label)
        if v:
            m = re.search(r"(\d+)", v)
            if m:
                out[key] = int(m.group(1))
    return out


def _detail(lines):
    """Residential-style construction detail (present on residential cards)."""
    out = {}
    for label, key in [("Story Height:", "stories"), ("Exterior Walls:", "exterior_walls"),
                       ("Roof Cover:", "roof"), ("Roof Structure:", "roof"),
                       ("Heat Type:", "heat_type"), ("Fuel Type:", "fuel_type"),
                       ("Basement:", "basement"), ("Condition:", "condition"),
                       ("Attic:", "attic"), ("Eff Year Built:", "eff_year_built")]:
        if key in out:
            continue
        v = _field(lines, label)
        if v and v not in ("0", "NONE"):
            out[key] = v
    return out


def _construction(lines):
    """Commercial construction detail — first data row of the Use Type table
    (Use Type | Wall Height | Ext Walls | Construction | Partitions | Heating | Cooling | ...)."""
    idxs = [i for i, l in enumerate(lines) if l == "Use Type"]
    if not idxs:
        return {}
    seg = lines[idxs[0] + 1:idxs[0] + 40]
    # header continues: Wall Height, Ext Walls, Construction, Partitions, Heating, Cooling, Plumbing, Physical, Functional
    HDR = {"Wall Height", "Ext Walls", "Construction", "Partitions", "Heating",
           "Cooling", "Plumbing", "Physical", "Functional"}
    data = [t for t in seg if t not in HDR]
    if len(data) < 7:
        return {}
    # first data row: [use_type, wall_height, ext_walls, construction, partitions, heating, cooling, ...]
    return {
        "use_type": data[0],
        "exterior_walls": data[2] if len(data) > 2 else None,
        "construction_type": data[3] if len(data) > 3 else None,
        "heat_type": data[5] if len(data) > 5 else None,
        "cooling": data[6] if len(data) > 6 else None,
    }


def _features(lines):
    """Building Other Features list (canopies, loading docks, elevators, sprinklers, doors)."""
    idxs = [i for i, l in enumerate(lines) if "Building Other Features" in l]
    if not idxs:
        return []
    i0 = idxs[0]
    stop = next((j for j in range(i0, len(lines)) if "Interior / Exterior Information" in lines[j]), i0 + 60)
    feats = []
    for l in lines[i0 + 1:stop]:
        if re.match(r"^[A-Z0-9][A-Z0-9 ,/\-]+$", l) and any(c.isalpha() for c in l) and l == l.upper() and len(l) > 4:
            feats.append(l)
    return feats


def _assessment(lines):
    """Land / Building / Total assessed values from the Assessment Information block."""
    idxs = [i for i, l in enumerate(lines) if l == "Assessment Information"]
    if not idxs:
        return {}
    seg = lines[idxs[0]:idxs[0] + 25]
    out = {}
    for key in ("Land", "Building", "Total"):
        for j, l in enumerate(seg):
            if l == key and j + 1 < len(seg):
                m = re.search(r"([\d,]+)", seg[j + 1])
                if m:
                    out[key.lower()] = int(m.group(1).replace(",", ""))
                break
    return out


_SALE_STOP = ("CARD", "Building Information", "Style:", "Land Information",
              "Building Other Features", "Dwelling Information", "Permit Information")
_SALE_SKIP = {"LAND + BLDG", "SALE OF MULTIPLE PARCELS", "LAND ONLY", "BUILDING ONLY"}


def _sales(lines):
    idxs = [i for i, l in enumerate(lines) if l == "Sales/Ownership History"]
    if not idxs:
        return []
    # bound the section: from the header to the first following section boundary
    start = idxs[0] + 1
    end = start
    while end < len(lines) and not any(lines[end].startswith(s) for s in _SALE_STOP):
        end += 1
    seg = lines[start:end]
    sales, i = [], 0
    while i < len(seg):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", seg[i]):
            price, grantee = None, None
            for k in range(i + 1, min(i + 8, len(seg))):
                if re.fullmatch(r"[\d,]+", seg[k]) and price is None:
                    price = int(seg[k].replace(",", ""))
                elif (re.search(r"[A-Za-z]{3}", seg[k]) and "/" not in seg[k]
                      and ":" not in seg[k] and seg[k] not in _SALE_SKIP):
                    grantee = seg[k]  # last name-like token in the row = grantee
            sales.append({"date": seg[i], "price": price, "grantee": grantee})
        i += 1
    return sales


_PERMIT_HDR = {"Date Issued", "Number", "Price", "Purpose", "% Complete", "Complete"}


def _permits(lines, limit=15):
    """Desktop layout: a flat table — header row, then rows of
    date / number / price / purpose (/ optional % complete). Split on each date token."""
    idxs = [i for i, l in enumerate(lines) if l == "Permit Information"]
    if not idxs:
        return []
    start = idxs[0] + 1
    end = start
    while end < len(lines) and lines[end] not in ("Sales/Ownership History", "Building Information",
                                                  "Land Information", "Dwelling Information"):
        end += 1
    toks = [t for t in lines[start:end] if t not in _PERMIT_HDR]

    # group tokens into records, splitting whenever a date appears
    permits, cur = [], None
    for t in toks:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
            if cur:
                permits.append(cur)
            cur = {"date": t, "rest": []}
        elif cur is not None:
            cur["rest"].append(t)
    if cur:
        permits.append(cur)

    out = []
    for p in permits:
        rest = p["rest"]
        number = rest[0] if rest else None
        # price = first numeric token AFTER the permit number (so a numeric permit number
        # isn't mistaken for the price)
        price = next((int(x.replace(",", "")) for x in rest[1:] if re.fullmatch(r"[\d,]+", x)), None)
        purpose = next((x for x in rest if re.search(r"[A-Za-z]", x) and not re.fullmatch(r"[\d,]+", x)
                        and x != number), None)
        out.append({"date": p["date"], "number": number, "price": price, "purpose": purpose})
    return out[:limit]


def _card_count(body):
    m = re.search(r'name="card_num"[^>]*>(.*?)</select>', body, re.S)
    if not m:
        return 1
    opts = re.findall(r'<option value="(\d+)"', m.group(1))
    return max(int(o) for o in opts) if opts else 1


def fetch_parcel(apn):
    """Return a structured record-card profile for one Springfield parcel."""
    body0 = _get(apn)
    lines0 = _clean(body0)
    n = _card_count(body0)

    total_sqft = 0
    buildings = []
    all_features = []
    for c in range(1, n + 1):
        lines = lines0 if c == 1 else _clean(_get(apn, c))
        st = _structure_type(lines)
        sqft = _building_sqft(lines) or _living_area(lines)  # commercial OR residential
        is_bldg = st and st.strip().upper() != "LAND"
        if is_bldg:
            total_sqft += sqft
            feats = _features(lines)
            all_features += feats
            detail = _detail(lines) or _construction(lines)  # residential OR commercial
            buildings.append({
                "card": c, "structure_type": st, "grade": _grade(lines),
                "year_built": _year_built(lines), "sqft": sqft,
                "detail": detail or None,
            })

    up = " ".join(all_features).upper()
    systems = {
        "sprinkler": "SPRINKLER" in up,
        "elevator_count": sum(1 for f in all_features if "ELEVATOR" in f.upper()),
        "loading_docks": sum(1 for f in all_features if "LOAD DOCK" in f.upper()),
        "overhead_doors": sum(1 for f in all_features if "OVERHEAD" in f.upper()),
    }

    rooms = _room_detail(lines0)  # residential only; empty for commercial
    return {
        "apn": apn,
        "situs": _field(lines0, "Situs:"),
        "use_class": _field(lines0, "Class:"),
        "zoning": _field(lines0, "Zoning:"),
        "total_acres": _field(lines0, "Total Acres:"),
        "value_flag": _field(lines0, "Value Flag:"),  # e.g. INCOME APPROACH for income property
        "assessment": _assessment(lines0),
        "building_count": len(buildings),
        "total_building_sqft": total_sqft,
        "buildings": buildings,
        "systems": systems,
        "room_detail": rooms or None,
        "sales": _sales(lines0),
        "permits": _permits(lines0),
    }


if __name__ == "__main__":
    import json
    apn = sys.argv[1] if len(sys.argv) > 1 else "031700053"
    print(json.dumps(fetch_parcel(apn), indent=2))
