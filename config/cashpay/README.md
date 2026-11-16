# Independent cash-pay seed (Arizona)

`seed_providers.json` is the source of truth the app reads (`src/cashpay.py`); the CSV is
the same rows flattened for humans. Generated 2026-11-15 by `build_seed.py` (kept outside
this repo) from each provider's public price page, PDF, MDsave listing, or cash program.

Rules the app enforces when showing these rows:
- every price shows its source link and `date_checked`; when the source URL or notes carry
  their own document date (e.g. the AZ Advanced Imaging PDF uploaded Feb 2025) that is shown too
- `source_type: mdsave` rows are labeled "prepaid bundle", not cash
- `price_status: from_price` rows render as "from $X"; `call_for_price` rows render with no number
- providers are never merged or deduplicated by address (RadiologyAssist and SimonMed
  19th Avenue share 6707 N 19th Ave on purpose)
- ordering is by price, then data quality (exact code > typical code > family rate); no paid placement

To refresh: regenerate the JSON, drop it here, restart the app. Corrections from the review
queue (`tools/review_queue.py`) are applied by editing this file, never automatically.
