# Hospital price transparency toolkit

Hospitals are required to publish every price they charge, including what they have
negotiated with each insurer. They publish it as enormous, wildly inconsistent machine-readable
files that almost nobody can read. This project ingests those files and turns them into
something a person can act on.

One codebase runs two live sites from the same engine, switched by a config directory:

| | [healthcare.traqqit.com](https://healthcare.traqqit.com) | ortho.traqqit.com |
|---|---|---|
| For | patients comparing prices and negotiating a bill | contracting and network strategy |
| Profile | `APP_CONFIG=config` (default) | `APP_CONFIG=config-ortho` |
| Markets | 6 states, 27 metros, 333 hospitals | 5 states, 29 metros, 299 hospitals |
| Tracked codes | 107 shoppable procedures | 82 musculoskeletal procedures |
| Access | public | analysis pages behind HTTP basic auth |

> **Estimates, not quotes.** Published prices are frequently stale, mislabeled, or
> internally contradictory, and this tool says so rather than hiding it. Nothing here is
> legal, medical, or financial advice. Every output surface repeats this.

## Quick start

```bash
pip install -e .
python -u src/pipeline.py --state AZ          # fetch + ingest one state (a full run is hours)
uvicorn app:app --reload --app-dir src        # http://127.0.0.1:8000
APP_CONFIG=config-ortho uvicorn app:app --reload --app-dir src   # the ortho profile
```

The app serves fine with no data at all — every page degrades to "no data for this market"
rather than failing, so you can boot it first and ingest later.

## How it works

```
CMS machine-readable files          Medicare reference data
(CSV tall/wide, JSON, zipped)       (OPPS, PFS, CLFS, IPPS, ASC)
            |                                   |
      src/pipeline.py  ──fetch, stream──►  src/ingest.py
            |                                   |
     filter to the allowlist            normalize + aggregate
            |                                   |
            └──────────►  data/*.parquet  ◄─────┘
                      (hive-partitioned by state/code)
                                 |
                          src/app.py (FastAPI + DuckDB)
```

Everything streams. A single hospital file can be 900 MB of JSON or a 1.8 GB zip holding a
43 GB CSV, so nothing is ever read into memory whole, and only allowlisted codes are kept.
Writes are staged and swapped atomically per state partition, so a failed refresh leaves the
live dataset untouched.

There is no database server. DuckDB reads the Parquet files directly and holds the Medicare
reference tables in memory.

## Pages

Both profiles share the price lookup, per-procedure SEO pages, and a methodology page that
shows the live coverage numbers and a data-flow diagram. The consumer profile adds bill
analysis, negotiation talking points, and an appeal-letter generator. The ortho profile adds
a payer-rate matrix, a site-of-care comparison (hospital outpatient vs ambulatory surgery
center), an own-facility-vs-market position view, a payer scorecard as a multiple of Medicare,
episode/bundle pricing, and a radius map that takes a ZIP or address.

## Repo layout

```
src/
  pipeline.py     manifest-driven fetch; streams zips, handles bot-blocked hosts
  mrf_parser.py   CMS tall + wide CSV parsers (the formats vary enormously)
  mrf_json.py     streaming JSON parser (ijson), tolerant of control bytes and BOMs
  ingest.py       normalize -> filter -> aggregate per facility -> Parquet
  normalize.py    billing-code normalizer (the load-bearing messy part)
  medicare.py     OPPS/PFS/CLFS/IPPS/ASC benchmark loaders
  pricing.py      per-code lookup, outlier rejection, payer matrix
  analysis.py     bill issues, talking points, appeal letter
  app.py          FastAPI routes + Jinja templates
config/           consumer profile: hospitals, metros, allowlist, quality, geo
config-ortho/     ortho profile: same shape + brand.yaml, bundles.yaml, asc/ipps benchmarks
ops/              deployment helpers that live on the server
reference/        CMS fee-schedule CSVs, committed so the image needs no downloads
data/             (gitignored) Parquet lands here
```

`config-ortho/brand.yaml` is what makes a profile: its presence switches the branding,
navigation, and copy, and sets which facilities are treated as "ours".

## Adding a market or a procedure page

- **State**: append to `hospitals.yaml`, `metros.yaml`, `geo.yaml`, and `quality.yaml`, then
  run `python -u src/pipeline.py --state XX`. `tools/add_state.py` applies a research report
  to all four files at once.
- **Procedure page**: add `slug: my-procedure` to the code's row in `allowlist.yaml` and
  `/<state>/<metro>/my-procedure` exists for every live metro and enters the sitemap.
- **Bot-blocked hospitals**: some hosts refuse datacenter IPs entirely. Those manifest entries
  use `file: data/raw/<ID>.csv` instead of `url:`, and the file is fetched elsewhere and
  copied in. See DEPLOY.md.

## Code normalization

Pasted bills mix CPT, HCPCS, DRG, and revenue codes with descriptions and stray columns.
`normalize_code(line)` returns one classified code per line: it scans tokens left to right,
takes the first that matches a code pattern (so code-first and description-first both work),
strips a trailing modifier, and classifies:

| pattern | type | notes |
|---------|------|-------|
| `\d{5}` | CPT | Category I |
| `\d{4}[FT]` | CPT | Category II / III |
| `[A-V]\d{4}` | HCPCS | Level II |
| `0\d{3}` | REV | revenue code |
| `\d{1,3}` | DRG | MS-DRG |

Unmatched lines are kept and flagged rather than dropped, because a gap is itself useful
information when you are questioning a bill. A bare 3-digit number is genuinely ambiguous
between an MS-DRG and a revenue code; it is assumed to be a DRG and flagged.

## Tests

Each suite is a standalone runner with no framework:

```bash
python tests/test_normalize.py          # one suite
for t in tests/test_*.py; do python "$t"; done
```

`tests/test_ortho.py` must run standalone, because it sets `APP_CONFIG` before importing the
app. One test runs `node --check` over the page's inline JavaScript, after a bad escape once
broke search silently for hours.

## Deployment

Both sites run as containers behind a shared Caddy reverse proxy, refreshed weekly by systemd
timers that ingest one state at a time. See [DEPLOY.md](DEPLOY.md).
