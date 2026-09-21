# Deployment — healthcare.traqqit.com

## Enabling the AI features (one key turns on both)
`ANTHROPIC_API_KEY` gates two features; without it both return a graceful "not enabled"
notice and nothing else breaks:
- **AI negotiation coach** (`/ask`, `src/assistant.py`) — answers free-text questions
  grounded in the analyzed bill using **Claude Fable 5** (`claude-fable-5`) with an Opus 4.8
  server-side refusal-fallback, and a minimal-call retry if the SDK/params drift.
- **Bill-photo / EOB OCR** (`/eob`) — OCRs a bill photo or PDF (`claude-haiku-4-5`) to
  pre-fill the bill textarea.

To turn them on:
```
ssh ... root@85.31.233.230
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> /root/healthcare/.env   # compose reads ${ANTHROPIC_API_KEY}
cd /root/healthcare && ANTHROPIC_API_KEY=sk-ant-... docker compose up -d   # or export it first
```
⚠️ Both are public endpoints hitting a paid API (EOB ~$0.01–0.03/upload; `/ask` a few
cents/question on Fable). Add a per-IP cap before promoting widely. `/ask` already caps
the question at 1000 chars and `/eob` at a 12 MB file.

## Multi-state markets (added 2026-09-09)
- The dataset always partitioned by `state`; the app now exposes it. `config/metros.yaml`
  has a `states:` registry (`code`, `name`, `slug`) and every metro carries `state:` (+
  `promote: true` for the metros that go in the sitemap/home footer). A state appears in the
  home "Your state" selector only once it is listed there AND has hospitals in
  `config/hospitals.yaml` (the hospital count per state is derived from the manifest).
- URLs: `/` (+ `?state=CA`), `/card?...&state=CA`, `/<state-slug>/procedure/<code>`
  (e.g. `/california/procedure/73721`; Arizona keeps the bare `/procedure/<code>`), and
  `/<metro-key>/procedure/<code>` as before. Metro keys and state slugs share one URL segment,
  so keep them globally unique. A metro implies its state everywhere (`_analyze`).
- **Adding a state = data only**: manifest entries (`state: XX`) + metros/geo/quality ids, then
  redeploy and ingest ONLY that state's partition:
  `docker exec -d healthcare-app sh -c 'python src/pipeline.py --state XX > data/ingest_XX.log 2>&1; echo INGEST_DONE >> data/ingest_XX.log'`
  (`--state` wipes + rebuilds just `data/*/state=XX/`; the weekly timer's no-flag run still
  rebuilds everything). Both build into `data/*.build` and SWAP at the end, so the live site keeps
  serving the old dataset for the hours a big ingest takes. Raw MRFs over `RAW_CACHE_MAX_MB`
  (default 0 = keep nothing; zip members are STREAMED, never extracted) — raw files are deleted after ingest (disk, not bandwidth, is the
  constraint on the shared box) — they just re-download on the next refresh.
- A configured state shows up in the UI (selector, sitemap, `/<slug>/procedure/`) only once its
  `data/hospital_rates/state=XX` partition exists — no restart needed; states go live as their
  ingest lands. Rollout 2026-09-10: CA (92 hospitals), UT (33), NM (30), CO (41), TX (59) via
  `tools/add_state.py` from per-state research reports (wide-CSV layout now parsed — Intermountain,
  CHS, UNM...). Known gaps: files >2 GB (UC San Diego, U of Utah 43 GB, Community Fresno, Univ.
  Hospital SA, Ascension Seton 4 GB unzipped), bot-gated hosts (Texas Health Resources, CHLA).

## ortho.traqqit.com — second site, same codebase (added 2026-09-14)
- **Profile switch**: `APP_CONFIG=config-ortho` (+ `BASE_URL=https://ortho.traqqit.com`) makes the
  SAME image serve the MSK site: `config-ortho/` holds its own allowlist (~80 MSK CPT/HCPCS +
  MS-DRGs), Arizona-only hospitals/metros/geo/quality, `brand.yaml` (name/tagline; presence of this
  file = ortho profile: nav, hero, titles), `ipps.yaml` (MS-DRG weights x FY base rates ->
  IPPS benchmark), `asc.yaml` (Medicare ASC fee schedule -> OPPS-vs-ASC differential), and
  `freestanding.yaml` for ASCs with published prices (type: asc). The consumer site is untouched
  when brand.yaml is absent.
