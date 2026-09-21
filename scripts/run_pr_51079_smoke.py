#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pr_translation_smoke.runner import run_live


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", type=Path)
    parser.add_argument(
        "--experimental-recovery",
        action="store_true",
        help="retry invalid responses and split a failed chunk into individual fields",
    )
    args = parser.parse_args()
    if args.experimental_recovery:
        __import__("os").environ["YDBDOC_EXPERIMENTAL_RECOVERY"] = "1"
    print(run_live(Path("artifacts"), resume_root=args.resume))
