#!/usr/bin/env bash
# raspi installer — one-shot setup for Raspberry Pi 5 (or any Debian/Ubuntu host).
#
#   bash scripts/install.sh                # local dev install (uv tool > pipx > venv)
#   bash scripts/install.sh --venv         # force traditional venv install
#
# When a release tarball exists (v0.4+), the curl-bash flow will be:
#   curl -fsSL https://.../install.sh | sh -s -- --check-hash
# That path downloads a tarball + .sha256 sidecar, verifies the hash, and
# installs from the verified directory. The --check-hash branch in this script
# is the placeholder for that flow; today it falls through to the local install.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ---- args --------------------------------------------------------------------

INSTALL_MODE="auto"   # auto | venv
CHECK_HASH=0
RELEASE_URL="${RASPI_RELEASE_URL:-}"
RELEASE_HASH="${RASPI_RELEASE_SHA256:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --venv) INSTALL_MODE="venv" ;;
    --check-hash) CHECK_HASH=1 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf "[raspi] %s\n" "$*"; }
warn() { printf "[raspi] \033[33m%s\033[0m\n" "$*"; }
err()  { printf "[raspi] \033[31m%s\033[0m\n" "$*" >&2; }

# ---- ownership ---------------------------------------------------------------

if [ "$(stat -c %U "$ROOT" 2>/dev/null || stat -f %Su "$ROOT")" != "$USER" ]; then
  warn "dir not owned by $USER, fixing with sudo…"
  sudo chown -R "$USER" "$ROOT"
fi

# ---- (placeholder) hash verification ----------------------------------------

if [ "$CHECK_HASH" -eq 1 ]; then
  if [ -z "$RELEASE_URL" ] || [ -z "$RELEASE_HASH" ]; then
    warn "--check-hash given but RASPI_RELEASE_URL / RASPI_RELEASE_SHA256 unset; "
    warn "skipping hash verification and installing from the current tree."
  else
    say "verifying release tarball at $RELEASE_URL…"
    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "$tmp_dir"' EXIT
    curl -fsSL "$RELEASE_URL" -o "$tmp_dir/release.tar.gz"
    actual_hash="$(sha256sum "$tmp_dir/release.tar.gz" | awk '{print $1}')"
    if [ "$actual_hash" != "$RELEASE_HASH" ]; then
      err "hash mismatch — expected $RELEASE_HASH got $actual_hash"; exit 3
    fi
    say "hash ok"
    tar -xzf "$tmp_dir/release.tar.gz" -C "$tmp_dir"
    # First top-level directory in the tarball.
    extracted="$(find "$tmp_dir" -mindepth 1 -maxdepth 1 -type d | head -n1)"
    if [ -z "$extracted" ]; then err "tarball has no top-level dir"; exit 3; fi
    ROOT="$extracted"
    cd "$ROOT"
  fi
fi

# ---- platform sanity --------------------------------------------------------

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) say "arch ok: $ARCH" ;;
  x86_64) warn "running on $ARCH — works for dev, but raspi targets Pi 5 aarch64" ;;
  *) warn "unfamiliar arch $ARCH; proceeding anyway" ;;
esac

# ---- ollama -----------------------------------------------------------------

if ! command -v ollama >/dev/null 2>&1; then
  say "installing ollama…"
  curl -fsSL https://ollama.com/install.sh | sh
fi

# Ollama daemon. If not reachable, give a hint and exit non-zero.
if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  warn "ollama daemon not reachable on 127.0.0.1:11434"
  warn "start it with: ollama serve &     (or: systemctl --user start ollama)"
  exit 1
fi

DEFAULT_FAST="${RASPI_MODEL_FAST:-${RASP_MODEL_FAST:-qwen2.5-coder:1.5b}}"
DEFAULT_REASONER="${RASPI_MODEL_REASONER:-${RASP_MODEL_REASONER:-qwen3:4b}}"

is_pulled() {
  ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$1"
}

ensure_pulled() {
  local tag="$1"
  if is_pulled "$tag"; then
    say "$tag already pulled"
  else
    say "pulling $tag … (this can take a few minutes)"
    ollama pull "$tag"
  fi
}

ensure_pulled "$DEFAULT_FAST"
[ "$DEFAULT_REASONER" != "$DEFAULT_FAST" ] && ensure_pulled "$DEFAULT_REASONER"

# ---- install raspi-agent ----------------------------------------------------

INSTALL_USED="(none)"
case "$INSTALL_MODE" in
  venv)
    say "venv install requested"
    [ -d ".venv" ] || python3 -m venv .venv
    # shellcheck disable=SC1091
    source ".venv/bin/activate"
    pip install -q --upgrade pip
    pip install -q -e ".[dev]"
    INSTALL_USED="venv (.venv)"
    ;;
  auto)
    if command -v uv >/dev/null 2>&1; then
      say "installing with uv tool…"
      uv tool install --force "$ROOT"
      INSTALL_USED="uv tool"
    elif command -v pipx >/dev/null 2>&1; then
      say "installing with pipx…"
      pipx install --force "$ROOT"
      INSTALL_USED="pipx"
    else
      say "no uv/pipx found — falling back to .venv (run with --venv to force)"
      [ -d ".venv" ] || python3 -m venv .venv
      # shellcheck disable=SC1091
      source ".venv/bin/activate"
      pip install -q --upgrade pip
      pip install -q -e ".[dev]"
      INSTALL_USED="venv (.venv) — install uv (https://docs.astral.sh/uv/) for faster installs"
    fi
    ;;
esac

# ---- workspace + data dirs --------------------------------------------------

WORKSPACE_DIR="${RASPI_WORKSPACE:-${RASP_WORKSPACE:-$HOME/raspi_ws}}"
mkdir -p "$WORKSPACE_DIR"
mkdir -p "$ROOT/data/memory"

# ---- final banner -----------------------------------------------------------

cat <<EOF

[raspi] install complete  ($INSTALL_USED)
  fast model    : $DEFAULT_FAST
  reasoner model: $DEFAULT_REASONER
  workspace     : $WORKSPACE_DIR
  data dir      : $ROOT/data
  wiki          : $ROOT/data/memory/WIKI.md

Pi 5 8GB tuning — strongly recommended:
    raspi --tune                          # cpu governor, swappiness, ollama unit hints

First 5 things to try:
    raspi --doctor                        # verify environment + sandbox + models
    raspi "Say hi in one short sentence." # smoke check
    raspi --workspace ~/myproj "Read README and summarize"
    raspi                                 # interactive REPL; / for slash commands
    raspi /sleep                          # nightly memory consolidation (manual run)

Optional: nightly dream consolidation timer
    bash $ROOT/scripts/install-dream-timer.sh
EOF
