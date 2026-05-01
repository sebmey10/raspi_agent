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

# 3. Two-tier brain:
#    - fast: qwen2.5-coder:1.5b (Q4_K_M, ~1 GB resident, fast on Pi NEON).
#    - reasoner: gemma3n-e2b-iq3xs (IQ3_XS, ~2.7 GiB, slower but stronger).
DEFAULT_FAST="${RASP_MODEL_FAST:-qwen2.5-coder:1.5b}"
DEFAULT_REASONER="${RASP_MODEL_REASONER:-gemma3n-e2b-iq3xs}"

GGUF_REPO="bartowski/google_gemma-3n-E2B-it-GGUF"
GGUF_FILE="google_gemma-3n-E2B-it-IQ3_XS.gguf"
GGUF_DIR="$ROOT/data/gguf"
GGUF_PATH="$GGUF_DIR/$GGUF_FILE"
GEMMA3N_TAG="gemma3n-e2b-iq3xs"

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

ensure_gemma3n_iq3xs() {
  if is_pulled "$GEMMA3N_TAG"; then
    echo "[rasp] $GEMMA3N_TAG already registered"
    return
  fi
  echo "[rasp] $GEMMA3N_TAG not found — importing IQ3_XS GGUF…"
  mkdir -p "$GGUF_DIR"
  if [ ! -f "$GGUF_PATH" ]; then
    echo "[rasp] downloading $GGUF_FILE from $GGUF_REPO (~2.2 GB)…"
    curl -L --fail \
      "https://huggingface.co/$GGUF_REPO/resolve/main/$GGUF_FILE" \
      -o "$GGUF_PATH.part"
    mv "$GGUF_PATH.part" "$GGUF_PATH"
  fi
  local mf="$GGUF_DIR/Modelfile.$GEMMA3N_TAG"
  # Gemma uses <start_of_turn>/<end_of_turn> framing — declare both as stop
  # tokens so the model stops cleanly under ollama's chat API.
  cat >"$mf" <<MFEOF
FROM $GGUF_PATH
TEMPLATE """{{- range \$i, \$_ := .Messages }}
{{- \$last := eq (len (slice \$.Messages \$i)) 1 -}}
<start_of_turn>{{ if eq .Role "user" }}user
{{ else if eq .Role "system" }}user
{{ else }}model
{{ end }}{{ .Content }}<end_of_turn>
{{ if and \$last (ne .Role "assistant") }}<start_of_turn>model
{{ end }}
{{- end }}"""
PARAMETER num_ctx 8192
PARAMETER temperature 0.2
PARAMETER repeat_penalty 1.1
PARAMETER stop "<start_of_turn>"
PARAMETER stop "<end_of_turn>"
MFEOF
  ollama create "$GEMMA3N_TAG" -f "$mf"
  echo "[rasp] $GEMMA3N_TAG created"
}

echo "[rasp] models present:"
ollama list | sed 's/^/    /'

# Pull fast model
if [ "$DEFAULT_FAST" = "qwen2.5-coder:1.5b" ]; then
  ensure_pulled "qwen2.5-coder:1.5b"
elif ! is_pulled "$DEFAULT_FAST"; then
  echo "[rasp] WARN: $DEFAULT_FAST not in ollama. Pull it manually."
fi

# Build / pull reasoner model
if [ "$DEFAULT_REASONER" = "$GEMMA3N_TAG" ]; then
  ensure_gemma3n_iq3xs
elif [ "$DEFAULT_REASONER" != "$DEFAULT_FAST" ] && ! is_pulled "$DEFAULT_REASONER"; then
  echo "[rasp] WARN: reasoner $DEFAULT_REASONER not in ollama. Pull it manually."
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

Run:
    source $ROOT/.venv/bin/activate
    rasp

Nightly dream consolidation (systemd user timer at 03:00):
    bash $ROOT/scripts/install-dream-timer.sh
EOF
