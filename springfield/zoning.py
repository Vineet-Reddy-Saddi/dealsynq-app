"""
Springfield zoning lookup — GENERAL: given a zoning code or district name, return the
consolidated ordinance detail the city splits across many pages: the district's purpose,
its dimensional/intensity limits (height, coverage, setbacks, lot size), and which uses
are permitted / need a special permit / are prohibited there.

The point (per the 2026-07 client call): a property's assessor card just says "B3". This
turns that code into "Business C — Central Business District: downtown high-rise, 95% max
coverage, 400 ft height, retail permitted by right, restaurants by special permit, …" —
all sourced from the City of Springfield Zoning Ordinance (Articles 3, 4, 5).

Accepts any of: assessor record-card codes (B1, B3, C1, R2, I2…), Springfield WebGIS
ZONE_NAME values ("Business C", "Residence A"…), or ordinance names ("Bus C", "Business C").

    from springfield.zoning import lookup
    z = lookup("B3")          # -> full district profile dict, or None if unknown

CLI:  python springfield/zoning.py B3
"""
import json
import os
import sys

_DATA = None


def _data():
    global _DATA
    if _DATA is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zoning_data.json")
        with open(path, encoding="utf-8") as f:
            _DATA = json.load(f)
    return _DATA


def resolve(code_or_name):
    """Map any accepted code/name to a canonical district key, or None."""
    if not code_or_name:
        return None
    key = str(code_or_name).strip().upper()
    cmap = _data()["code_map"]
    if key in cmap:
        return cmap[key]
    # tolerate minor variants: strip trailing punctuation, collapse spaces
    key2 = " ".join(key.replace("-", " ").split())
    for k, v in cmap.items():
        if " ".join(k.replace("-", " ").split()) == key2:
            return v
    return None


# order uses are shown in each bucket
_TIER_ORDER = {"Y": 0, "D": 1, "1": 2, "T": 3, "2": 4, "3": 5, "N": 9}


def uses_for(district_key):
    """Return {permitted, special_permit, prohibited} lists of use names for a district,
    plus the raw code per use. `permitted` = by right (Y) or state-exempt (D);
    `special_permit` = any tiered review (T/1/2/3); `prohibited` = N."""
    d = _data()["districts"].get(district_key)
    if not d:
        return None
    col = d.get("use_column")
    permitted, special, prohibited = [], [], []
    for u in _data()["uses"]:
        code = (u["codes"].get(col) or "").upper()
        row = {"use": u["use"], "category": u.get("category"), "code": code}
        if code in ("Y", "D"):
            permitted.append(row)
        elif code in ("T", "1", "2", "3"):
            special.append(row)
        elif code == "N":
            prohibited.append(row)
    keyf = lambda r: (_TIER_ORDER.get(r["code"], 8), r["use"])
    return {"permitted": sorted(permitted, key=keyf),
            "special_permit": sorted(special, key=keyf),
            "prohibited": sorted(prohibited, key=lambda r: r["use"])}


def lookup(code_or_name):
    """Full consolidated zoning profile for a code/name, or None if not recognized."""
    key = resolve(code_or_name)
    if not key:
        return None
    d = _data()["districts"][key]
    return {
        "query": code_or_name,
        "district_key": key,
        "district_name": d["name"],
        "group": d["group"],
        "purpose": d["purpose"],
        "dimensional": {k: v for k, v in (d.get("dimensional") or {}).items() if v is not None},
        "uses": uses_for(key),
        "code_legend": _data()["code_legend"],
        "source": _data()["meta"]["source"],
        "source_url": _data()["meta"]["source_url"],
        "ordinance_current_as_of": _data()["meta"]["ordinance_current_as_of"],
    }


def _fmt_dim(dim):
    labels = {
        "min_lot_sf": "Min lot area", "min_lot_acres": "Min lot area",
        "min_lot_sf_sf_dwelling": "Min lot / single-family", "min_lot_sf_per_apt_unit": "Min lot / apartment unit",
        "min_frontage_ft": "Min frontage", "min_lot_width_ft": "Min lot width",
        "front_yard_min_ft": "Front yard (min)",
        "side_yard_abut_residential_ft": "Side yard (abuts residential)",
        "side_yard_abut_nonresidential_ft": "Side yard (abuts non-res)",
        "side_yard_min_ft": "Side yard (min)",
        "rear_yard_abut_residential_ft": "Rear yard (abuts residential)",
        "rear_yard_abut_nonresidential_ft": "Rear yard (abuts non-res)",
        "rear_yard_min_ft": "Rear yard (min)",
        "max_stories": "Max height (stories)", "max_height_ft": "Max height",
        "max_building_coverage_pct": "Max building coverage",
        "max_building_coverage_pct_residential": "Max coverage (residential)",
        "max_residential_density_du_per_acre": "Max residential density",
    }
    unit = {"min_lot_sf": " sf", "min_lot_acres": " ac", "min_lot_sf_sf_dwelling": " sf",
            "min_lot_sf_per_apt_unit": " sf", "min_frontage_ft": " ft", "min_lot_width_ft": " ft",
            "front_yard_min_ft": " ft", "side_yard_abut_residential_ft": " ft",
            "side_yard_abut_nonresidential_ft": " ft", "side_yard_min_ft": " ft",
            "rear_yard_abut_residential_ft": " ft", "rear_yard_abut_nonresidential_ft": " ft",
            "rear_yard_min_ft": " ft", "max_height_ft": " ft",
            "max_building_coverage_pct": "%", "max_building_coverage_pct_residential": "%",
            "max_residential_density_du_per_acre": " du/ac"}
    out = []
    for k, v in dim.items():
        out.append(f"    {labels.get(k, k):32} {v}{unit.get(k, '')}")
    return "\n".join(out)


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "B3"
    z = lookup(q)
    if not z:
        print(f"No Springfield zoning district recognized for {q!r}.")
        sys.exit(1)
    print(f"{q!r}  ->  {z['district_name']}  ({z['group']})")
    print(f"\nPurpose: {z['purpose']}")
    if z["dimensional"]:
        print("\nDimensional & intensity limits:")
        print(_fmt_dim(z["dimensional"]))
    u = z["uses"]
    if u:
        print(f"\nPermitted by right ({len(u['permitted'])}): " +
              ", ".join(r["use"] for r in u["permitted"]))
        print(f"\nBy special permit ({len(u['special_permit'])}): " +
              ", ".join(f"{r['use']} [{r['code']}]" for r in u["special_permit"]))
        print(f"\nNot allowed ({len(u['prohibited'])}): " +
              ", ".join(r["use"] for r in u["prohibited"]))
    print(f"\nSource: {z['source']} ({z['ordinance_current_as_of']})")
    print(z["source_url"])
