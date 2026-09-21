#!/bin/sh
# Weekly price-data refresh, ONE STATE AT A TIME.
#
# Why not a single full rebuild: this box has 3.8 GB RAM. On 2026-09-21 the full healthcare
# rebuild exhausted it, the kernel OOM-killer picked the biggest process (uvicorn, 849 MB) and
# killed the WEB SERVER, the container restarted, and the refresh died 2.5 h in with nothing
# written. Each `--state XX` run does its own atomic partition swap, so:
#   - peak memory is bounded by one state's work, not six states',
#   - a state that fails leaves the other states' fresh data in place,
#   - the live dataset is never half-written.
# oom_score_adj=500 makes the pipeline itself the kernel's preferred victim, so if memory does
# run short the batch job dies instead of the site.
#
# usage: ingest-by-state.sh <container> <STATE> [STATE...]
container="$1"; shift
rc=0
for s in "$@"; do
  echo "=== $container $s starting $(date -u +%FT%TZ)"
  if docker exec "$container" sh -c "echo 500 > /proc/self/oom_score_adj; exec python -u src/pipeline.py --state $s"; then
    echo "=== $container $s ok $(date -u +%FT%TZ)"
  else
    echo "=== $container $s FAILED (exit $?) $(date -u +%FT%TZ)"
    rc=1
  fi
done
exit $rc
