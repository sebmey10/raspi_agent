#!/usr/bin/env bash
# Pi 5 inference tuning. Idempotent: safe to re-run. Prints before changing.
#
# Applies (with sudo where needed):
#   - CPU governor   -> performance     (~5-15% generation speedup)
#   - vm.swappiness  -> 1                (avoid swapping the model out)
#   - ollama systemd drop-in:
#       LimitMEMLOCK=infinity            (mlock model weights)
#       Environment=OLLAMA_KEEP_ALIVE=-1
#       Environment=OLLAMA_NUM_PARALLEL=1
#       Environment=OLLAMA_MAX_LOADED_MODELS=1
#       Environment=OLLAMA_LOAD_TIMEOUT=10m
#
# Re-run scripts/tune-pi.sh --revert to undo all of the above.
set -euo pipefail

REVERT=0
if [ "${1:-}" = "--revert" ]; then
  REVERT=1
fi

DROPIN_DIR=/etc/systemd/system/ollama.service.d
DROPIN_FILE="$DROPIN_DIR/rasp-tune.conf"
SYSCTL_FILE=/etc/sysctl.d/90-rasp.conf

echo "[tune] platform: $(uname -m) $(uname -sr)"

# 1. CPU governor
gov_now="$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo unknown)"
echo "[tune] cpu governor: $gov_now"
if [ "$REVERT" -eq 1 ]; then
  echo "[tune] -> ondemand (kernel default)"
  for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo ondemand | sudo tee "$f" >/dev/null || true
  done
else
  if [ "$gov_now" != "performance" ]; then
    echo "[tune] -> performance"
    for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
      echo performance | sudo tee "$f" >/dev/null
    done
  fi
fi

# 2. swappiness
sw_now="$(cat /proc/sys/vm/swappiness 2>/dev/null || echo unknown)"
echo "[tune] vm.swappiness: $sw_now"
if [ "$REVERT" -eq 1 ]; then
  echo "[tune] removing $SYSCTL_FILE"
  sudo rm -f "$SYSCTL_FILE"
  sudo sysctl -p >/dev/null || true
else
  if [ "$sw_now" != "1" ]; then
    echo "[tune] -> 1 (persisted to $SYSCTL_FILE)"
    echo 'vm.swappiness=1' | sudo tee "$SYSCTL_FILE" >/dev/null
    sudo sysctl -p "$SYSCTL_FILE" >/dev/null
  fi
fi

# 3. ollama systemd drop-in (only if ollama is a systemd unit)
if systemctl list-unit-files 2>/dev/null | grep -q '^ollama\.service'; then
  if [ "$REVERT" -eq 1 ]; then
    echo "[tune] removing $DROPIN_FILE"
    sudo rm -f "$DROPIN_FILE"
    sudo systemctl daemon-reload
    sudo systemctl restart ollama || true
  else
    echo "[tune] writing $DROPIN_FILE"
    sudo mkdir -p "$DROPIN_DIR"
    sudo tee "$DROPIN_FILE" >/dev/null <<'EOF'
[Service]
LimitMEMLOCK=infinity
Environment=OLLAMA_KEEP_ALIVE=-1
Environment=OLLAMA_NUM_PARALLEL=1
Environment=OLLAMA_MAX_LOADED_MODELS=1
Environment=OLLAMA_LOAD_TIMEOUT=10m
EOF
    sudo systemctl daemon-reload
    sudo systemctl restart ollama || echo "[tune] WARN: could not restart ollama; restart manually"
  fi
else
  echo "[tune] ollama.service not found via systemctl — skipping unit drop-in"
  echo "[tune]   if you run ollama manually, export these in your shell:"
  echo "[tune]     OLLAMA_KEEP_ALIVE=-1 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1"
fi

if [ "$REVERT" -eq 1 ]; then
  echo "[tune] reverted."
else
  echo "[tune] done. verify with: rasp --doctor"
fi
