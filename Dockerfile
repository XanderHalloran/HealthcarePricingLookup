FROM python:3.12-slim
WORKDIR /app

# Runtime deps only. duckdb covers CSV read + aggregation + Parquet (no pyarrow).
# Plain uvicorn (not [standard]) keeps the image and memory footprint small — this
# is a low-traffic app on a shared box.
# Pinned to the exact resolved set (requirements.txt), transitive deps included.
# Unpinned installs mean every rebuild pulls whatever PyPI serves that minute, so
# one poisoned release of any dep lands straight in prod. Copied before src/ so
# the (slow, rarely-changing) dep layer stays cached across code redeploys.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/
COPY config-ortho/ ./config-ortho/
COPY reference/ ./reference/

EXPOSE 8200
# Serve immediately; do NOT block startup on ingest. Real MRFs are ~500 MB to
# download+parse and a flaky hospital URL must not crash-loop the app. Ingest is
# run separately (initial `docker exec ... pipeline` + the weekly systemd timer);
# the app handles a missing/empty dataset gracefully ("no data"). Data persists
# in the healthcare_data volume across restarts.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8200", "--app-dir", "src"]
