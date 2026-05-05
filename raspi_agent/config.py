from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_WORKSPACE = Path.home() / "raspi_ws"


def _env_first(*names: str | None) -> str | None:
    """Return the first env var set among names. Pass None to skip a slot."""
    for name in names:
        if not name:
            continue
        v = os.environ.get(name)
        if v is not None and v.strip():
            return v
    return None


def _env_path(new: str, legacy: str | None, default: Path) -> Path:
    v = _env_first(new, legacy)
    return Path(v).expanduser() if v else default


def _env_int(new: str, legacy: str | None, default: int) -> int:
    v = _env_first(new, legacy)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _env_keep_alive(new: str, legacy: str | None, default: int | str) -> int | str:
    v = _env_first(new, legacy)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return v


def _env_bool(new: str, legacy: str | None, default: bool) -> bool:
    v = _env_first(new, legacy)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_think(new: str, legacy: str | None,
               default: bool | Literal["low", "medium", "high"] | None):
    v = _env_first(new, legacy)
    if v is None:
        return default
    value = v.strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"low", "medium", "high"}:
        return value
    if value in {"none", "auto", "default"}:
        return None
    return default


def _env_choice(new: str, legacy: str | None, default: str, allowed: set[str]) -> str:
    v = _env_first(new, legacy)
    if v is None:
        return default
    value = v.strip().lower()
    return value if value in allowed else default


def _env_str(new: str, legacy: str | None, default: str) -> str:
    v = _env_first(new, legacy)
    return v if v is not None else default


