"""
Hampden County Registry of Deeds — browser-driven name search (GENERAL: any name).

Why this exists alongside hampden_deeds_pipeline.py
---------------------------------------------------
The registry sits behind **Incapsula (Imperva)** bot protection. As of 2026-07-15, plain
HTTP is fully blocked — verified:
  * direct from a residential IP  -> Incapsula JS challenge (the "works from home" note in
    hampden_deeds_pipeline.py is now STALE)
  * rotating datacenter proxies   -> JS challenge stub
  * Playwright cookies replayed into `requests` -> HTTP 403 (Incapsula fingerprints the
    client, not just the cookie, so cookie-lifting does NOT work)
  * one browser context reused for several searches -> first request 200, the rest 403
    (it rate-limits the session hard)

What DOES work (measured):
  **a fresh browser context per lookup, paced ~25s apart** -> HTTP 200 with full results,
  every time. That is exactly what this module does.

Because each lookup costs a browser launch (~6s) plus the pacing gap, this is a
**background job + cache** tool, never something to call inline in a web request. Every
owner is fetched once and cached by the caller; the pacing lock below makes concurrent
callers queue instead of tripping the rate limit.

Scope note (honest): the registry indexes by **NAME, not address**. For an LLC owner
(typical for commercial property) the result is effectively that property's record. For an
individual it may return nothing (name-format mismatch) or records spanning several
properties. Label results as "documents recorded under this owner", not "for this parcel".

    from deeds.hampden_browser import fetch_records
    records = fetch_records("FIVE TOWN STATION LLC")   # -> [{document_type, book_page, ...}]

CLI:
    python -u deeds/hampden_browser.py "COOLEY STREET ASSOCIATES LLC"
    python -u deeds/hampden_browser.py "W & M REALTY INC" --json
"""
import argparse
import html as html_mod
import json
import os
import re
import sys
import threading
import time
import urllib.parse

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
from deeds.hampden_deeds_pipeline import _parse_results, PARTY_CODES  # noqa: E402  reuse the parser

BASE = "https://search.hampdendeeds.com/ALIS/WW400R.HTM"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Incapsula rate-limits per session/IP: back-to-back lookups get 403 even from a real
# browser. ~25s between lookups tested clean; keep a global lock so concurrent callers
# queue rather than race.
MIN_GAP_SECONDS = 25
_PACE_LOCK = threading.Lock()
_last_fetch = [0.0]


def _search_url(name, party="all", doc_type="*ALL", years="AY"):
    params = {
        "W9SNM": name, "W9GNM": "", "W9IXTP": PARTY_CODES.get(party, "A"),
        "W9ABR": doc_type, "W9INQ": years, "AYVAL": "1948", "CYVAL": "2020",
        "W9FDTA": "", "W9TDTA": "", "WSHTNM": "WW401R00", "WSIQTP": "LR01LP",
        "WSKYCD": "N", "WSWVER": "2",
    }
    return BASE + "?" + urllib.parse.urlencode(params)


