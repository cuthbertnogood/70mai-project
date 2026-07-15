"""Project Python runtime: auto-select .venv (Python 3.10+) for CLI scripts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
MIN_PYTHON = (3, 10)


def venv_python() -> Path | None:
    return VENV_PYTHON if VENV_PYTHON.is_file() else None


def cli_python() -> str:
    """Command string for log hints (prefer project venv)."""
    return ".venv/bin/python" if venv_python() else sys.executable


def _python_candidates() -> list[str]:
    return [
        str(VENV_PYTHON),
        "/usr/local/bin/python3.12",
        "/opt/homebrew/bin/python3.12",
        "python3.12",
        "python3.11",
        "python3.10",
        "python3",
    ]


def _find_bootstrap_python() -> Path | None:
    for candidate in _python_candidates():
        path = Path(candidate)
        if not path.is_file():
            found = subprocess.run(
                ["which", candidate],
                capture_output=True,
                text=True,
                check=False,
            )
            if found.returncode != 0:
                continue
            path = Path(found.stdout.strip())
        try:
            version = subprocess.run(
                [str(path), "-c", "import sys; print(sys.version_info[:2])"],
                capture_output=True,
                text=True,
                check=True,
            )
            major, minor = map(int, version.stdout.strip().strip("()").split(", "))
            if (major, minor) >= MIN_PYTHON:
                return path.resolve()
        except (OSError, subprocess.CalledProcessError, ValueError):
            continue
    return None


def _create_venv() -> None:
    bootstrap = _find_bootstrap_python()
    if bootstrap is None:
        raise SystemExit(
            "Python 3.10+ not found. Install: brew install python@3.12\n"
            "Then run: scripts/setup-venv.sh"
        )
    print(f"Creating .venv with {bootstrap} ...", flush=True)
    subprocess.run([str(bootstrap), "-m", "venv", str(PROJECT_ROOT / ".venv")], check=True)
    if not VENV_PYTHON.is_file():
        raise SystemExit("Failed to create .venv")
    subprocess.run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    if REQUIREMENTS.is_file():
        subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
            check=True,
        )


def ensure_venv_python(*, auto_create: bool = True) -> None:
    """Re-exec the current script with .venv/bin/python (create venv if missing)."""
    if venv_python() is None:
        if not auto_create:
            raise SystemExit(
                "Missing .venv. Run once:\n"
                "  scripts/setup-venv.sh"
            )
        _create_venv()

    current = Path(sys.executable).resolve()
    target = venv_python()
    assert target is not None
    if current != target.resolve():
        os.execv(str(target), [str(target), *sys.argv])