- **Container**: `ortho-app` from `docker-compose.ortho.yml` in `/root/ortho`, volume `ortho_data`,
  same Caddy network. Redeploy: build the tarball as for healthcare, scp to `/root/ortho.tgz`,
  `ssh ... 'bash -s' < ~/ortho-redeploy.sh`. Ingest: `docker exec -d ortho-app sh -c 'python -u src/pipeline.py > data/ingest.log 2>&1'`
  (APP_CONFIG is in the container env, so the pipeline reads config-ortho). Ortho states: AZ, VA, NV, MI, FL (289 hospitals); a full rebuild is several hours, so refresh one state at a time with `--state XX` (FL alone is ~2.5 h because of 48 HCA files).
- **Analysis pages** (all behind Caddy basic_auth `@internal`, creds `/root/ortho/basic_auth.txt`):
  `/rates` payer matrix, `/site-of-care`, `/position` (own facility vs market), `/payers` scorecard,
  `/bundles` episode pricing (config-ortho/bundles.yaml), `/map` facilities within a radius of an
  address/ZIP (Leaflet + OSM tiles; ZIP via zippopotam, address via Nominatim, both lru_cached),
  `/methodology`. The Area selector on every page is one `metro` param: a metro key, or a state
  code for "All <State>" (`_area()` in app.py). Adding a state to the ortho site = append to
  config-ortho hospitals/metros/geo/quality (tools/add_state.py targets config/, so pass the
  report through it with CFG pointed at config-ortho or append by hand), redeploy, then
  `docker exec -d ortho-app sh -c 'python -u src/pipeline.py --state XX > data/ingest-XX.log 2>&1'`.
- Weekly refresh: host `ortho-ingest.timer` (Tue 02:00 UTC).
- **Weekly refresh runs ONE STATE AT A TIME** (`/root/ingest-by-state.sh <container> <STATES...>`, called by both
  `healthcare-ingest.service` and `ortho-ingest.service`). A single full rebuild OOM-killed the box on 2026-09-21:
  3.8 GB RAM, no swap, and the kernel picked the largest process -- `uvicorn`, the web server -- so the SITE went
  down and the refresh died 2.5 h in having written nothing. Now each `--state XX` run does its own atomic partition
  swap, peak memory is bounded by one state, a failing state does not cost the others, and the pipeline sets
  `oom_score_adj=500` so any future squeeze kills the batch job instead of the site. The box also has a 4 GB swapfile.
  Add a state to the weekly run by appending its code to the unit's `ExecStart` line.
- **Bot-blocked MRFs (both sites).** Some hosts (Akamai: vhchealth.org, centrahealth.com, valleyhealthlink.com;
  Cloudflare: marywashingtonhealthcare.com; IP-range blocks: memorialhermann.org, mrfs.hyvehealthcare.com) refuse
  the VPS. Their manifest entries use `file: data/raw/<ID>.<ext>` instead of `url:`; the file is fetched
  off-box (a browser session works), copied in with `docker cp <file> <container>:/app/data/raw/`, then
  ingested with `--only <ID,...>`. `--only` APPENDS, so to refresh an id that already has rows delete its
  parquet files first (one hospital per file: query `read_parquet(..., filename=true) WHERE hospital_id IN (...)`
  and `os.remove` them). Files with `file:` are never auto-deleted by the raw-cache bound. The weekly timers
  re-read the local copies, so a stale `file:` stays stale until replaced by hand.
