#!/usr/bin/env bash
# Install (or refresh) all Granjur bot timers on a Linux server. Idempotent: re-run after any change.
# Assumes the repo is at /opt/granjur with a venv at /opt/granjur/.venv and a 'granjur' user.
# Edit granjur@.service if your paths/user differ, then run:  sudo bash deploy/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     # .../deploy
UNIT_DIR=/etc/systemd/system

# CORE bots are low-risk (free APIs, polite pacing, your own mailbox) — auto-enabled on install.
CORE_BOTS=(osm remoteok remotive csv enrich qualify send followup reply-scan health export)
# EXPERIMENTAL bots scrape a hostile target and need a residential proxy — installed but left DISABLED.
# Prove them by hand first (see deploy/README.md §4), then: systemctl enable --now granjur-gmaps.timer
EXPERIMENTAL_BOTS=(gmaps)
ALL_BOTS=("${CORE_BOTS[@]}" "${EXPERIMENTAL_BOTS[@]}")

echo "==> Installing templated service + ${#ALL_BOTS[@]} timers into $UNIT_DIR"
install -m 0644 "$SRC/systemd/granjur@.service" "$UNIT_DIR/granjur@.service"
for b in "${ALL_BOTS[@]}"; do
  install -m 0644 "$SRC/systemd/granjur-$b.timer" "$UNIT_DIR/granjur-$b.timer"
done
chmod +x "$SRC/run_bot.sh"

echo "==> daemon-reload"
systemctl daemon-reload

echo "==> enabling + starting ${#CORE_BOTS[@]} CORE timers"
for b in "${CORE_BOTS[@]}"; do
  systemctl enable --now "granjur-$b.timer"
done

echo "==> leaving ${#EXPERIMENTAL_BOTS[@]} EXPERIMENTAL timer(s) installed but DISABLED"
for b in "${EXPERIMENTAL_BOTS[@]}"; do
  systemctl disable --now "granjur-$b.timer" 2>/dev/null || true   # idempotent: ensure it stays off
done

echo "==> Done. Current schedule:"
systemctl list-timers 'granjur-*' --no-pager || true
echo
echo "EXPERIMENTAL (off by default): ${EXPERIMENTAL_BOTS[*]}"
echo "  Prove it by hand first (needs Playwright + GMAPS_PROXIES), then enable:"
echo "    systemctl start granjur@gmaps.service          # one manual run, watch the block detector"
echo "    systemctl enable --now granjur-gmaps.timer     # hand it to cron once it's clean"
echo
echo "Tip: watch a bot ->  journalctl -u granjur@osm.service -f"
echo "     run one now  ->  systemctl start granjur@osm.service"
echo "     governor state -> $( [ -x /opt/granjur/.venv/bin/python ] && echo '/opt/granjur/.venv/bin/python scripts/governor.py' || echo 'python scripts/governor.py')"
