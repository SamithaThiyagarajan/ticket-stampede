#!/usr/bin/env bash
# Inject a Postgres fault at a precise time and ALWAYS restore it.
#   [DRYRUN=1] scripts/inject_db_fault.sh MODE OFFSET_S DURATION_S START_AT_EPOCH LOGFILE
#   MODE: pause (docker pause: processes frozen, connections hang)
#         kill  (docker kill -s SIGKILL, then docker start the SAME container: crash recovery)
#   DRYRUN=1: every state-changing docker command is logged, not executed (for testing).
# Safety: acts only on the db container ID captured at start. Never down/rm/up/recreate:
# the data lives on an anonymous volume that a recreated container would not reattach.
# An interrupt (INT/TERM) restores and EXITS: it must never fall through to the injection.
set -uo pipefail
cd "$(dirname "$0")/.."
MODE=$1 OFFSET=$2 DURATION=$3 START_AT=$4 LOG=$5
case "$MODE" in pause|kill) ;; *) echo "MODE must be pause or kill" >&2; exit 2 ;; esac
DRYRUN=${DRYRUN:-0}

now() { date +%s.%N; }
log() { echo "$(now) $*" | tee -a "$LOG"; }
act() { if [ "$DRYRUN" = 1 ]; then log "DRYRUN (not executed): $*"; else "$@" >/dev/null 2>&1; fi; }

DB=$(docker compose ps -q db)
VOL=$(docker inspect -f '{{range .Mounts}}{{.Name}}{{end}}' "$DB")
log "fault=$MODE offset=${OFFSET}s duration=${DURATION}s start_at=$START_AT db=${DB:0:12} volume=${VOL:0:12} dryrun=$DRYRUN"

# Bounded event stream: exits by itself (killing the bash wrapper does not stop docker.exe).
# Separate file: docker.exe (a Windows process) ignores POSIX append and would overwrite $LOG.
UNTIL=$(awk -v a="$START_AT" -v o="$OFFSET" -v d="$DURATION" 'BEGIN{printf "%d", a + o + d + 120}')
docker events --filter "container=$DB" --until "$UNTIL" --format '{{.TimeNano}} docker-event {{.Action}}' \
  < /dev/null > "$LOG.events" 2>&1 &

INJECTED=0
restore() {
  [ "$INJECTED" = 1 ] || return 0
  INJECTED=0
  if [ "$MODE" = pause ]; then
    log "restore: docker unpause issued"; act docker unpause "$DB"; log "restore: docker unpause returned"
  else
    log "restore: docker start issued"; act docker start "$DB"; log "restore: docker start returned"
  fi
  for _ in $(seq 120); do     # wait until Postgres accepts connections again (crash recovery for kill)
    docker exec "$DB" pg_isready -U tickets -q < /dev/null 2>/dev/null && { log "postgres accepting connections"; break; }
    sleep 0.25
  done
  [ "$(docker compose ps -q db)" = "$DB" ] && log "db container unchanged (${DB:0:12})" || log "ERROR: db container changed"
  [ "$(docker inspect -f '{{range .Mounts}}{{.Name}}{{end}}' "$DB")" = "$VOL" ] && log "db volume unchanged" || log "ERROR: db volume changed"
}
# Bash defers traps until the foreground command finishes, so a plain `sleep 3600` would
# delay an interrupt by up to an hour. Sleep in the background and `wait` (interruptible).
SLEEP_PID=
nap() { sleep "$1" & SLEEP_PID=$!; wait "$SLEEP_PID"; }
trap restore EXIT
trap 'log "interrupted"; [ -n "$SLEEP_PID" ] && kill "$SLEEP_PID" 2>/dev/null; restore; exit 130' INT TERM

# Sleep until start_at + OFFSET (fractional seconds).
WAIT=$(awk -v a="$START_AT" -v o="$OFFSET" -v n="$(now)" 'BEGIN{w=a+o-n; print (w>0?w:0)}')
nap "$WAIT"

INJECTED=1
if [ "$MODE" = pause ]; then
  log "inject: docker pause issued"; act docker pause "$DB"; log "inject: docker pause returned"
else
  log "inject: docker kill -s SIGKILL issued"; act docker kill -s SIGKILL "$DB"; log "inject: docker kill returned"
fi
nap "$DURATION"
restore