def _env_float(new: str, legacy: str | None, default: float) -> float:
    v = _env_first(new, legacy)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_csv(new: str, legacy: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    v = _env_first(new, legacy)
    if v is None:
        return default
    parts = tuple(p.strip() for p in v.split(",") if p.strip())
    return parts or default


@dataclass(frozen=True)
class Config:
    # ---- paths ----
    data_dir: Path = field(
        default_factory=lambda: _env_path("RASPI_DATA_DIR", "RASP_DATA_DIR", DEFAULT_DATA_DIR)
    )
    workspace: Path = field(
        default_factory=lambda: _env_path("RASPI_WORKSPACE", "RASP_WORKSPACE", DEFAULT_WORKSPACE)
    )

    # ---- ollama ----
    ollama_url: str = field(
        default_factory=lambda: os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    )

    # Pi 5 8GB defaults — both Q4_K_M.
    #   fast     = qwen2.5-coder:1.5b — coding-tuned, BFCL-strong tool calls,
    #              ~1.2 GiB resident, ~12-15 tok/s on Pi 5.
    #   reasoner = qwen3:4b           — agent reasoner with /think,
    #              ~3.5 GiB resident, ~5-6 tok/s.
    # Stay on Q4_K_M; IQ-quants are arithmetic-heavy on ARM CPU and degrade
    # tool-call reliability at <=4B.
    model_fast: str = field(
        default_factory=lambda: _env_str("RASPI_MODEL_FAST", "RASP_MODEL_FAST", "qwen2.5-coder:1.5b")
    )
    model_reasoner: str = field(
        default_factory=lambda: _env_str("RASPI_MODEL_REASONER", "RASP_MODEL_REASONER", "qwen3:4b")
    )

    ctx_fast: int = field(
        default_factory=lambda: _env_int("RASPI_CTX_FAST", "RASP_CTX_FAST", 2048)
    )
    ctx_reasoner: int = field(
        default_factory=lambda: _env_int("RASPI_CTX_REASONER", "RASP_CTX_REASONER", 4096)
    )
    keep_alive: int | str = field(
        default_factory=lambda: _env_keep_alive("RASPI_KEEP_ALIVE", "RASP_KEEP_ALIVE", -1)
    )
    num_thread: int = field(
        default_factory=lambda: _env_int(
            "RASPI_NUM_THREAD", "RASP_NUM_THREAD", min(os.cpu_count() or 4, 4)
        )
    )
    num_predict_fast: int = field(
        default_factory=lambda: _env_int(
            "RASPI_NUM_PREDICT_FAST", "RASP_NUM_PREDICT_FAST", 192
        )
    )
    num_predict_reasoner: int = field(
        default_factory=lambda: _env_int(
            "RASPI_NUM_PREDICT_REASONER", "RASP_NUM_PREDICT_REASONER", 384
        )
    )
    think_fast: bool | Literal["low", "medium", "high"] | None = field(
        default_factory=lambda: _env_think("RASPI_THINK_FAST", "RASP_THINK_FAST", False)
    )
    think_reasoner: bool | Literal["low", "medium", "high"] | None = field(
        default_factory=lambda: _env_think("RASPI_THINK_REASONER", "RASP_THINK_REASONER", None)
    )
    native_tools: str = field(
        default_factory=lambda: _env_choice(
            "RASPI_NATIVE_TOOLS", "RASP_NATIVE_TOOLS", "auto", {"auto", "on", "off"}
        )
    )

    # ---- shell / sandbox ----
    bash_allowlist: tuple[str, ...] = (
        "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "tree",
        "sed", "awk", "sort", "uniq", "cut", "tr",
        "pwd", "echo", "date", "which", "diff", "stat", "file",
        "git", "python3", "python", "pip", "pytest", "ruff",
        "node", "npm", "pnpm", "yarn",
        "df", "du", "free", "uname", "uptime", "ps", "env",
        "curl", "jq",
    )
    bash_timeout: int = field(
        default_factory=lambda: _env_int("RASPI_BASH_TIMEOUT", "RASP_BASH_TIMEOUT", 30)
    )
    sandbox: str = field(
        default_factory=lambda: _env_choice(
            "RASPI_SANDBOX", None, "auto", {"auto", "on", "off"}
        )
    )
    confirm_tools: tuple[str, ...] = field(
        default_factory=lambda: _env_csv(
            "RASPI_CONFIRM", None,
            ("write", "edit", "apply_patch", "ast_edit", "bash"),
        )
    )

    # ---- dream / consolidation ----
    dream_min_turns: int = field(
        default_factory=lambda: _env_int("RASPI_DREAM_MIN_TURNS", "RASP_DREAM_MIN_TURNS", 4)
    )
    dream_max_turns: int = field(
        default_factory=lambda: _env_int("RASPI_DREAM_MAX_TURNS", "RASP_DREAM_MAX_TURNS", 256)
    )
    dream_transcript_chars: int = field(
        default_factory=lambda: _env_int(
            "RASPI_DREAM_TRANSCRIPT_CHARS", "RASP_DREAM_TRANSCRIPT_CHARS", 24000
        )
    )

    # ---- prompt budgets ----
    wiki_prompt_chars: int = field(
        default_factory=lambda: _env_int(
            "RASPI_WIKI_PROMPT_CHARS", "RASP_WIKI_PROMPT_CHARS", 9000
        )
    )
    plan_prompt_chars: int = field(
        default_factory=lambda: _env_int("RASPI_PLAN_PROMPT_CHARS", None, 1500)
    )
    context_prompt_chars: int = field(
        default_factory=lambda: _env_int("RASPI_CONTEXT_PROMPT_CHARS", None, 1500)
    )
    repo_instructions_chars: int = field(
        default_factory=lambda: _env_int("RASPI_REPO_INSTRUCTIONS_CHARS", None, 4000)
    )
    history_prompt_chars: int = field(
        default_factory=lambda: _env_int(
            "RASPI_HISTORY_PROMPT_CHARS", "RASP_HISTORY_PROMPT_CHARS", 7000
        )
    )
    max_history_messages: int = field(
        default_factory=lambda: _env_int(
            "RASPI_MAX_HISTORY_MESSAGES", "RASP_MAX_HISTORY_MESSAGES", 16
        )
    )
    tool_result_chars: int = field(
        default_factory=lambda: _env_int(
            "RASPI_TOOL_RESULT_CHARS", "RASP_TOOL_RESULT_CHARS", 5000
        )
    )

    # ---- agent loop ----
    max_tool_loops: int = field(
        default_factory=lambda: _env_int("RASPI_MAX_TOOL_LOOPS", None, 12)
    )
    reflect_every: int = field(
        default_factory=lambda: _env_int("RASPI_REFLECT_EVERY", None, 4)
    )
    compact_threshold_ratio: float = field(
        default_factory=lambda: _env_float("RASPI_COMPACT_THRESHOLD", None, 0.8)
    )
    fail_streak_to_escalate: int = field(
        default_factory=lambda: _env_int(
            "RASPI_FAIL_STREAK_TO_ESCALATE", "RASP_FAIL_STREAK_TO_ESCALATE", 2
        )
    )
    auto_dream_on_exit: bool = field(
        default_factory=lambda: _env_bool(
            "RASPI_AUTO_DREAM_ON_EXIT", "RASP_AUTO_DREAM_ON_EXIT", False
        )
    )

    # ---- npu (stub for v0.4) ----
    npu: str = field(
        default_factory=lambda: _env_choice("RASPI_NPU", None, "off", {"off", "hailo"})
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db.sqlite"

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memory"

    @property
    def wiki_path(self) -> Path:
        return self.memory_dir / "WIKI.md"

    @property
    def wiki_snapshots_dir(self) -> Path:
        return self.memory_dir / "_snapshots"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def workspace_state_dir(self) -> Path:
        return self.workspace / ".raspi"

    @property
    def plan_path(self) -> Path:
        return self.workspace_state_dir / "plan.md"

    @property
    def scratchpad_path(self) -> Path:
        return self.workspace_state_dir / "scratchpad.md"

    @property
    def context_path(self) -> Path:
        return self.workspace_state_dir / "context.md"

    def ensure_dirs(self) -> None:
        for p in [self.data_dir, self.memory_dir, self.wiki_snapshots_dir,
                  self.sessions_dir, self.workspace, self.workspace_state_dir]:
            p.mkdir(parents=True, exist_ok=True)


CONFIG = Config()
