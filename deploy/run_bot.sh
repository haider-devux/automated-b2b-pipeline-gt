#!/usr/bin/env bash
# Dispatcher for every Granjur bot. systemd's granjur@<bot>.service calls: run_bot.sh <bot>
# It cd's into the right phase folder (so each script's `import db` resolves) and runs it with the
# project venv python. Environment (secrets + governor knobs) is injected by systemd via
# EnvironmentFile=-/opt/granjur/.env, and the phase scripts also self-load .env via python-dotenv.
#
# The GOVERNOR inside each bot is the real safety authority — this wrapper only routes. If a bot is
# resting/capped it exits 0 immediately (a clean no-op), which is exactly what we want under cron.
set -euo pipefail

BOT="${1:?usage: run_bot.sh <bot>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${GRANJUR_PY:-$ROOT/.venv/bin/python}"
SEND_DRIP="${GRANJUR_SEND_DRIP:-5}"        # emails per send tick (anti-burst drip; jittered in wf4.py)
REPLY_DAYS="${GRANJUR_REPLY_DAYS:-14}"

cd "$ROOT"
case "$BOT" in
  gmaps)      cd wf1_python && exec "$PY" collect_maps.py ;;
  osm)        cd wf1_python && exec "$PY" collect_osm.py ;;
  remoteok)   cd wf1_python && exec "$PY" collect_jobs.py remoteok ;;
  remotive)   cd wf1_python && exec "$PY" collect_jobs.py remotive ;;
  csv)        exec "$PY" scripts/cron/ingest_csv.py ;;
  enrich)     cd wf2_python && exec "$PY" wf2.py ;;
  qualify)    cd wf3_python && exec "$PY" wf3.py ;;
  send)       cd wf4_python && exec "$PY" wf4.py --limit "$SEND_DRIP" ;;
  followup)   cd wf4_python && exec "$PY" followup.py ;;
  reply-scan) cd wf4_python && exec "$PY" reply_parser.py --days "$REPLY_DAYS" ;;
  health)     ( cd wf4_python && "$PY" bounce_parser.py ) ; exec "$PY" scripts/cron/health_check.py ;;
  export)     exec "$PY" scripts/cron/export.py ;;
  *) echo "unknown bot: $BOT" >&2; exit 2 ;;
esac
