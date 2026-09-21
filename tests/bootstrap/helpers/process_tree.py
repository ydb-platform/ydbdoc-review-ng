from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def ignore_term(signum: int, frame: object) -> None:
    del signum, frame


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ready", type=Path)
    parser.add_argument("pids", type=Path)
    parser.add_argument("--ignore-term", action="store_true")
    args = parser.parse_args()

    if args.ignore_term:
        signal.signal(signal.SIGTERM, ignore_term)

    child_code = """
import os
import signal
import time
from pathlib import Path

def ignore(signum, frame):
    pass

if IGNORE:
    signal.signal(signal.SIGTERM, ignore)
Path(PIDS).open("a", encoding="utf-8").write(f"{os.getpid()}\\n")
while True:
    time.sleep(1)
""".replace("IGNORE", repr(args.ignore_term)).replace("PIDS", repr(str(args.pids)))
    subprocess.Popen([sys.executable, "-c", child_code])
    with args.pids.open("a", encoding="utf-8") as stream:
        stream.write(f"{os.getpid()}\n")
    while len(args.pids.read_text(encoding="utf-8").splitlines()) < 2:
        time.sleep(0.01)
    args.ready.write_text("ready", encoding="utf-8")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
