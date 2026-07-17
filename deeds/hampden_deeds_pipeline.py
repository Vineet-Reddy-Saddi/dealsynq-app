"""
Hampden County Registry of Deeds (search.hampdendeeds.com) — name search scraper.

General tool: given ANY grantor/grantee name (person or corporation/LLC), returns every
recorded document indexed under that name — deeds, mortgages, discharges, liens,
easements, leases, financing statements, etc. Not specific to any one property; the name
is the only input that varies per run.

System: "ALIS" (Cott Systems-style) land records platform. The disclaimer text server-side
even mentions "Norfolk County Registry of Deeds" verbatim on Hampden's own page — strong
evidence this same platform is reused, white-labeled, across multiple MA county registries.
Worth checking search.<county>deeds.com for the same URL/param shape when this needs to
generalize to another county.

No login required; full public index access. GET-only, no session/cookie needed.

Anti-bot note (IMPORTANT — verified 2026-07-12): search.hampdendeeds.com sits behind
**Incapsula (Imperva)** bot-protection. A home/residential IP passes and returns the full
results page, but gets rate-limited (HTTP 403) after sustained access. Rotating DATACENTER
proxies does NOT help here: Incapsula serves those IPs a 212-byte JS challenge stub instead
of the results. So:
  - Occasional single lookups: run DIRECT (no proxy) from a residential IP; pace slowly.
  - At scale, this site needs EITHER (a) residential/ISP proxies (not the cheap datacenter
    pool), OR (b) a Playwright step that solves the Incapsula JS challenge once, grabs the
    cookie, and reuses it (the same pattern the Barnstable Cloudflare scraper uses).
The --proxies flag still rotates the datacenter pool and is useful on rate-limit-only
registries (the ALIS/20-20 systems on other MA counties may differ) — but it will report a
clear "Incapsula challenge" error on this specific host rather than fail silently.

Usage:
    python -u deeds/hampden_deeds_pipeline.py "FIVE TOWN STATION LLC"            # direct
    python -u deeds/hampden_deeds_pipeline.py "FIVE TOWN STATION LLC" --proxies  # rotated
    python -u deeds/hampden_deeds_pipeline.py "FIVE TOWN STATION LLC" --party grantor --json
"""
import argparse
import json
import os
import random
import re
import sys
import urllib.parse

import requests
import urllib3

urllib3.disable_warnings()  # proxied HTTPS to a gov host: silence verify warnings

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY_FILE = os.path.join(ROOT_DIR, "axisgis", "proxy_config.json")

BASE = "https://search.hampdendeeds.com/ALIS/WW400R.HTM"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
TIMEOUT = 15  # short so dead/slow proxy IPs fail fast and the retry budget rotates on

# W9IXTP party-role filter: A=All parties, R=Grantors/Mortgagors, E=Grantees/Mortgagees
PARTY_CODES = {"all": "A", "grantor": "R", "grantee": "E"}


