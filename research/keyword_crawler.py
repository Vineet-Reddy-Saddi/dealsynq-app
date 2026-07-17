"""
DealSynq — Address-driven web research crawler.

General tool (address is the only per-run input). Given a property address (and,
optionally, a known name / owner), it:

  1. Expands the address into ~55 keyword search queries spanning every category in
     the DealSynq "Data Sources" taxonomy (sale/listing, tenants, ownership,
     financials, permits, zoning, environmental, legal, tax, news, market, ...).
  2. Runs each query through DuckDuckGo's server-rendered HTML endpoints
     (html.duckduckgo.com + lite.duckduckgo.com) — NOT Google, which blocks
     scrapers — pacing requests and rotating User-Agents so the source IP is not
     rate-limited/blocked.
  3. Dedupes every result across all queries into one ranked list of websites,
     categorized, with the queries that surfaced each site.

Output: a single JSON document (and an optional Markdown report) — "every website
that comes up" for a property, as a first-pass data dump the enrichment pipeline
can then target. This is the deliverable Rahul asked for on the 2026-07 call.

Engines (a cascade, tried in order until one returns results for a query):
  1. DuckDuckGo /html/  — best index, no JS, no key. What Rahul suggested. Its only
     downside is an anomaly/rate-limit page after bursts, so we pace it.
  2. DuckDuckGo /lite/  — same index, lighter page; a second chance when /html/ soft-blocks.
  3. Mojeek           — an independent crawler (its own index, not a Google/Bing proxy),
     very scraper-tolerant; smaller index, so it's the fallback, not the primary.
Google is deliberately NOT used (it blocks scrapers); Bing strips organic links for
non-JS clients. This cascade means one engine throttling never stops the crawl — the
next engine covers that query, and the run records which engine surfaced each site.

IP-block hygiene (this is the part that matters):
  * one query at a time, with a jittered delay between queries (default ~2-4s),
  * rotating desktop User-Agents,
  * exponential backoff, then engine fallback, on a soft block,
  * optional --proxies to rotate the shared Decodo pool (axisgis/proxy_config.json).
Run modestly and it behaves like a person doing 55 searches, not a scraper.

Usage:
    python -u research/keyword_crawler.py "380 Cooley St, Springfield, MA" \
        --name "Five Town Plaza" --out fivetownplaza/RESEARCH.json --report

    python -u research/keyword_crawler.py "1391 Main St, Springfield, MA"     # quick
    python -u research/keyword_crawler.py "<addr>" --per-query 20 --pace 3     # gentler
    python -u research/keyword_crawler.py "<addr>" --proxies                   # rotate IPs
"""
import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests
import urllib3

urllib3.disable_warnings()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY_FILE = os.path.join(ROOT_DIR, "axisgis", "proxy_config.json")

HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
LITE_ENDPOINT = "https://lite.duckduckgo.com/lite/"
MOJEEK_ENDPOINT = "https://www.mojeek.com/search"

# Rotate a small set of realistic desktop UAs so 55 queries don't all look identical.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
]

# ---------------------------------------------------------------------------
# 1. Query generation — the DealSynq "Data Sources" taxonomy, as search modifiers.
#    Each category maps to short phrases appended to the quoted address. Kept broad
#    enough to surface the yellow-highlighted high-value items (on/off-market,
#    broker, asking price, offering memorandum) AND the public-record categories.
# ---------------------------------------------------------------------------
CATEGORY_TERMS = {
    "Sale / Listing": [
        "for sale", "sold", "asking price", "offering memorandum",
        "investment sale", "LoopNet", "Crexi",
    ],
    "Brokers": [
        "CBRE", "JLL", "Cushman Wakefield", "Colliers", "Marcus Millichap",
    ],
    "Tenants / Leasing": [
        "tenants", "for lease", "leasing", "anchor tenant", "vacancy", "retailers",
    ],
    "Ownership / Entity": [
        "owner", "acquired", "acquisition", "sold to", "REIT",
    ],
    "Financials": [
        "cap rate", "NOI", "rent roll", "valuation",
    ],
    "Permits / Construction": [
        "building permit", "renovation", "construction",
    ],
    "Zoning / Planning": [
        "zoning", "site plan", "planning board", "special permit", "development",
    ],
    "Environmental": [
        "environmental contamination", "underground storage tank", "brownfield", "MassDEP",
    ],
    "Legal": [
        "lawsuit", "litigation", "bankruptcy", "lien", "foreclosure",
    ],
    "Tax": [
        "property tax", "tax lien", "delinquent taxes", "tax appeal",
    ],
    "News / Activity": [
        "news", "press release", "grand opening", "closing", "expansion",
    ],
    "Market / Location": [
        "demographics", "traffic count", "comparable sales", "shopping center",
    ],
}


