from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_WORKSPACE = Path.home() / "rasp_agent_ws"


def _env_path(var: str, default: Path) -> Path:
    v = os.environ.get(var)
    return Path(v).expanduser() if v else default


@dataclass(frozen=True)
class Config:
    data_dir: Path = field(default_factory=lambda: _env_path("RASP_DATA_DIR", DEFAULT_DATA_DIR))
    workspace: Path = field(default_factory=lambda: _env_path("RASP_WORKSPACE", DEFAULT_WORKSPACE))

    ollama_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"))
    # Pi 5 reality check (measured on 8 GB Pi, May 2026):
    #   - qwen2.5-coder:1.5b Q4_K_M: ~1 GB resident, ~14 tok/s prefill, fine quality.
    #   - gemma3n:e2b IQ3_XS:        ~2.7 GiB resident, ~4-5 tok/s prefill, slower.
    # k-quants run fast on Pi NEON; IQ-quants are arithmetic-heavy and dominate
    # CPU time. So we use qwen as the fast tier and only escalate to gemma3n
    # for hard problems where the extra wait is worth it.
    model_fast: str = field(default_factory=lambda: os.environ.get("RASP_MODEL_FAST", "qwen2.5-coder:1.5b"))
    model_reasoner: str = field(default_factory=lambda: os.environ.get("RASP_MODEL_REASONER", "gemma3n-e2b-iq3xs"))

    ctx_fast: int = 2048
    ctx_reasoner: int = 4096
    keep_alive: int = -1
    num_thread: int = 4
    num_predict_fast: int = 192
    num_predict_reasoner: int = 384

    bash_allowlist: tuple[str, ...] = (
        "ls", "cat", "head", "tail", "wc", "grep", "find", "tree",
        "pwd", "echo", "which", "diff", "stat", "file",
        "git", "python3", "python", "pip", "pytest", "ruff",
        "node", "npm", "pnpm", "yarn",
        "df", "du", "free", "uname", "uptime", "ps",
        "curl", "jq",
    )
    bash_timeout: int = 30

    dream_min_turns: int = 4
    wiki_prompt_chars: int = 12000

    fail_streak_to_escalate: int = 2

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
