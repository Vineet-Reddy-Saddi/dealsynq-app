# DealSynq — MA Town Property Data Pipelines

Collects parcel geometry, zoning, and assessor/appraisal data for Massachusetts towns.
Pure Python (no Node) — `requirements`: `aiohttp`, `pandas`.

## Folder layout

```
DealSynq/
├── axisgis/              AxisGIS pipeline (active work — ~104 towns)
│   ├── axisgis_pipeline.py      main pipeline: geometry + assessor + zoning → CSV
│   ├── axisgis_town_slugs.json  104 AxisGIS town slugs
│   ├── classify_towns.py        diagnostic: classify towns as BULK vs SCRAPE
│   ├── proxy_config.json        proxy pool + credentials (PRIVATE — do not commit)
│   ├── proxy_config.example.json template
│   └── previous_versions/       archived (e.g. axisgis_hero.js — old Playwright scraper)
├── massgis/             MassGIS GDB pipeline (done)
│   └── massgis_pipeline.py
├── vendor_census/       Which assessor/GIS vendor each of 351 MA towns uses (done)
│   ├── ma_vendor_census_v3.py / _v3.csv   latest
│   ├── ma_town_websites.txt               input: town names + URLs
│   └── previous_versions/                 v1, v2 (superseded)
├── outputs/             all *_axisgis.csv output files
├── checkpoints/         per-town scraper checkpoints ({town}_pipeline_checkpoint.json)
└── README.md
```

## Running the AxisGIS pipeline

```
python -u axisgis/axisgis_pipeline.py              # all 104 towns
python -u axisgis/axisgis_pipeline.py SHUTESBURY   # one town
python -u axisgis/axisgis_pipeline.py HADLEY BOLTON # several
```

Per town it auto-detects the data path:
- **BULK** — town exposes a joined assessor table on the ArcGIS server → pulled in a few
  bulk queries (seconds, no proxy, no rate limit).
- **SCRAPE** — no join → per-parcel fetch over plain HTTP (`aiohttp`), 50 concurrent,
  rotating every request across the proxy pool in `axisgis/proxy_config.json`.

Output: `outputs/{town}_axisgis.csv` (geometry WKT + all raw assessor fields).
Checkpoints in `checkpoints/` make runs resumable. Run one pipeline process at a time.

## Proxies

`axisgis/proxy_config.json` holds the proxy pool. Datacenter proxies are sufficient
(AxisGIS rate-limits by IP but doesn't fingerprint or block datacenter ranges). Copy
`proxy_config.example.json` and fill in credentials to set up.
