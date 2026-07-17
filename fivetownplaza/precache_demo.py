"""
Pre-cache the featured demo addresses so they load instantly and never depend on a live
scrape/crawl during a presentation. Writes into fivetownplaza/webapp/precache/, which the
server loads at startup (see load_precache() in server.py).

For each address it saves:
  * enrich_<APN>.json    — the assessor record-card enrichment (fast)
  * deeds_<slug>.json    — Registry of Deeds documents for the owner (~30s, browser-driven)
  * research_<slug>.json — the deep web-research doc (slow; only for commercial demos)

The point: the demo's own example buttons become bulletproof. The flagship (Five Town
Plaza) is already cached separately via PROFILE.json + RESEARCH.json.

Usage:
    python -u fivetownplaza/precache_demo.py                 # enrichment for all + research for commercial
    python -u fivetownplaza/precache_demo.py --no-research   # enrichment only (fast)
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import importlib.util

spec = importlib.util.spec_from_file_location("srv", os.path.join(ROOT, "fivetownplaza", "webapp", "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

# (address to type, run web research too?)
DEMOS = [
    ("380 Cooley St", False),   # the flagship — research already cached in RESEARCH.json;
                                # this pass gives its deeds card (independent "no mortgage" proof)
    ("415 Cooley St", True),    # supermarket — commercial
    ("1391 Main St", True),     # B3 downtown — commercial/office
    ("115 Cooley St", False),   # single-family home — enrichment only
]


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-research", action="store_true", help="skip the slow web-research crawl")
    ap.add_argument("--no-deeds", action="store_true", help="skip the Registry of Deeds lookup")
    ap.add_argument("--per-query", type=int, default=20)
    ap.add_argument("--max-queries", type=int, default=44)
    args = ap.parse_args()

    os.makedirs(srv.PRECACHE_DIR, exist_ok=True)
    srv.load()  # populate PARCELS / BY_OWNER
    pool = srv.RESEARCH_PROXIES
    print(f"proxies: {'yes ('+str(len(pool))+' IPs)' if pool else 'none — direct'}\n")

    for addr, do_research in DEMOS:
        res = srv.search(addr)
        if not res.get("matched"):
            print(f"[skip] {addr}: no match")
            continue
        anchor = res["anchor_address"]
        apn = res["extra_params"]["apn"] if res.get("extra_params") else None
        owner = res.get("owner", "")
        print(f"== {addr}  ->  {anchor}  (APN {apn}, owner {owner[:30]})")

        # 1) record-card enrichment
        if apn:
            data = srv.fetch_parcel(apn)
            if data:
                out = os.path.join(srv.PRECACHE_DIR, f"enrich_{apn}.json")
                json.dump(data, open(out, "w", encoding="utf-8"), indent=1)
                print(f"   wrote {os.path.basename(out)}  (use_class={data.get('use_class')})")

        # 2) Registry of Deeds for this owner (browser-driven; ~30s incl. pacing)
        if owner and not args.no_deeds:
            try:
                recs = srv.deeds_fetch(owner)
                doc = {"owner": owner, "summary": srv.deeds_summarize(recs), "records": recs,
                       "source": "Hampden County Registry of Deeds",
                       "fetched_at": __import__("time").strftime("%Y-%m-%d")}
                out = os.path.join(srv.PRECACHE_DIR, f"deeds_{_slug(owner)}.json")
                json.dump(doc, open(out, "w", encoding="utf-8"), indent=1)
                print(f"   wrote {os.path.basename(out)}  ({doc['summary']['total']} docs, "
                      f"{doc['summary']['counts']['mortgages']} mortgage(s))")
            except Exception as e:
                print(f"   [deeds] {owner[:30]}: {e}")

        # 3) deep web research (commercial only, and only if requested)
        if do_research and not args.no_research:
            def prog(done, total, q, n):
                print(f"      research [{done:>2}/{total}]  {n:>2}  {q}", file=sys.stderr)
            doc = srv.research_crawl(anchor, name=(owner or None), per_query=args.per_query,
                                     pace=(0.8, 1.8), proxies_pool=pool, progress_cb=prog,
                                     max_queries=args.max_queries)
            out = os.path.join(srv.PRECACHE_DIR, f"research_{_slug(anchor)}.json")
            json.dump(doc, open(out, "w", encoding="utf-8"), indent=1)
            print(f"   wrote {os.path.basename(out)}  ({doc['unique_url_count']} sites, {doc['elapsed_seconds']}s)")
        print()

    print("done. Restart the server to load the pre-cache.")


if __name__ == "__main__":
    main()
