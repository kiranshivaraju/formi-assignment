#!/usr/bin/env bash
# Starts the controller against a simulator and runs until the scenario finishes.
#   ./run.sh http://127.0.0.1:8700 [extra controller flags, e.g. --step 60]
#
# Uses Docker (docker compose) for Postgres, the controller's state store. The controller itself
# runs on the host with the Python from .venv if present, else python3 (needs requirements.txt).
#
# If the controller dies (crash, OOM, lost connection) it is restarted and resumes from its
# bookmark in Postgres. Exit 2 means a problem a restart can't fix, so we stop. We give up after
# MAX_RESTARTS failures in a row; a run that lasted MIN_HEALTHY_S seconds resets the count.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL> [controller flags]}"
shift
cd "$(dirname "$0")"

PY="${PYTHON:-$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3 )}"
MAX_RESTARTS="${MAX_RESTARTS:-20}"
MIN_HEALTHY_S="${MIN_HEALTHY_S:-60}"

docker compose up -d --wait db >&2

child=""
trap '[ -n "$child" ] && kill -TERM "$child" 2>/dev/null; exit 143' TERM INT

failures=0
while true; do
  started=$SECONDS
  "$PY" -m controller.main --url "$SIM_URL" "$@" &
  child=$!
  code=0
  wait "$child" || code=$?
  child=""
  [ "$code" -eq 0 ] && exit 0
  if [ "$code" -eq 2 ]; then
    echo "controller hit a fatal configuration error (see above); not restarting" >&2
    exit 2
  fi
  [ $((SECONDS - started)) -ge "$MIN_HEALTHY_S" ] && failures=0
  failures=$((failures + 1))
  if [ "$failures" -ge "$MAX_RESTARTS" ]; then
    echo "controller failed $failures times in a row (last exit $code); giving up" >&2
    exit "$code"
  fi
  echo "controller exited $code; restart $failures/$MAX_RESTARTS in 2s" >&2
  sleep 2
done