def load_proxies():
    """Load the shared Decodo pool (same file/shape the other DealSynq scrapers use)."""
    if not os.path.exists(PROXY_FILE):
        raise SystemExit(f"--proxies requested but {PROXY_FILE} not found.")
    with open(PROXY_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    user, pw = cfg.get("username", ""), cfg.get("password", "")
    pool = []
    for hostport in cfg.get("proxies", []):
        url = f"http://{user}:{pw}@{hostport}" if user else f"http://{hostport}"
        pool.append({"http": url, "https": url})
    return pool


def _block_reason(resp):
    """Return a short reason string if this response is a block/challenge, else None."""
    if resp.status_code in (403, 429):
        return f"HTTP {resp.status_code} rate-limit"
    if "_Incapsula_Resource" in resp.text or "Incapsula" in resp.text:
        return "Incapsula JS challenge (bot protection)"
    if len(resp.text) < 2000:
        return f"stub page ({len(resp.text)} bytes)"
    return None


def _fetch(url, proxies=None, tries=15):
    """GET url. If a proxy pool is given, rotate to a fresh IP on each block/failure."""
    last = None
    saw_incapsula = False
    attempts = tries if proxies else 1
    for _ in range(attempts):
        proxy = random.choice(proxies) if proxies else None
        try:
            r = requests.get(url, headers=HEADERS, proxies=proxy, timeout=TIMEOUT, verify=False)
            reason = _block_reason(r)
            if reason:
                last = reason
                if "Incapsula" in reason:
                    saw_incapsula = True
                continue
            return r.text
        except requests.RequestException as e:
            last = e
    hint = ""
    if saw_incapsula:
        hint = ("\n  NOTE: this registry is behind Incapsula bot-protection, which challenges "
                "datacenter proxy IPs. Datacenter proxies do NOT beat it. Options: (1) residential "
                "proxies, or (2) a Playwright browser step to solve the JS challenge and reuse the "
                "cookie (same approach used for the Cloudflare-protected Barnstable scraper).")
    raise RuntimeError(f"fetch failed after {attempts} attempt(s): {last}{hint}")


def search_by_name(name, party="all", doc_type="*ALL", years="AY", proxies=None):
    """Search the recorded-land name index. Returns (records, query_url).

    party: "all" | "grantor" | "grantee"
    doc_type: "*ALL" or a specific code (e.g. "MTG", "*DD" deed group, "*LN" lien group)
    years: "AY" (all years, 1948+), "CY" (current years), "1Y" (12mo index), "50" (50yr index)
    proxies: None for direct, or a pool from load_proxies() for rotated access.
    """
    params = {
        "W9SNM": name,
        "W9GNM": "",
        "W9IXTP": PARTY_CODES.get(party, "A"),
        "W9ABR": doc_type,
        "W9INQ": years,
        "AYVAL": "1948",
        "CYVAL": "2020",
        "W9FDTA": "",
        "W9TDTA": "",
        "WSHTNM": "WW401R00",
        "WSIQTP": "LR01LP",
        "WSKYCD": "N",
        "WSWVER": "2",
    }
    url = BASE + "?" + urllib.parse.urlencode(params)
    body = _fetch(url, proxies=proxies)
    return _parse_results(body), url


def _parse_results(body):
    tables = re.findall(r"<table.*?</table>", body, re.S | re.I)
    records = []
    for t in tables:
        rows = re.findall(r"<tr.*?</tr>", t, re.S | re.I)
        if not rows:
            continue
        header_cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", rows[0], re.S | re.I)
        header = [re.sub("<[^>]+>", "", c).strip() for c in header_cells]
        if "Document Type" not in header or "Book (page)" not in header:
            continue  # not the results table
        for row in rows[1:]:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
            if len(cells) < 7:
                continue
            clean = [re.sub("<[^>]+>", "", c).replace("&nbsp;", "").strip() for c in cells]
            name_raw = clean[0]
            m = re.match(r"^(.*?)\s*\((&O)?\s*\)?\s*\((Gtor|Gtee)\)\s*$", name_raw)
            role = None
            party_name = name_raw
            m2 = re.search(r"\((Gtor|Gtee)\)\s*$", name_raw)
            if m2:
                role = {"Gtor": "grantor", "Gtee": "grantee"}[m2.group(1)]
                party_name = name_raw[: m2.start()].strip()
            party_name = re.sub(r"\(&O\)\s*$", "", party_name).strip()
            records.append(
                {
                    "party_name": party_name,
                    "party_role": role,
                    "reverse_party": clean[1] or None,
                    "town": clean[2] or None,
                    "date_received": clean[3] or None,
                    "document_type": clean[4] or None,
                    "document_desc": clean[5] or None,
                    "book_page": clean[6] or None,
                }
            )
        break
    return records


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="Grantor/grantee name or corporation/LLC name to search")
    ap.add_argument("--party", choices=["all", "grantor", "grantee"], default="all")
    ap.add_argument("--doctype", default="*ALL")
    ap.add_argument("--json", action="store_true", help="print raw JSON instead of a table")
    ap.add_argument("--proxies", action="store_true",
                    help="rotate through the Decodo proxy pool (for scale / to beat the IP block)")
    args = ap.parse_args()

    pool = load_proxies() if args.proxies else None
    if pool:
        print(f"[proxies] rotating across {len(pool)} IPs", file=sys.stderr)
    records, url = search_by_name(args.name, party=args.party, doc_type=args.doctype, proxies=pool)
    if args.json:
        print(json.dumps({"query_url": url, "records": records}, indent=2))
    else:
        print(f"Query: {url}\n")
        if not records:
            print("No records found.")
        for rec in records:
            print(
                f"{rec['date_received']:<11} {rec['document_type']:<20} "
                f"{rec['document_desc']:<24} book/page {rec['book_page']:<12} "
                f"{'<-' if rec['party_role']=='grantee' else '->'} {rec['reverse_party']}"
            )
        print(f"\n{len(records)} record(s)")
