#!/usr/bin/env bash
# One instrumented benchmark run with a verified seller configuration.
#   scripts/bench.sh NAME WORK_DELAY_MS DB_POOL_SIZE [buyer args...]
# Writes results/NAME.{txt,json.gz,pgsample.jsonl,pgsample.txt,dockerstats.csv,env}
set -euo pipefail
export MSYS_NO_PATHCONV=1          # Git Bash on Windows would rewrite /results/... paths
cd "$(dirname "$0")/.."
NAME=$1 DELAY=$2 POOL=$3; shift 3
R=results/$NAME

# Seller config is set explicitly on every run; never inherited from the shell.
SELLER_MODE=safe WORK_DELAY_MS=$DELAY DB_POOL_SIZE=$POOL SELLER_SCHEMA= \
  docker compose up -d --force-recreate seller >/dev/null 2>&1
sleep 3
ENV=$(docker compose exec -T seller printenv SELLER_MODE WORK_DELAY_MS DB_POOL_SIZE | tr '\n' ' ')
SELLER_ID=$(docker compose ps -q seller)
[ "$ENV" = "safe $DELAY $POOL " ] || { echo "ABORT: seller env is '$ENV', wanted 'safe $DELAY $POOL'"; exit 1; }
echo "seller env verified: $ENV(container ${SELLER_ID:0:12})" | tee "$R.env"

docker compose run --rm -T --no-deps --name "${NAME}_sampler" --entrypoint python buyer \
  pgsample.py --duration 1800 --out "/$R.pgsample.jsonl" >/dev/null 2>&1 &
( echo "t,name,cpu,mem"; S=$(date +%s)
  while [ ! -f "$R.done" ]; do
    docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' | sed "s/^/$(( $(date +%s) - S )),/"
  done ) > "$R.dockerstats.csv" &
STATS=$!
sleep 2

docker compose run --rm -T --no-deps buyer --url http://seller:8000 "$@" --out "/$R.json.gz" 2>/dev/null | tee "$R.txt" || true

touch "$R.done"; wait $STATS; rm -f "$R.done"
docker stop "${NAME}_sampler" >/dev/null 2>&1 || true
[ "$(docker compose ps -q seller)" = "$SELLER_ID" ] \
  && echo "seller container unchanged during run" | tee -a "$R.env" \
  || { echo "INVALID: seller container changed during run" | tee -a "$R.env"; exit 1; }

echo; echo "===== docker stats: mean / max CPU per container ====="
awk -F, 'NR>1{gsub(/%/,"",$3); s[$2]+=$3; n[$2]++; if($3+0>m[$2]+0)m[$2]=$3+0}
         END{for(k in s) printf "  %-40s mean %6.1f%%  max %6.1f%%\n", k, s[k]/n[k], m[k]}' "$R.dockerstats.csv"
echo "===== pg sampler ====="
docker compose run --rm -T --no-deps --entrypoint python buyer pgsample.py --summarize --out "/$R.pgsample.jsonl" 2>/dev/null | tee "$R.pgsample.txt"
