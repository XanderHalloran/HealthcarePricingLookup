# Healthcare price transparency + bill-negotiation coaching

Paste medical billing codes from an itemized hospital bill, get back the Medicare
benchmark, local hospital cash/negotiated prices, a suggested target ask, and a
specific negotiation talking point per code.

> **Estimates only.** Not a guarantee of bill reduction, and not legal, medical,
> or financial advice. Every output surface repeats this.

## Status
Phases 1–4 done. Phase 5 (deploy) not started — gated on explicit go-ahead.

## Web app (Phase 4)
```
python src/pipeline.py                              # populate data/hospital_rates first
uvicorn app:app --reload --app-dir src              # http://127.0.0.1:8000
```
Paste codes (one per line, charge optional, e.g. `73721 MRI $2,400`), optional state,
get a per-code pricing table + copy-able negotiation talking points. Disclaimer on page.
Single template `src/web/templates/index.html`; no auth, no build step.

## Ingest (Phase 2)
```
python src/pipeline.py        # reads config/hospitals.yaml, writes data/ + reports/
```
- `src/mrf_parser.py` — defensive CMS v3.0.0 tall-CSV parser (skiprows=2).
- `src/ingest.py` — parse → normalize → filter to allowlist → aggregate per code →
  append partitioned Parquet (`state`/`code`). Dropped codes go to `reports/`.
- `src/medicare.py` — PFS/OPPS/IPPS loaders into `medicare_rates`.
- `src/pipeline.py` — manifest-driven fetch (urllib) + ingest driver.
- `config/hospitals.yaml` — MRF manifest. `config/medicare.yaml` — rate params.
Run tests: `for t in tests/test_*.py; do python "$t"; done` (or `pytest`).

## Layout
```
config/
  allowlist.yaml      filter-before-store list: only these codes are kept at ingest
src/
  schema.sql          DuckDB DDL (procedures, medicare_rates, hospitals) + Parquet spec
  normalize.py        billing-code normalizer (the load-bearing messy part)
tests/
  test_normalize.py   normalizer tests (pytest OR `python tests/test_normalize.py`)
data/                 (gitignored) partitioned Parquet lands here
reports/              (gitignored) dropped/unmatched-code reports from ingest
```

## Data model
- **procedures**(code, code_type[CPT|HCPCS|DRG], description, category, is_shoppable)
- **medicare_rates**(code, code_type, locality, rate, year, source[PFS|OPPS|IPPS])
- **hospitals**(id, name, state, ccn, lat, lng)
- **hospital_rates** → Parquet under `data/hospital_rates/`, partitioned by `state`
  then `code`: (hospital_id, state, code, code_type, cash_price, negotiated_min,
  negotiated_median, negotiated_max, payer_optional). Queried with DuckDB; no DB server.

Full column details and types are in [src/schema.sql](src/schema.sql).

## Normalization spec
Consumer-pasted bills mix CPT, HCPCS, DRG, and revenue codes with descriptions
and extra columns. `normalize_code(line)` returns one classified code per line:

`NormalizedCode(raw, code, code_type, matched, note)`

Steps:
1. Strip + uppercase.
2. Scan tokens (split on whitespace / `,` `|` `;` tab) left-to-right; take the
   **first** token matching a code pattern — handles code-first and description-first.
3. Strip a trailing 2-char modifier (`J1234-RT` → `J1234`), recorded in `note`.
4. Classify:

   | pattern | type | notes |
   |---------|------|-------|
   | `\d{5}` | CPT | Category I |
   | `\d{4}[FT]` | CPT | Category II / III |
   | `[A-V]\d{4}` | HCPCS | Level II |
   | `0\d{3}` | REV | revenue code (4-digit, leading zero) |
   | `\d{1,3}` | DRG | MS-DRG; bare numerics are ambiguous |
   | else | UNKNOWN | |

5. Revenue codes name a department, not a service — only the few in `REV_TO_CPT`
   resolve to a CPT; the rest get `matched=False` for the dropped-codes report.

`matched=False` is deliberate, not a failure: gaps and ambiguities are surfaced
(in `note`) rather than silently dropped, because a gap is useful negotiation info.

**Known ambiguity:** a bare 3-digit number (e.g. `450`) could be an MS-DRG or a
non-zero-padded revenue code. We assume DRG and flag it. Upgrade path: use
surrounding context / a code dictionary lookup if real bills make this common.

## Running tests (needs Python 3.10+ installed)
```
python tests/test_normalize.py      # or: pytest
```
> ⚠️ No Python interpreter is installed on the current machine (only Windows
> Store stubs). Tests are written but unrun until Python is available.

## Adding a metro / procedure page, and email
- **Procedure SEO page**: add `slug: my-procedure` to the code's row in `config/allowlist.yaml`
  -> `/<state>/<metro>/my-procedure` exists for every live metro and lands in the sitemap.
- **Metro**: add a `metros:` entry in `config/metros.yaml` (key, state, label, slug_city, ids) and the
  hospitals in `config/hospitals.yaml` (`tools/add_state.py` does both from a research report).
- **Email me this letter/comparison**: set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`,
  `SMTP_FROM` (see DEPLOY.md). Unset = feature hidden. Captures land in `data/capture.sqlite`.

