from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Config


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def run_checks(cfg: Config) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("python", "ok", platform.python_version()))
    checks.append(Check("platform", "ok", f"{platform.machine()} {platform.system()}"))

    cfg.ensure_dirs()
    checks.append(_writable("workspace", cfg.workspace))
    checks.append(_writable("data", cfg.data_dir))
    checks.extend(_ollama_checks(cfg))
    checks.extend(_pi_health_checks())
    checks.append(Check("threads", "ok", f"num_thread={cfg.num_thread} cpu_count={os.cpu_count() or 'unknown'}"))
    checks.append(Check("prompt budgets", "ok", f"wiki={cfg.wiki_prompt_chars} history={cfg.history_prompt_chars} chars"))
    checks.append(Check("tools", "ok", f"native_tools={cfg.native_tools} think_fast={cfg.think_fast!r}"))
    return checks


def _writable(name: str, path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".rasp-check-", delete=True) as f:
            f.write(b"ok")
        return Check(name, "ok", str(path))
    except OSError as e:
        return Check(name, "fail", f"{path}: {e}")


def _ollama_checks(cfg: Config) -> list[Check]:
    checks: list[Check] = []
    if not shutil.which("ollama"):
        checks.append(Check("ollama binary", "warn", "ollama command not found"))
    else:
        checks.append(Check("ollama binary", "ok", shutil.which("ollama") or "found"))

    try:
        r = httpx.get(f"{cfg.ollama_url.rstrip('/')}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]
    except httpx.HTTPError as e:
        checks.append(Check("ollama api", "fail", f"{cfg.ollama_url}: {e}"))
        return checks

    checks.append(Check("ollama api", "ok", cfg.ollama_url))
    for label, model in (("fast model", cfg.model_fast), ("reasoner model", cfg.model_reasoner)):
        if _model_present(model, models):
            checks.append(Check(label, "ok", model))
        else:
            checks.append(Check(label, "warn", f"{model} not pulled/created"))
    return checks


def _model_present(model: str, models: list[str]) -> bool:
    target = model.lower()
    return any(m.lower() == target for m in models)


def _pi_health_checks() -> list[Check]:
    checks: list[Check] = []
    temp_path = Path("/sys/class/thermal/thermal_zone0/temp")
    if temp_path.exists():
        try:
            c = int(temp_path.read_text().strip()) / 1000
            status = "warn" if c >= 70 else "ok"
            checks.append(Check("temperature", status, f"{c:.1f} C"))
        except (OSError, ValueError):
            checks.append(Check("temperature", "warn", "unable to read"))

    vcgencmd = shutil.which("vcgencmd")
    if vcgencmd:
        try:
            proc = subprocess.run(
                [vcgencmd, "get_throttled"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            out = (proc.stdout or proc.stderr).strip()
            status = "ok" if "0x0" in out else "warn"
            checks.append(Check("throttling", status, out or f"exit={proc.returncode}"))
        except (OSError, subprocess.TimeoutExpired) as e:
            checks.append(Check("throttling", "warn", str(e)))
    return checks