def _browser_get(url, timeout=60000, settle_ms=4000):
    """Load `url` in a fresh headless browser context and return the settled HTML.

    A fresh context per call is deliberate: reusing one gets the 2nd+ request 403'd.
    Uses Playwright's SYNC api, so call this from a plain thread (never inside an
    asyncio loop)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        # --no-sandbox: required to run Chromium as root inside a container.
        # --disable-dev-shm-usage / --single-process / --disable-gpu: keep memory low so it
        # survives a small (512MB) cloud instance. These are harmless locally too.
        browser = pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
            "--disable-gpu", "--single-process", "--no-zygote",
        ])
        try:
            ctx = browser.new_context(user_agent=UA)
            page = ctx.new_page()
            resp = page.goto(url, wait_until="networkidle", timeout=timeout)
            page.wait_for_timeout(settle_ms)          # let the Incapsula JS challenge run
            html = page.content()
            status = resp.status if resp else None
            if "Document Type" not in html:           # challenge may still be resolving
                page.wait_for_timeout(3000)
                html = page.content()
            return html, status
        finally:
            browser.close()


def fetch_records(name, party="all", doc_type="*ALL", years="AY"):
    """Return every document indexed under `name` — deeds, mortgages, discharges, liens,
    easements, leases. Returns [] if the name has no records.

    Raises RuntimeError if the registry blocked us (so callers can surface it honestly
    rather than show an empty list as if it meant "no debt").
    """
    url = _search_url(name, party=party, doc_type=doc_type, years=years)
    with _PACE_LOCK:                       # serialize + pace all callers
        gap = MIN_GAP_SECONDS - (time.time() - _last_fetch[0])
        if gap > 0:
            time.sleep(gap)
        html, status = _browser_get(url)
        _last_fetch[0] = time.time()

    if status == 403 or ("_Incapsula_Resource" in html and len(html) < 3000):
        raise RuntimeError(f"registry blocked the request (HTTP {status}) — Incapsula "
                           "rate-limit; retry after a pause")
    if "Document Type" not in html:
        raise RuntimeError(f"unexpected registry response (HTTP {status}, {len(html)} bytes)")
    records = _parse_results(html)
    # the shared parser strips tags but leaves HTML entities ("SAGON, SHIRLEY M (&amp;O)")
    for rec in records:
        for k, v in rec.items():
            if isinstance(v, str):
                rec[k] = html_mod.unescape(v).strip()
    return records


def _year(rec):
    """Parse the MM-DD-YYYY 'date_received' into a sortable date, or None."""
    import datetime
    try:
        return datetime.datetime.strptime(rec.get("date_received") or "", "%m-%d-%Y").date()
    except Exception:
        return None


def summarize(records):
    """Roll the raw index rows up into the picture a CRE analyst actually wants.

    Deliberately does NOT claim "outstanding debt". A discharge is normally recorded with
    the LENDER as grantor, so it can sit under the bank's name rather than the owner's —
    meaning "no discharge under this name" is NOT evidence the loan is open. The honest,
    useful signal is the MOST RECENT mortgage: a 2026 mortgage matters, a 1986 one is
    almost certainly long satisfied. We report recency and let the reader judge.
    """
    def w(rec, *words):
        """Whole-WORD match on the document type. Crucial: substring matching miscounts —
        'RELEASE' contains 'LEASE', 'Release of Lien' contains 'LIEN', etc. Word boundaries
        stop that ('\\bLEASE\\b' does not match inside 'RELEASE')."""
        t = (rec.get("document_type") or "").upper()
        return any(re.search(r"\b" + re.escape(x) + r"\b", t) for x in words)

    # Instruments whose wording FLIPS the meaning of the base document — a subordination,
    # assignment, release, discharge or surrender of a mortgage/lease is not itself a new
    # mortgage/lease. These are the exclusions that keep the counts honest.
    FLIP = ("DISCHARGE", "RELEASE", "ASSIGNMENT", "SUBORDINAT", "SUBORDINATION",
            "SURRENDER", "TERMINATION", "SATISFACTION", "PARTIAL")

    mortgages = [r for r in records if w(r, "MORTGAGE") and not w(r, *FLIP)]
    # a payoff instrument recorded under the OWNER: an actual discharge, or a
    # release/satisfaction OF a mortgage (real evidence the loan was paid).
    discharges = [r for r in records if w(r, "DISCHARGE")
                  or (w(r, "RELEASE", "SATISFACTION") and w(r, "MORTGAGE"))]
    deeds_ = [r for r in records if w(r, "DEED") and not w(r, "TRUST")]
    # a release/discharge/dissolution OF a lien is the opposite of adding one — exclude it
    liens = [r for r in records if w(r, "LIEN", "ATTACHMENT", "EXECUTION", "LIS")
             and not w(r, "RELEASE", "DISCHARGE", "DISSOLUTION", "SATISFACTION")]
    # an Assignment of Lease/Rents is loan collateral, a Surrender/Termination ends a lease
    # — neither is a lease. Only genuine LEASE / NOTICE / MEMORANDUM instruments count.
    leases = [r for r in records if w(r, "LEASE") and not w(r, *FLIP)]
    easements = [r for r in records if w(r, "EASEMENT")]

    import datetime
    today = datetime.date.today()

    def _newest(items):
        d = sorted(((_year(r), r) for r in items if _year(r)), key=lambda t: t[0])
        if not d:
            return None, None
        rec = d[-1][1]
        return rec, today.year - _year(rec).year

    latest, latest_age = _newest(mortgages)
    latest_lien, lien_age = _newest(liens)
    return {
        "total": len(records),
        "counts": {
            "deeds": len(deeds_), "mortgages": len(mortgages), "discharges": len(discharges),
            "liens": len(liens), "leases": len(leases), "easements": len(easements),
        },
        "latest_mortgage": ({"date": latest.get("date_received"),
                             "lender": latest.get("reverse_party"),
                             "book_page": latest.get("book_page"),
                             "age_years": latest_age} if latest else None),
        # newest lien + its age: a lien is only a live concern if recent. Federal tax liens
        # self-release ~10yr; most others resolve within a decade, so an old one is very
        # likely stale — surfaced so the UI never red-flags a decades-old lien as active.
        "latest_lien": ({"date": latest_lien.get("date_received"),
                         "type": latest_lien.get("document_type"),
                         "party": latest_lien.get("reverse_party"),
                         "age_years": lien_age} if latest_lien else None),
        "mortgage_dates": [r.get("date_received") for r in mortgages],
        "discharges_found": len(discharges),
        "has_any_lien": bool(liens),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Hampden Registry of Deeds — browser name search")
    ap.add_argument("name")
    ap.add_argument("--party", choices=["all", "grantor", "grantee"], default="all")
    ap.add_argument("--doctype", default="*ALL")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    recs = fetch_records(args.name, party=args.party, doc_type=args.doctype)
    if args.json:
        print(json.dumps({"name": args.name, "summary": summarize(recs), "records": recs}, indent=2))
    else:
        s = summarize(recs)
        print(f"\n{args.name} — {s['total']} recorded document(s)")
        print(f"  {s['counts']}")
        lm = s["latest_mortgage"]
        print(f"  most recent mortgage: {lm['date']} to {lm['lender']}" if lm
              else "  no mortgage recorded")
        print()
        for r in recs:
            arrow = "<-" if r["party_role"] == "grantee" else "->"
            print(f"  {str(r['date_received']):<11} {str(r['document_type']):<22} "
                  f"book/page {str(r['book_page']):<12} {arrow} {r['reverse_party']}")
