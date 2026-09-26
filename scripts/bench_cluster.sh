#!/usr/bin/env bash
# One instrumented run against 3 seller instances behind nginx, all on ONE Postgres.
#   [FASTPATH=0|1] scripts/bench_cluster.sh NAME WORK_DELAY_MS DB_POOL_SIZE [buyer args...]
# Writes results/NAME.{txt,json.gz,pgsample.jsonl,pgsample.txt,dockerstats.csv,env,instances}
set -euo pipefail
export MSYS_NO_PATHCONV=1          # Git Bash on Windows would rewrite /results/... paths
cd "$(dirname "$0")/.."
NAME=$1 DELAY=$2 POOL=$3; shift 3
FASTPATH=${FASTPATH:-1}
R=results/$NAME
SELLERS="seller1 seller2 seller3"
PSQL="docker compose exec -T db psql -U tickets -tAc"

# The single-seller service would add its own connections/CPU; keep it out of cluster runs.
docker compose stop seller >/dev/null 2>&1 || true

# Config set explicitly on every run; never inherited from the shell.
SELLER_MODE=safe WORK_DELAY_MS=$DELAY DB_POOL_SIZE=$POOL SELLER_SCHEMA= SOLDOUT_FASTPATH=$FASTPATH \
  docker compose --profile cluster up -d --force-recreate $SELLERS lb >/dev/null 2>&1
sleep 3
: > "$R.env"
for S in $SELLERS; do
  ENV=$(docker compose exec -T $S printenv SELLER_MODE WORK_DELAY_MS DB_POOL_SIZE SOLDOUT_FASTPATH WEB_CONCURRENCY | tr '\n' ' ')
  [ "$ENV" = "safe $DELAY $POOL $FASTPATH 1 " ] || { echo "ABORT: $S env '$ENV'"; exit 1; }
  echo "$S env verified: $ENV DATABASE_URL=$(docker compose exec -T $S printenv DATABASE_URL)" | tee -a "$R.env"
done

# Shared-state proof, part 1: Postgres sees POOL connections from EACH seller's IP, and
# nothing else. Each instance opens its full pool (min_size = max_size) at startup.
WANT=$(( 3 * POOL ))
for _ in $(seq 30); do
  CONNS=$($PSQL "select count(*) from pg_stat_activity where datname='tickets' and backend_type='client backend' and pid<>pg_backend_pid()")
  [ "$CONNS" = "$WANT" ] && break; sleep 1
done
[ "$CONNS" = "$WANT" ] || { echo "ABORT: $CONNS DB connections, wanted $WANT (3 x $POOL)"; exit 1; }
BY_IP=$($PSQL "select host(client_addr) || '=' || count(*) from pg_stat_activity where datname='tickets' and backend_type='client backend' and pid<>pg_backend_pid() group by client_addr order by 1" | tr '\n' ' ')
IPS=""
for S in $SELLERS; do
  IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(docker compose ps -q $S)")
  echo "$BY_IP" | grep -q "$IP=$POOL" || { echo "ABORT: $S ($IP) does not hold $POOL connections: $BY_IP"; exit 1; }
  IPS="$IPS $S=$IP"
done
echo "one Postgres: $CONNS client connections = 3 x $POOL; by client IP: $BY_IP; seller IPs:$IPS" | tee -a "$R.env"
IDS=$(for S in $SELLERS lb db; do docker compose ps -q $S | cut -c1-12; done | tr '\n' ' ')
echo "containers: $IDS" | tee -a "$R.env"

docker compose run --rm -T --no-deps --name "${NAME}_sampler" --entrypoint python buyer \
  pgsample.py --duration 1800 --out "/$R.pgsample.jsonl" >/dev/null 2>&1 &
( echo "t,name,cpu,mem"; S0=$(date +%s)
  while [ ! -f "$R.done" ]; do
    docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' | sed "s/^/$(( $(date +%s) - S0 )),/"
  done ) > "$R.dockerstats.csv" &
STATS=$!
sleep 2

docker compose run --rm -T --no-deps buyer --url http://lb:80 "$@" --out "/$R.json.gz" 2>/dev/null | tee "$R.txt" || true

touch "$R.done"; wait $STATS; rm -f "$R.done"
docker stop "${NAME}_sampler" >/dev/null 2>&1 || true
NOW=$(for S in $SELLERS lb db; do docker compose ps -q $S | cut -c1-12; done | tr '\n' ' ')
[ "$NOW" = "$IDS" ] && echo "all containers unchanged during run" | tee -a "$R.env" \
  || { echo "INVALID: containers changed during run ($IDS -> $NOW)" | tee -a "$R.env"; exit 1; }

echo; echo "===== /buy handled per instance (each seller's own access log) =====" | tee "$R.instances"
for S in $SELLERS; do
  echo "  $S ($(echo "$IPS" | tr ' ' '\n' | grep "^$S=" | cut -d= -f2)): $(docker compose logs $S 2>&1 | grep -c 'POST /buy')" | tee -a "$R.instances"
done
echo; echo "===== docker stats: mean / max CPU per container ====="
awk -F, 'NR>1{gsub(/%/,"",$3); s[$2]+=$3; n[$2]++; if($3+0>m[$2]+0)m[$2]=$3+0}
         END{for(k in s) printf "  %-40s mean %6.1f%%  max %6.1f%%\n", k, s[k]/n[k], m[k]}' "$R.dockerstats.csv"
echo "===== pg sampler ====="
docker compose run --rm -T --no-deps --entrypoint python buyer pgsample.py --summarize --out "/$R.pgsample.jsonl" 2>/dev/null | tee "$R.pgsample.txt"
