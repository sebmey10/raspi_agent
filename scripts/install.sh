#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "[rasp] root: $ROOT"

# 1. Ownership
if [ "$(stat -c %U "$ROOT")" != "$USER" ]; then
  echo "[rasp] dir owned by $(stat -c %U "$ROOT"), fixing with sudo…"
  sudo chown -R "$USER":"$USER" "$ROOT"
fi

# 2. ollama present + serving
if ! command -v ollama >/dev/null 2>&1; then
  echo "[rasp] ollama not found. Install from https://ollama.com first." >&2
  exit 1
fi
if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null; then
  echo "[rasp] ollama daemon not reachable on 11434. Start it: ollama serve &"
  exit 1
fi

# 3. Two-tier brain (both Qwen3 Q4_K_M for shared tool-call format on ARM):
#    - fast:     qwen3:1.7b (~1.4 GiB resident, ~10-13 tok/s on Pi 5)
#    - reasoner: qwen3:4b   (~3.5 GiB resident, ~5-6 tok/s,  /think capable)
DEFAULT_FAST="${RASP_MODEL_FAST:-qwen3:1.7b}"
DEFAULT_REASONER="${RASP_MODEL_REASONER:-qwen3:4b}"

is_pulled() {
  ollama list | awk 'NR>1 {print $1}' | grep -qx "$1"
}

ensure_pulled() {
  local tag="$1"
  if is_pulled "$tag"; then
    echo "[rasp] $tag already pulled"
  else
    echo "[rasp] pulling $tag …"
    ollama pull "$tag"
  fi
}

echo "[rasp] models present:"
ollama list | sed 's/^/    /'

ensure_pulled "$DEFAULT_FAST"
if [ "$DEFAULT_REASONER" != "$DEFAULT_FAST" ]; then
  ensure_pulled "$DEFAULT_REASONER"
fi

# 4. Python venv
if [ ! -d "$ROOT/.venv" ]; then
  echo "[rasp] creating venv…"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

pip install -q --upgrade pip
pip install -q -e .

# 5. Workspace + data dirs
mkdir -p "${RASP_WORKSPACE:-$HOME/rasp_agent_ws}"
mkdir -p "$ROOT/data/memory"

cat <<EOF

[rasp] install complete.
  fast model    : $DEFAULT_FAST
  reasoner model: $DEFAULT_REASONER
  workspace     : ${RASP_WORKSPACE:-$HOME/rasp_agent_ws}
  data dir      : $ROOT/data
  wiki          : $ROOT/data/memory/WIKI.md

Pi 5 8GB tuning — strongly recommended for inference speed and stability:
    bash $ROOT/scripts/tune-pi.sh         # CPU governor, swappiness, ollama unit hints

Run:
    source $ROOT/.venv/bin/activate
    rasp --doctor                         # verify environment
    rasp                                  # interactive REPL

Nightly dream consolidation (systemd user timer at 03:00):
    bash $ROOT/scripts/install-dream-timer.sh
EOF
