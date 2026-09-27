#!/usr/bin/env bash
# Measure Docker Desktop's delay between issuing `docker kill -s SIGKILL` and Docker's own
# `die` event, on a THROWAWAY postgres:16 container (same image as the ticket db, default
# network, own volume, removed after each trial). Never touches the ticket stack.
#   diag/calibrate_kill_delay.sh TRIALS OUTFILE
set -uo pipefail
TRIALS=${1:-10} OUT=${2:-results/m11_calibration.txt}
NAME=calib_throwaway_pg
case "$NAME" in *ticket_stampede*|db|seller*|lb) echo "refusing: name collides with the ticket stack"; exit 2 ;; esac
now() { date +%s.%N; }
echo "# trial issue_epoch return_epoch kill_event_epoch die_event_epoch cmd_return_s kill_event_s die_event_s" > "$OUT"
for i in $(seq "$TRIALS"); do
  docker rm -f -v "$NAME" > /dev/null 2>&1
  docker run -d --name "$NAME" -e POSTGRES_PASSWORD=calib postgres:16 < /dev/null > /dev/null
  for _ in $(seq 60); do docker exec "$NAME" pg_isready -q < /dev/null 2>/dev/null && break; sleep 0.5; done
  CID=$(docker inspect -f '{{.Id}}' "$NAME")
  EV=$(mktemp)
  docker events --filter "container=$CID" --until "$(( $(date +%s) + 30 ))" \
    --format '{{.TimeNano}} {{.Action}}' < /dev/null > "$EV" 2>&1 &
  sleep 2
  T0=$(now); docker kill -s SIGKILL "$NAME" < /dev/null > /dev/null; T1=$(now)
  for _ in $(seq 40); do grep -q " die" "$EV" && break; sleep 0.25; done
  K=$(awk '$2=="kill"{printf "%.6f", $1/1e9; exit}' "$EV"); D=$(awk '$2=="die"{printf "%.6f", $1/1e9; exit}' "$EV")
  awk -v i="$i" -v a="$T0" -v b="$T1" -v k="$K" -v d="$D" \
    'BEGIN{printf "%d %.6f %.6f %s %s %.3f %.3f %.3f\n", i, a, b, k, d, b-a, k-a, d-a}' | tee -a "$OUT"
  docker rm -f -v "$NAME" > /dev/null 2>&1; rm -f "$EV"
done
awk '!/^#/{n++; d[n]=$8; s+=$8} END{asort(d); printf "die delay over %d trials: min %.3f  median %.3f  mean %.3f  max %.3f  (s)\n", n, d[1], d[int((n+1)/2)], s/n, d[n]}' "$OUT" | tee -a "$OUT"