def _split_address(address):
    """Split "380 Cooley St, Springfield, MA 01128" into ("380 Cooley St", "Springfield MA").

    Search engines return far more for a quoted STREET plus loose city/state than for
    the whole comma-punctuated string quoted as one phrase (tested: ~10x more hits)."""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) >= 2:
        street = parts[0]
        # join the rest, drop a trailing zip so it stays a loose locality match
        rest = " ".join(parts[1:])
        rest = re.sub(r"\b\d{5}(-\d{4})?\b", "", rest).strip()
        return street, rest
    return address.strip(), ""


def generate_queries(address, name=None):
    """Build the ordered list of ~55-70 search queries for a property.

    Each query quotes the street address and appends a loose city/state + a category
    modifier: `"380 Cooley St" Springfield MA for sale`. When a name/owner is known, a
    handful of `"<name>" <modifier>` queries are added for the highest-value categories,
    since the entity name (e.g. "Five Town Plaza", "Phillips Edison") surfaces listings,
    SEC filings and news the raw street address never will.
    """
    street, locality = _split_address(address)
    loc = f" {locality}" if locality else ""
    queries = []
    seen = set()

    def add(q, category, via):
        key = q.lower()
        if key in seen:
            return
        seen.add(key)
        queries.append({"query": q, "category": category, "via": via})

    # bare street+locality + a generic "property" variant (broadest possible surface)
    add(f'"{street}"{loc}', "General", "address")
    add(f'"{street}"{loc} property', "General", "address")

    for category, terms in CATEGORY_TERMS.items():
        for term in terms:
            add(f'"{street}"{loc} {term}', category, "address")

    # name-driven queries for the categories where the entity name is the real key
    if name:
        nm = name.strip()
        add(f'"{nm}"', "General", "name")
        for category in ("Sale / Listing", "Tenants / Leasing", "Ownership / Entity",
                         "Financials", "News / Activity"):
            for term in CATEGORY_TERMS[category][:2]:
                add(f'"{nm}" {term}', category, "name")

    return queries