- **Internal pages** (`noindex`, basic-auth'd in the Caddy block): `/rates?code=&metro=&mine=`
  facility x payer contracted-rate matrix with market percentiles + `/rates.csv`; `/site-of-care?code=&metro=`
  HOPD vs ASC with Medicare's own differential and per-case shift savings. Quality overlay: `quality.yaml`
  `hip_knee:` (Care Compare COMP_HIP_KNEE by hospital id) shows under Quality in every facility table.
- Caddy block (appended to `/root/travelmap/Caddyfile`, reload via stdin): `ortho.traqqit.com { encode gzip;
  @internal path /rates* /site-of-care*; basic_auth @internal { hopco <bcrypt> }; reverse_proxy ortho-app:8200 }`.
  Credentials live in `/root/ortho/basic_auth.txt` (chmod 600).
- Tests: `python tests/test_ortho.py` (standalone — sets APP_CONFIG before importing app).

## Top-procedure SEO pages + "email me this" (added 2026-09-11)
- **Readable SEO pages**: allowlist entries with a `slug:` (the 10 most-shopped procedures:
  mri-brain 70551, mri-lumbar-spine 72148, ct-abdomen-pelvis 74177, colonoscopy 45378, mammogram
  77067, ultrasound-abdomen 76700, er-visit 99284, knee-arthroscopy 29881, cataract-surgery 66984,
  vaginal-delivery 59400) render at `/<state-slug>/<metro-key>/<slug>` (e.g.
  `/arizona/phoenix/mri-brain`) via the SAME `_procedure_page` builder as `/<metro>/procedure/<code>`;
  the code URL canonicalizes to the slug URL, and `related` links become the other 9 in that metro.
  The sitemap lists slug pages for every LIVE metro. **Add a procedure**: add `slug:` to its
  allowlist row. **Add a metro**: `config/metros.yaml` (+ hospital ids) - pages appear once the
  state's partition exists. A configured state with no data (Nevada scaffold: `las-vegas`) is
  hidden from the selector and its pages return an honest "coming soon" **404**, never fake prices.
- **Soft email capture** (`src/capture.py`, `POST /email`): optional "Email me this letter /
  comparison" cards after bill results and under the shop cards. Never gates anything; one
  transactional email per submission; separate default-off tips checkbox is only RECORDED
  (no drip exists). Store = `data/capture.sqlite` (in the volume; `CAPTURE_DB` to move it).
  Per-IP limit `CAPTURE_RATE_PER_HOUR` (5). Mail = stdlib smtplib; env `SMTP_HOST`, `SMTP_PORT`
  (465 implicit TLS, else STARTTLS), `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` in `/root/healthcare/.env`
  (compose passes them through; unset -> the cards are hidden and `/email` says "not enabled").
  Prod uses the fleet Hostinger SMTP (support@traqqit.com, same creds as accounts-pb).
  QA: `python tests/test_capture.py` + `tests/test_app.py`; live: submit the card, expect
  "Sent to ...", check `sqlite3 data/capture.sqlite 'select * from captures'`.

## Metro filter + SEO landing pages (added 2026-07-28)
- **Metro filter** (`config/metros.yaml`): each hospital_id → one of 4 metros (phoenix,
  tucson, northern, southern). The "Your area" selector on the home page + `&metro=` on
  `/card`/`/analyze` restrict the price range, fair-price median, target, AND facility list
  to that metro (apples-to-apples) — else statewide. Pure query-time filter in
  `pricing.lookup` (filter breakdown to metro ids, recompute range from the survivors);
  **no re-ingest** to change metro membership, just edit the YAML + redeploy.
- **SEO** for ads: programmatic landing pages. `/procedure/{code}` (statewide) and
  `/{metro}/procedure/{code}` (e.g. `/tucson/procedure/73721`) — same template, metro-scoped
  title/H1/FAQ/canonical. Each page has JSON-LD (MedicalProcedure + BreadcrumbList + FAQPage),
  OG/Twitter meta, visible FAQ, and sibling-metro + related-procedure internal links. Home has
  WebApplication JSON-LD + OG + an SEO footer link grid. `sitemap.xml` lists statewide + the
  two dense metros (phoenix, tucson) × every code (~321 URLs; rural metro pages still resolve
  + are crawlable via on-page links). `robots.txt` points at the sitemap. **Ad flow**: point a
  "MRI cost Tucson" ad at `/tucson/procedure/73721`. USER TODO: verify the domain in Google
  Search Console + submit the sitemap.
- **Social/ad card**: `og:image` (summary_large_image) → `/static/og.png`, a committed
  1200×630 PNG generated by `tools/make_og.py` (Pillow, DEV-ONLY — production just serves the
  static file via the `/static` StaticFiles mount; no runtime Pillow). Regenerate after a copy
  change: `python tools/make_og.py`. Same card on home + every procedure page.
- **Landing-page quality** (for Google Ads): home hero has a trust strip (free/no-signup/
  official-data/AI-coach) + a 3-step "how it works" section (`.how`) above the tool.

## Distance + outlier handling (added 2026-07-28)
- **Distance/#2**: `config/geo.yaml` = per-hospital city-centroid `[lat,lon]`. `/card?...&lat=&lon=`
  adds a per-facility distance (haversine); the UI gets it from a ZIP box (`GET /geo?zip=` →
  free no-key zippopotam.us, `lru_cache`d) or `navigator.geolocation`. Distance column + a
  client-side Cheapest/Closest sort appear only when a location is set. No re-ingest to edit coords.
- **Outliers/#7**: `pricing.price_stats()` computes a robust distribution over the per-facility
  prices — q1..q3 IQR band + a median that EXCLUDES outliers (hard floor 0.25×Medicare + Tukey
  1.5×IQR fences). Flagged rows show "likely error"/"gross charge" and are dropped from the typical
  price. `neg_low/neg_high` are now the IQR (result also carries `neg_min/neg_max/n_excluded`).

## Config files (no re-ingest needed to change these)
- `config/geo.yaml` — hospital_id → [lat, lon] for the distance sort.
- `config/metros.yaml` — hospital_id → metro (the area filter + SEO page grouping).
- `config/freestanding.yaml` — non-hospital imaging-center/ASC cash prices, merged into
  the facility list at query time (add `metro:` per facility; defaults to phoenix).
  `config/quality.yaml` — CMS star ratings by hospital id.


Live at **https://healthcare.traqqit.com** on the Hostinger VPS `srv1047338`
(`85.31.233.230`), a **shared box** also running travelmap (`map.traqqit.com`),
n8n, babybuddy, and Hostinger's openclaw agent. Tread carefully.

## Access
SSH key `~/.ssh/hostinger_travelmap` (account key name `claude-travelmap`),
`root@85.31.233.230`. No password.

## Architecture
- App runs as a Docker container **`healthcare-app`** on the existing
  **`travelmap_default`** network. No published port — only Caddy reaches it.
- Ingress is the shared **`travelmap-caddy-1`** Caddy container (owns `:443`,
  auto-TLS via TLS-ALPN-01; host `:80` is Apache2 and is left alone).
- Code at `/root/healthcare`; Parquet in the `healthcare_data` named volume
  (`/app/data` in-container). Container rebuilds the dataset on each start.
- Weekly refresh: host systemd `healthcare-ingest.timer` →
  `docker exec healthcare-app python src/pipeline.py`.

## Redeploy (code change)
```
# from repo root, local:
tar czf /tmp/healthcare.tgz --exclude=.venv --exclude=data --exclude=reports \
    --exclude=.git --exclude=__pycache__ .
scp -i ~/.ssh/hostinger_travelmap /tmp/healthcare.tgz root@85.31.233.230:/root/healthcare.tgz
ssh -i ~/.ssh/hostinger_travelmap root@85.31.233.230 'bash -s' <<'SH'
cd /root/healthcare && tar xzf /root/healthcare.tgz && docker compose up -d --build
# WAIT for the rebuilt app to actually serve before reloading Caddy (see below):
until docker exec travelmap-caddy-1 wget -qO- --timeout=3 http://healthcare-app:8200/ >/dev/null 2>&1; do sleep 1; done
cat /root/travelmap/Caddyfile | docker exec -i travelmap-caddy-1 caddy reload --config /dev/stdin --adapter caddyfile
SH
```
**The Caddy reload is REQUIRED after every rebuild, and must come AFTER the app is up:**
`docker compose up -d --build` recreates `healthcare-app` with a NEW docker IP; Caddy
caches the old upstream → HTTP 502. Reloading re-resolves it (graceful — map stays up).
Reloading too early (before uvicorn serves) re-caches a dead upstream → still 502, so
the `until ... wget` health-wait above is essential.

## ⚠️ Caddy gotcha (cost an hour the first time)
`/root/travelmap/Caddyfile` is a **single-file bind mount**. The Caddy container
pins the file's *inode* at start; editing the host file in place is NOT seen by a
`caddy reload --config /etc/caddy/Caddyfile` ("config is unchanged"). To add/route
without restarting Caddy (restart = `map` downtime, and the harness blocks it),
pipe the current host file in via stdin:
```
cat /root/travelmap/Caddyfile | docker exec -i travelmap-caddy-1 \
    caddy reload --config /dev/stdin --adapter caddyfile
```
Our block (already appended to that Caddyfile):
```
healthcare.traqqit.com {
	encode gzip
	reverse_proxy healthcare-app:8200
}
```

## DNS
`healthcare` A → `85.31.233.230`, AAAA → `2a02:4780:10:bf9a::1` (Hostinger DNS,
TTL 300). `map`, `www`, root untouched.

## Real data (Arizona statewide)
- **Hospitals** (`config/hospitals.yaml`): **54 AZ hospitals in the manifest, 53 with data**
  — the 26 Phoenix-metro ones (Banner, HonorHealth, Valleywise, Phoenix Children's, Dignity,
  Abrazo) **plus 27 statewide** (added 2026-07-24): Tucson metro ×9 (Banner UMC Tucson/South,
  TMC, Northwest/Oro Valley/Houghton, Carondelet St Joseph/St Mary/Holy Cross), Northern AZ
  (Flagstaff + Verde Valley zips, Yavapai combined JSON, Kingman, Havasu zip, Summit JSON,
  Banner Page, Winslow, Springerville), and Southern/Western + Pinal (Yuma, Banner Casa
  Grande/Payson, Canyon Vista, Copper Queen, Mt. Graham, Cobre Valley, Northern Cochise,
  Wickenburg). Only **La Paz Regional = 0 codes** (old CDM-oriented 2023 file, no CMS tall
  schema — self-skipped, harmless). Everything is `state: AZ`; the dataset partitions by
  state, so statewide is just more hospital_ids in the AZ partition — no schema change.
  New MRF hosts beyond the Phoenix set: `sthpiprd.blob` (TMC/N.Cochise), `healthiertucson.com`
  (CHS Tucson), `nahealth.com` (zip), `commonspirit.org` (Yavapai), PARA/Craneware/SlicedHealth/
  AccuReg/Panacea endpoints (Kingman/Winslow/Cobre/Wickenburg/Yuma). CSV via `mrf_parser`,
  JSON via `mrf_json` (ijson), zip via `unzip: true`; routed in `pipeline.py`/`ingest.py`.
  Ingest ~13 min, **~6.7 GB** raw downloaded server-side (cached in the volume; weekly refresh
  reuses it). A common code (MRI 73721) now lands at ~35+ facilities statewide; the card shows
  the cheapest 12 + a "+N more" count. **Streaming parse = memory-safe** even for the 500 MB
  Tucson CSVs (Python `csv`/`ijson` row-by-row, only allowlist rows kept), so no OOM on the
  shared box despite ~2 GB free RAM.
  Run **detached**: `docker exec -d healthcare-app sh -c 'python src/pipeline.py > data/ingest.log 2>&1; echo INGEST_DONE >> data/ingest.log'`
  then poll `docker exec healthcare-app tail data/ingest.log` (full ingest exceeds the 10-min Bash cap).
- **Allowlist** (`config/allowlist.yaml`): ~107-code common-shoppable-procedure list — the
  CORE of what's retained. A common service (e.g. MRI 73721) lands at ~24 hospitals.
- **Payer rates**: ingest also writes `data/payer_rates/` (per code × canonical insurer,
  via `payers.normalize_payer`). Powers the payer dropdown + deductible toggle.
- ⚠️ **Parquet type gotcha**: a hospital whose kept codes ALL lack cash → all-empty
  `cash_price` column → DuckDB infers VARCHAR for that file → glob union promotes the
  column to VARCHAR → `median()` returns a string → app throws. Fixed two ways: writes
  pin numeric cols to DOUBLE (`ingest._write_parquet`), reads `TRY_CAST(... AS DOUBLE)`
  (`pricing.get_hospital*`). After any re-ingest, curl a card and check it 200s.
- **Medicare** (`reference/`, baked into image): CY2025 OPPS Addendum B (primary) +
  CY2025 PPRRVU (PFS) + CY2025 **CLFS** (labs). Source priority OPPS→PFS→CLFS per
  `pricing.SOURCE_PRIORITY`. ~97/100 covered (remainder: 80050 panel, not separately priced).
- **Shop tab is fetch-on-demand**: GET / renders only the search UI (fast); each added
  service fetches its card from `GET /card?code=&state=` so the page scales to any #services.

## ⚠️ Caddy route fragility (IMPORTANT)
The healthcare route lives in the SHARED `/root/travelmap/Caddyfile`, which a
**travelmap redeploy regenerates from scratch — wiping our block** (this already
happened once; symptom: healthcare.traqqit.com → connection failure / HTTP 000 while
map stays up). Durable fix: add the block to travelmap's Caddyfile SOURCE (in the
travelmap repo) so its deploys keep it. Until then, after any travelmap deploy, restore:
```
ssh -i ~/.ssh/hostinger_travelmap root@85.31.233.230 'bash -s' <<SH
grep -q healthcare.traqqit.com /root/travelmap/Caddyfile || \
  printf "\nhealthcare.traqqit.com {\n\tencode gzip\n\treverse_proxy healthcare-app:8200\n}\n" >> /root/travelmap/Caddyfile
cat /root/travelmap/Caddyfile | docker exec -i travelmap-caddy-1 caddy reload --config /dev/stdin --adapter caddyfile
SH
```

## Notes
- No firewall (box default). Memory shared (~1 GB available); 1 uvicorn worker.
- App startup does NOT run ingest (robust to flaky MRF URLs); weekly timer + initial
  `docker exec` populate the data volume.
