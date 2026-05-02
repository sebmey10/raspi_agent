from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_WORKSPACE = Path.home() / "rasp_agent_ws"


def _env_path(var: str, default: Path) -> Path:
    v = os.environ.get(var)
    return Path(v).expanduser() if v else default


def _env_int(var: str, default: int) -> int:
    v = os.environ.get(var)
    if v is None or not v.strip():
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _env_keep_alive(var: str, default: int | str) -> int | str:
    v = os.environ.get(var)
    if v is None or not v.strip():
        return default
    try:
        return int(v)
    except ValueError:
        return v


def _env_bool(var: str, default: bool) -> bool:
    v = os.environ.get(var)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_think(var: str, default: bool | Literal["low", "medium", "high"] | None):
    v = os.environ.get(var)
    if v is None or not v.strip():
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


def _env_choice(var: str, default: str, allowed: set[str]) -> str:
    v = os.environ.get(var)
    if v is None or not v.strip():
        return default
    value = v.strip().lower()
    return value if value in allowed else default


@dataclass(frozen=True)
class Config:
    data_dir: Path = field(default_factory=lambda: _env_path("RASP_DATA_DIR", DEFAULT_DATA_DIR))
    workspace: Path = field(default_factory=lambda: _env_path("RASP_WORKSPACE", DEFAULT_WORKSPACE))

    ollama_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"))
    # Pi 5 8GB defaults — Q4_K_M for both tiers. Qwen3 family for shared
    # tool-call format and KV-cache friendliness. Reasoner runs ~5-6 tok/s; fast
    # ~10-13 tok/s. Stay clear of IQ-quants on ARM CPU — they are arithmetic
    # heavy and degrade tool-call reliability at <=4B.
    model_fast: str = field(default_factory=lambda: os.environ.get("RASP_MODEL_FAST", "qwen3:1.7b"))
    model_reasoner: str = field(default_factory=lambda: os.environ.get("RASP_MODEL_REASONER", "qwen3:4b"))

    ctx_fast: int = field(default_factory=lambda: _env_int("RASP_CTX_FAST", 2048))
    ctx_reasoner: int = field(default_factory=lambda: _env_int("RASP_CTX_REASONER", 4096))
    keep_alive: int | str = field(default_factory=lambda: _env_keep_alive("RASP_KEEP_ALIVE", -1))
    num_thread: int = field(default_factory=lambda: _env_int("RASP_NUM_THREAD", min(os.cpu_count() or 4, 4)))
    num_predict_fast: int = field(default_factory=lambda: _env_int("RASP_NUM_PREDICT_FAST", 192))
    num_predict_reasoner: int = field(default_factory=lambda: _env_int("RASP_NUM_PREDICT_REASONER", 384))
    think_fast: bool | Literal["low", "medium", "high"] | None = field(
        default_factory=lambda: _env_think("RASP_THINK_FAST", False)
    )
    think_reasoner: bool | Literal["low", "medium", "high"] | None = field(
        default_factory=lambda: _env_think("RASP_THINK_REASONER", None)
    )
    native_tools: str = field(
        default_factory=lambda: _env_choice("RASP_NATIVE_TOOLS", "auto", {"auto", "on", "off"})
    )

    bash_allowlist: tuple[str, ...] = (
        "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "tree",
        "sed", "awk", "sort", "uniq", "cut", "tr",
        "pwd", "echo", "date", "which", "diff", "stat", "file",
        "git", "python3", "python", "pip", "pytest", "ruff",
        "node", "npm", "pnpm", "yarn",
        "df", "du", "free", "uname", "uptime", "ps", "env",
        "curl", "jq",
    )
    bash_timeout: int = field(default_factory=lambda: _env_int("RASP_BASH_TIMEOUT", 30))

    dream_min_turns: int = field(default_factory=lambda: _env_int("RASP_DREAM_MIN_TURNS", 4))
    wiki_prompt_chars: int = field(default_factory=lambda: _env_int("RASP_WIKI_PROMPT_CHARS", 9000))
    history_prompt_chars: int = field(default_factory=lambda: _env_int("RASP_HISTORY_PROMPT_CHARS", 7000))
    max_history_messages: int = field(default_factory=lambda: _env_int("RASP_MAX_HISTORY_MESSAGES", 16))
    tool_result_chars: int = field(default_factory=lambda: _env_int("RASP_TOOL_RESULT_CHARS", 5000))
    auto_dream_on_exit: bool = field(default_factory=lambda: _env_bool("RASP_AUTO_DREAM_ON_EXIT", False))

    fail_streak_to_escalate: int = field(default_factory=lambda: _env_int("RASP_FAIL_STREAK_TO_ESCALATE", 2))

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

    def ensure_dirs(self) -> None:
        for p in [self.data_dir, self.memory_dir, self.wiki_snapshots_dir,
                  self.sessions_dir, self.workspace]:
            p.mkdir(parents=True, exist_ok=True)


CONFIG = Config()