# ---------------------------------------------------------------------------
# 2. DuckDuckGo search — server-rendered HTML, decoded, de-redirected.
# ---------------------------------------------------------------------------
_RESULT_A = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S)
_SNIPPET = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(?P<snip>.*?)</a>', re.S)
# lite endpoint: plain result links in a table
_LITE_A = re.compile(r'<a[^>]+class="result-link"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S)
# mojeek: <a class="title" ... href="...">Title</a>
_MOJEEK_A = re.compile(r'<a class="title"[^>]*href="(?P<href>https?://[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S)
_TAG = re.compile(r"<[^>]+>")


def _strip_tags(s):
    return html.unescape(_TAG.sub("", s or "")).strip()


def _decode_ddg_href(href):
    """DDG wraps outbound links as //duckduckgo.com/l/?uddg=<encoded>&... — unwrap."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs:
            return urllib.parse.unquote(qs["uddg"][0])
    return href


def _looks_blocked(text):
    if not text or len(text) < 400:
        return True
    low = text.lower()
    return ("anomaly" in low and "traffic" in low) or "if this error persists" in low


def _parse_ddg_results(text):
    out = []
    titles = list(_RESULT_A.finditer(text))
    snips = list(_SNIPPET.finditer(text))
    for i, m in enumerate(titles):
        url = _decode_ddg_href(m.group("href"))
        if not url.startswith("http"):
            continue
        snip = _strip_tags(snips[i].group("snip")) if i < len(snips) else ""
        out.append({"title": _strip_tags(m.group("title")), "url": url, "snippet": snip})
    if not out:  # try the lite layout
        for m in _LITE_A.finditer(text):
            url = _decode_ddg_href(m.group("href"))
            if url.startswith("http"):
                out.append({"title": _strip_tags(m.group("title")), "url": url, "snippet": ""})
    return out


def _parse_mojeek_results(text):
    out = []
    for m in _MOJEEK_A.finditer(text):
        url = m.group("href")
        if url.startswith("http"):
            out.append({"title": _strip_tags(m.group("title")), "url": url, "snippet": ""})
    return out


def _headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://duckduckgo.com/",
    }


# Short timeout so a dead/slow proxy IP fails fast and we rotate to a fresh one, rather
# than burning 20s per bad proxy. Good proxies respond in 1-3s.
TIMEOUT = 9


def _get(endpoint, *, method, params=None, data=None, proxy=None):
    if method == "post":
        return requests.post(endpoint, data=data, headers=_headers(),
                             proxies=proxy, timeout=TIMEOUT, verify=False)
    return requests.get(endpoint, params=params, headers=_headers(),
                       proxies=proxy, timeout=TIMEOUT, verify=False)


def _ddg_engine(endpoint):
    """Build a DDG engine (html or lite). When a proxy pool is given, retry across a few
    fresh IPs on timeout/soft-block before giving up — a rotated IP usually clears the
    202 anomaly page. Direct (no pool) tries once, since the IP won't change."""
    def engine(query, proxies_pool):
        attempts = 4 if proxies_pool else 1
        last = None
        for _ in range(attempts):
            proxy = random.choice(proxies_pool) if proxies_pool else None
            try:
                r = _get(endpoint, method="post", data={"q": query, "kl": "us-en"}, proxy=proxy)
                if r.status_code in (202, 403, 429):
                    last = f"HTTP {r.status_code}"
                    continue
                if _looks_blocked(r.text):
                    last = "anomaly/soft-block"
                    continue
                results = _parse_ddg_results(r.text)
                if results:
                    return results, None
                last = "no results parsed"
            except requests.RequestException as e:
                last = str(e)[:80]
        return [], last
    return engine


_engine_ddg_html = _ddg_engine(HTML_ENDPOINT)
_engine_ddg_lite = _ddg_engine(LITE_ENDPOINT)


def _engine_mojeek(query, proxies_pool):
    attempts = 3 if proxies_pool else 1
    last = None
    for _ in range(attempts):
        proxy = random.choice(proxies_pool) if proxies_pool else None
        try:
            r = _get(MOJEEK_ENDPOINT, method="get", params={"q": query}, proxy=proxy)
            if r.status_code in (403, 429):
                last = f"HTTP {r.status_code}"
                continue
            results = _parse_mojeek_results(r.text)
            if results:
                return results, None
            last = "no results"
        except requests.RequestException as e:
            last = str(e)[:80]
    return [], last


ENGINES = [("ddg", _engine_ddg_html), ("ddg-lite", _engine_ddg_lite), ("mojeek", _engine_mojeek)]


def search_one(query, per_query=25, proxies_pool=None, base_sleep=0.6):
    """Search one query across the engine cascade until one returns results.

    Returns (results, engine_name, reason). `engine_name` is the engine that produced
    the results (for provenance), or None if all engines came up empty. Never raises.
    """
    reasons = []
    for name, engine in ENGINES:
        results, reason = engine(query, proxies_pool)
        if results:
            for res in results:
                res["engine"] = name
            return results[:per_query], name, None
        reasons.append(f"{name}:{reason}")
        time.sleep(base_sleep + random.uniform(0, 1.0))  # brief pause before next engine
    return [], None, "; ".join(reasons)


# ---------------------------------------------------------------------------
# 3. Crawl orchestration — run all queries, dedupe, categorize, rank.
# ---------------------------------------------------------------------------
def _norm_url(url):
    """Canonicalize a URL for dedup: drop scheme, 'www.', trailing slash, tracking qs."""
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower().lstrip("www.")
    path = p.path.rstrip("/")
    # drop common tracking params
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
            if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref", "_ga"))]
    q = urllib.parse.urlencode(keep)
    return f"{host}{path}" + (f"?{q}" if q else "")


def _domain(url):
    return urllib.parse.urlparse(url).netloc.lower().lstrip("www.")


# Search-engine chrome / ad redirects that are not real result sites.
_SKIP_HOSTS = ("duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com",
               "mojeek.com", "www.mojeek.com", "bing.com", "www.bing.com")


def _is_junk(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    return host in _SKIP_HOSTS or "/y.js" in url


def crawl(address, name=None, per_query=25, pace=(2.0, 4.0), proxies_pool=None,
          progress_cb=None, max_queries=None):
    """Run the full research crawl for one property.

    progress_cb(done, total, query, n_found) is called after each query so a UI can
    show live progress. Returns the assembled research document (a dict).
    """
    queries = generate_queries(address, name)
    if max_queries:
        queries = queries[:max_queries]
    total = len(queries)

    by_url = {}          # norm_url -> aggregated result record
    errors = []
    engines_used = {}
    started = time.time()

    for i, q in enumerate(queries, 1):
        results, engine, reason = search_one(q["query"], per_query=per_query,
                                             proxies_pool=proxies_pool)
        if reason and not results:
            errors.append({"query": q["query"], "reason": reason})
        if engine:
            engines_used[engine] = engines_used.get(engine, 0) + 1
        for rank, res in enumerate(results):
            if _is_junk(res["url"]):
                continue
            nu = _norm_url(res["url"])
            if not nu:
                continue
            rec = by_url.get(nu)
            if rec is None:
                rec = {
                    "url": res["url"],
                    "domain": _domain(res["url"]),
                    "title": res["title"],
                    "snippet": res["snippet"],
                    "categories": [],
                    "found_by": [],
                    "engines": [],
                    "hits": 0,
                    "best_rank": rank,
                }
                by_url[nu] = rec
            rec["hits"] += 1
            rec["best_rank"] = min(rec["best_rank"], rank)
            if q["category"] not in rec["categories"]:
                rec["categories"].append(q["category"])
            if q["query"] not in rec["found_by"]:
                rec["found_by"].append(q["query"])
            if res.get("engine") and res["engine"] not in rec["engines"]:
                rec["engines"].append(res["engine"])
            if not rec["snippet"] and res["snippet"]:
                rec["snippet"] = res["snippet"]

        if progress_cb:
            progress_cb(i, total, q["query"], len(results))
        # polite pacing between queries (skip after the final one)
        if i < total:
            time.sleep(random.uniform(*pace))

    # rank: sites surfaced by more queries first, then by best result rank
    sources = sorted(by_url.values(),
                     key=lambda r: (-r["hits"], r["best_rank"], r["domain"]))

    # group by category (a site can appear in several)
    categories = {}
    for r in sources:
        for c in r["categories"]:
            categories.setdefault(c, []).append(r["url"])

    # domain rollup
    dom = {}
    for r in sources:
        d = dom.setdefault(r["domain"], {"domain": r["domain"], "count": 0,
                                         "categories": set(), "sample_url": r["url"]})
        d["count"] += 1
        d["categories"].update(r["categories"])
    by_domain = sorted(
        ({"domain": d["domain"], "count": d["count"],
          "categories": sorted(d["categories"]), "sample_url": d["sample_url"]}
         for d in dom.values()),
        key=lambda d: (-d["count"], d["domain"]))

    return {
        "address": address,
        "property_name": name,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": "DuckDuckGo (html+lite) with Mojeek fallback",
        "engines_used": engines_used,
        "elapsed_seconds": round(time.time() - started, 1),
        "query_count": total,
        "queries": [q["query"] for q in queries],
        "unique_url_count": len(sources),
        "unique_domain_count": len(by_domain),
        "categories": categories,
        "by_domain": by_domain,
        "sources": sources,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 4. Markdown report (human-readable "document of every website").
# ---------------------------------------------------------------------------
def to_markdown(doc):
    L = []
    L.append(f"# Web research — {doc.get('property_name') or doc['address']}")
    L.append("")
    L.append(f"- **Address:** {doc['address']}")
    if doc.get("property_name"):
        L.append(f"- **Name:** {doc['property_name']}")
    L.append(f"- **Generated:** {doc['generated_at']}  ·  **Engine:** {doc['engine']}")
    L.append(f"- **Queries run:** {doc['query_count']}  ·  "
             f"**Unique sites:** {doc['unique_url_count']}  ·  "
             f"**Unique domains:** {doc['unique_domain_count']}  ·  "
             f"**Time:** {doc['elapsed_seconds']}s")
    if doc.get("errors"):
        L.append(f"- **Queries with no results (soft-blocked/empty):** {len(doc['errors'])}")
    L.append("")

    L.append("## Top domains")
    L.append("")
    L.append("| Domain | Times surfaced | Categories |")
    L.append("|---|---:|---|")
    for d in doc["by_domain"][:30]:
        L.append(f"| {d['domain']} | {d['count']} | {', '.join(d['categories'])} |")
    L.append("")

    L.append("## Sites by category")
    for cat, urls in doc["categories"].items():
        L.append("")
        L.append(f"### {cat} ({len(urls)})")
        for u in urls[:25]:
            L.append(f"- {u}")
    L.append("")

    L.append("## All unique sites (ranked)")
    L.append("")
    for r in doc["sources"]:
        cats = ", ".join(r["categories"])
        L.append(f"- **[{r['title'] or r['domain']}]({r['url']})** — "
                 f"{r['domain']} · surfaced {r['hits']}× · {cats}")
        if r["snippet"]:
            L.append(f"  - {r['snippet'][:220]}")
    L.append("")
    return "\n".join(L)


def load_proxies():
    """Load the proxy pool. Prefers the DEALSYNQ_PROXY_CONFIG env var (JSON string) so a
    deployed host supplies credentials as a secret; falls back to the local file for dev."""
    env = os.environ.get("DEALSYNQ_PROXY_CONFIG")
    if env:
        cfg = json.loads(env)
    elif os.path.exists(PROXY_FILE):
        cfg = json.load(open(PROXY_FILE, encoding="utf-8"))
    else:
        raise SystemExit("proxies requested but neither DEALSYNQ_PROXY_CONFIG env var "
                         f"nor {PROXY_FILE} is present.")
    user, pw = cfg.get("username", ""), cfg.get("password", "")
    pool = []
    for hostport in cfg.get("proxies", []):
        url = f"http://{user}:{pw}@{hostport}" if user else f"http://{hostport}"
        pool.append({"http": url, "https": url})
    return pool


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Address-driven web research crawler (DuckDuckGo).")
    ap.add_argument("address", help="Property address (the only required input)")
    ap.add_argument("--name", default=None, help="Known property/owner name (adds name-based queries)")
    ap.add_argument("--per-query", type=int, default=25, help="Max results kept per query")
    ap.add_argument("--pace", type=float, default=3.0, help="Avg seconds between queries (jittered)")
    ap.add_argument("--max-queries", type=int, default=None, help="Cap number of queries (for a quick test)")
    ap.add_argument("--proxies", action="store_true", help="Rotate the Decodo proxy pool")
    ap.add_argument("--out", default=None, help="Write the JSON document to this path")
    ap.add_argument("--report", action="store_true", help="Also write a .md report next to --out")
    args = ap.parse_args()

    pool = load_proxies() if args.proxies else None
    if pool:
        print(f"[proxies] rotating across {len(pool)} IPs", file=sys.stderr)

    def _progress(done, total, query, n):
        print(f"  [{done:>2}/{total}] {n:>2} results  ·  {query}", file=sys.stderr)

    pace = (max(0.5, args.pace - 1.0), args.pace + 1.0)
    doc = crawl(args.address, name=args.name, per_query=args.per_query, pace=pace,
                proxies_pool=pool, progress_cb=_progress, max_queries=args.max_queries)

    print(f"\n{doc['unique_url_count']} unique sites across {doc['unique_domain_count']} "
          f"domains from {doc['query_count']} queries in {doc['elapsed_seconds']}s "
          f"({len(doc['errors'])} empty).", file=sys.stderr)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        json.dump(doc, open(args.out, "w", encoding="utf-8"), indent=2)
        print(f"wrote {args.out}", file=sys.stderr)
        if args.report:
            md = args.out.rsplit(".", 1)[0] + ".md"
            open(md, "w", encoding="utf-8").write(to_markdown(doc))
            print(f"wrote {md}", file=sys.stderr)
    else:
        print(json.dumps(doc, indent=2))
