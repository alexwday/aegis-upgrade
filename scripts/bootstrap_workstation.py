#!/usr/bin/env python3
"""Create a local venv, install workstation requirements, and sync env files."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENV = PROJECT_ROOT / ".venv"
REQUIREMENTS = PROJECT_ROOT / "requirements-workstation.txt"


def main() -> int:
    args = parse_args()
    venv_dir = args.venv.expanduser().resolve()

    if not venv_python(venv_dir).exists():
        run([args.python, "-m", "venv", str(venv_dir)], cwd=PROJECT_ROOT)

    py = venv_python(venv_dir)
    if not args.skip_install:
        run([str(py), "-m", "pip", "install", "--upgrade", "pip"], cwd=PROJECT_ROOT)
        run([str(py), "-m", "pip", "install", "-r", str(REQUIREMENTS)], cwd=PROJECT_ROOT)

    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        run([str(py), "scripts/sync_env.py"], cwd=PROJECT_ROOT)
    else:
        print("root .env not found; copy .env.example to .env, fill it, then run:")
        print(f"  {py} scripts/sync_env.py")

    print(f"venv ready: {venv_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, default=DEFAULT_VENV)
    parser.add_argument("--python", default="python3")
    parser.add_argument("--skip-install", action="store_true")
    return parser.parse_args()


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def run(command: list[str], *, cwd: Path) -> None:
    print(f"run    {' '.join(command)}")
    subprocess.run(command, cwd=cwd, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
