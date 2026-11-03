"""Shared helpers for the LyraT bench tools: paths, redaction, PowerShell calls."""
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
FW_DIR = BENCH_DIR.parents[1]                 # lyrat_resend/
REPO_DIR = FW_DIR.parent
DEFAULT_PORT = os.environ.get("BENCH_PORT", "COM15")
RUNS_ROOT = Path(os.environ.get("BENCH_RUNS", REPO_DIR / "goals_harness" / "bench_runs"))

_MAC = re.compile(r"\b([0-9a-fA-F]{2}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\b")
_PHONE = re.compile(r"\+?\d[\d\- ]{5,}\d")


def redact(line: str) -> str:
    """Hide MAC prefixes and any caller number; the last two MAC octets stay for correlation."""
    if "CLIP" in line:
        line = _PHONE.sub("<number>", line)
    return _MAC.sub(lambda m: "xx:xx:xx:xx:%s:%s" % (m.group(5), m.group(6)), line)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def run_dir(stage: str, label: str = "") -> Path:
    name = stamp() + ("-" + label if label else "")
    path = RUNS_ROOT / stage / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def powershell(script: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    # ESP-IDF refuses to activate when it sees MSYSTEM, which leaks in from Git Bash.
    env = {k: v for k, v in os.environ.items() if k != "MSYSTEM"}
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
