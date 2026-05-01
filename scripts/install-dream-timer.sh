#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/rasp-dream.service" <<EOF
[Unit]
Description=rasp agent nightly memory consolidation

[Service]
Type=oneshot
ExecStart=/usr/bin/env bash $ROOT/scripts/dream.sh
EOF

cat > "$UNIT_DIR/rasp-dream.timer" <<EOF
[Unit]
Description=Run rasp-dream every night

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now rasp-dream.timer
echo "[rasp] dream timer installed. Status:"
systemctl --user status rasp-dream.timer --no-pager || true
