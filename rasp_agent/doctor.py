from __future__ import annotations

import os
import platform
import resource
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
    checks.extend(_pi_tuning_checks())
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


_THROTTLE_BITS = (
    (0, "under-voltage now"),
    (1, "freq capped now"),
    (2, "throttled now"),
    (3, "soft temp limit now"),
    (16, "under-voltage since boot"),
    (17, "freq capped since boot"),
    (18, "throttled since boot"),
    (19, "soft temp limit since boot"),
)


def _decode_throttled(value: int) -> str:
    if value == 0:
        return "clean"
    parts = [label for bit, label in _THROTTLE_BITS if value & (1 << bit)]
    return ", ".join(parts) if parts else f"unknown bits 0x{value:x}"


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
            raw = (proc.stdout or proc.stderr).strip()
            value = 0
            if "=" in raw:
                hex_part = raw.split("=", 1)[1]
                try:
                    value = int(hex_part, 16)
                except ValueError:
                    value = 0
            now_bits = value & 0xF
            status = "ok" if value == 0 else ("warn" if now_bits == 0 else "fail")
            checks.append(Check("throttling", status, f"{raw} ({_decode_throttled(value)})"))
        except (OSError, subprocess.TimeoutExpired) as e:
            checks.append(Check("throttling", "warn", str(e)))
    return checks


def _pi_tuning_checks() -> list[Check]:
    """Pi 5 inference perf knobs. Each bad value is ~5-30% slower or risks OOM."""
    checks: list[Check] = []
    gov_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    if gov_path.exists():
        try:
            gov = gov_path.read_text().strip()
            status = "ok" if gov == "performance" else "warn"
            detail = gov if gov == "performance" else f"{gov} (run scripts/tune-pi.sh for 'performance')"
            checks.append(Check("cpu governor", status, detail))
        except OSError:
            pass

    sw_path = Path("/proc/sys/vm/swappiness")
    if sw_path.exists():
        try:
            sw = int(sw_path.read_text().strip())
            status = "ok" if sw <= 10 else "warn"
            detail = str(sw) if status == "ok" else f"{sw} (recommend <=10; run tune-pi.sh)"
            checks.append(Check("swappiness", status, detail))
        except (OSError, ValueError):
            pass

    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
        unlimited = soft == resource.RLIM_INFINITY
        mb = "unlimited" if unlimited else f"{soft // 1024 // 1024} MiB"
        status = "ok" if unlimited or soft >= 4 * 1024 * 1024 * 1024 else "warn"
        checks.append(Check("memlock limit", status, mb))
    except (OSError, ValueError):
        pass

    keep = os.environ.get("OLLAMA_KEEP_ALIVE", "")
    parallel = os.environ.get("OLLAMA_NUM_PARALLEL", "")
    flash = os.environ.get("OLLAMA_FLASH_ATTENTION", "")
    bits = []
    if keep:
        bits.append(f"keep_alive={keep}")
    if parallel:
        bits.append(f"num_parallel={parallel}")
    flash_status = "ok"
    if flash and flash not in ("0", "false", "off"):
        flash_status = "warn"
        bits.append(f"flash_attention={flash} (no-op on ARM, unset)")
    checks.append(Check("ollama env", flash_status, " ".join(bits) or "unset (defaults are fine)"))

    return checks
