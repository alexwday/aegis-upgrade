#!/usr/bin/env python3
"""Run one or more source pipelines.

Source finalize stages sync PostgreSQL directly.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from retrieval_source_config import PIPELINE_ROOT, source_keys


SOURCES = source_keys()


def main() -> int:
    args = parse_args()
    source_names = SOURCES if args.all else tuple(args.sources or ())
    if not source_names:
        raise SystemExit("Choose --all or at least one --source.")

    for source_name in source_names:
        run_source(source_name)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        dest="sources",
        choices=SOURCES,
        action="append",
        help="Source pipeline to run. Can be passed multiple times.",
    )
    parser.add_argument("--all", action="store_true", help="Run all source pipelines.")
    return parser.parse_args()


def run_source(source_name: str) -> None:
    source_dir = PIPELINE_ROOT / source_name
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    print(f"\n== {source_name} ==")
    run([sys.executable, "main.py"], cwd=source_dir)


def run(command: list[str], *, cwd: Path) -> None:
    print(f"run    {display_command(command)}  (cwd={cwd})")
    subprocess.run(command, cwd=cwd, check=True)


def display_command(command: list[str]) -> str:
    return " ".join(command)


if __name__ == "__main__":
    raise SystemExit(main())
