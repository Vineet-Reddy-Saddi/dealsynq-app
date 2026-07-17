# research/ — Address-driven web research crawler

The deliverable Rahul asked for on the 2026-07 call: *"search a property address, get a
document of every website that comes up"* across all the DealSynq data-source categories.

## What it does

`keyword_crawler.py` takes **one input — a property address** (optionally a known
name/owner) and:

1. **Expands** it into ~55–70 keyword search queries spanning the full DealSynq
   "Data Sources" taxonomy — sale/listing, brokers, tenants/leasing, ownership,
   financials, permits, zoning, environmental, legal, tax, news, market.
   (`"380 Cooley St" Springfield MA for sale`, `… offering memorandum`, `… MassDEP`, …)
2. **Searches** each query through a resilient engine cascade:
   **DuckDuckGo `/html/` → DuckDuckGo `/lite/` → Mojeek**. Google is deliberately not
   used (it blocks scrapers); Bing strips organic links for non-JS clients. One engine
   throttling never stops the crawl — the next covers that query.
3. **Dedupes** every result across all queries into one ranked list of websites,
   tagged by category, with the queries + engine that surfaced each, plus a top-domains
   rollup.

Output: a single `RESEARCH.json` document (and an optional Markdown report) — a
first-pass **discovery list of where a property's data lives**, which the enrichment
pipeline can then target. It is not verified/extracted data; it's the map.

## Not getting your IP blocked (important)

DuckDuckGo serves an anomaly/rate-limit page after bursts. The crawler defends against it:

- one query at a time, **jittered delay between queries** (`--pace`, default ~3s),
- **rotating desktop User-Agents**,
- backoff + **engine fallback** on a soft block,
- **`--proxies`** rotates the shared Decodo pool (`axisgis/proxy_config.json`). This is
  the reliable path at any real volume: it gives DuckDuckGo fresh IPs and keeps *your*
  (or a demo host's) IP unflagged. Verified working — proxied runs return the
  high-value LoopNet / broker / SEC sources cleanly.

## Usage

```bash
# full run for a property, save JSON + Markdown report, via proxies (recommended)
python -u research/keyword_crawler.py "380 Cooley St, Springfield, MA" \
    --name "Five Town Plaza" --proxies --out fivetownplaza/RESEARCH.json --report

python -u research/keyword_crawler.py "1391 Main St, Springfield, MA"   # quick, direct
python -u research/keyword_crawler.py "<addr>" --max-queries 20 --pace 4 # gentle/short
```

Importable: `from research.keyword_crawler import crawl, generate_queries, to_markdown`.
`crawl(...)` takes a `progress_cb(done, total, query, n)` so a UI can show live progress.

## Wired into the web app

The demo web app (`fivetownplaza/webapp/server.py`) exposes this as a **"Deep Web
Research"** section:

- `GET /api/research?q=<addr>&name=<name>` — returns a cached result instantly
  (flagship Five Town Plaza, pre-built into `fivetownplaza/RESEARCH.json`) or starts a
  background job and returns a job id.
- `GET /api/research/status?job=<id>` — progress + final result the page polls.

Live runs route through the proxy pool and are query-capped so a viewer can't flag the
host IP. The section renders a source count, top domains, category filter chips, the
ranked source list, and a **source citation** ("DuckDuckGo/Mojeek · N queries · date —
first-pass automated sweep, not yet verified").

## Next steps (phase 2, per the call)

- Actually fetch/scrape the discovered pages (with JS execution where needed) into a
  per-property **data dump**, then factorize fields across properties so the portfolio
  is queryable ("all properties with X").
- Targeted parsers for the recurring high-value hosts this surfaces (LoopNet/Crexi
  listings, SEC EDGAR, county registry, municipal permit portals).
