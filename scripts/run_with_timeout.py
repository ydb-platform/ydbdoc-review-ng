from __future__ import annotations

import math
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Sequence

GRACE_SECONDS = 0.5


class WrapperTerminated(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except (PermissionError, ProcessLookupError):
        return False
    return True


def _signal_group(process_group: int, signum: signal.Signals) -> None:
    try:
        os.killpg(process_group, signum)
    except (PermissionError, ProcessLookupError):
        pass


def _cleanup_group(child: subprocess.Popen[bytes]) -> None:
    _signal_group(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + GRACE_SECONDS
    while _group_exists(child.pid) and time.monotonic() < deadline:
        child.poll()
        time.sleep(0.01)
    if _group_exists(child.pid):
        _signal_group(child.pid, signal.SIGKILL)
    try:
        child.wait(timeout=GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(child.pid, signal.SIGKILL)
        child.wait()


def _parse_args(argv: Sequence[str]) -> tuple[float, list[str]] | None:
    if len(argv) < 2:
        print("usage: run_with_timeout.py SECONDS COMMAND...", file=sys.stderr)
        return None
    try:
        timeout = float(argv[0])
    except ValueError:
        print("invalid timeout", file=sys.stderr)
        return None
    if not math.isfinite(timeout) or timeout <= 0:
        print("timeout must be a finite positive number", file=sys.stderr)
        return None
    return timeout, list(argv[1:])


def main() -> int:
    parsed = _parse_args(sys.argv[1:])
    if parsed is None:
        return 2
    timeout, command = parsed

    previous_mask: set[signal.Signals] | None = None
    child: subprocess.Popen[bytes] | None = None
    if os.name == "posix":
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})

    def restore_child_mask() -> None:
        if previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    try:
        if previous_mask is None:
            child = subprocess.Popen(command, start_new_session=True)
        else:
            child = subprocess.Popen(
                command,
                start_new_session=True,
                preexec_fn=restore_child_mask,  # noqa: PLW1509
            )
    except FileNotFoundError as error:
        print(f"command not found: {error.filename}", file=sys.stderr)
        return 127
    except OSError as error:
        print(f"unable to launch command: {error}", file=sys.stderr)
        return 126
    finally:
        if child is None and previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def forward_sigterm(signum: int, frame: object) -> None:
        del frame
        raise WrapperTerminated(signum)

    try:
        previous_handler = signal.signal(signal.SIGTERM, forward_sigterm)
    except BaseException:
        _cleanup_group(child)
        if previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        raise

    mask_is_blocked = previous_mask is not None
    try:
        if previous_mask is not None:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            except WrapperTerminated:
                mask_is_blocked = False
                raise
            mask_is_blocked = False
        return_code = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _cleanup_group(child)
        print(f"timeout after {timeout:g}s: {shlex.join(command)}", file=sys.stderr)
        return 124
    except WrapperTerminated as error:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _cleanup_group(child)
        return 128 + error.signum
    finally:
        if mask_is_blocked and previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        signal.signal(signal.SIGTERM, previous_handler)
    return return_code if return_code >= 0 else 128 + abs(return_code)


if __name__ == "__main__":
    raise SystemExit(main())
