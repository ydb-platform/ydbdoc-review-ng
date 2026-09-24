"""Run the official documentation compiler against an unpublished candidate."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from ydbdoc_review_ng.publication import PublicationPlan

_DOCS_PREFIX = "ydb/docs/"
_ERROR = re.compile(r"^ERR .+$")
_MAX_ISSUES = 20


class DiplodocBuildError(RuntimeError):
    """A bounded, public-safe summary of a failed documentation build."""

    def __init__(self, issues: tuple[str, ...], /) -> None:
        self.issues = issues
        super().__init__("diplodoc_build_failed")


class DiplodocBuildValidator:
    """Overlay a publication plan, build it, then restore the checkout."""

    def __init__(
        self,
        docs_root: Path,
        /,
        *,
        command: tuple[str, ...] = ("yfm",),
        timeout_seconds: int = 180,
    ) -> None:
        self.docs_root = docs_root.resolve()
        self.command = command
        self.timeout_seconds = timeout_seconds

    def _candidate_path(self, repository_path: str, /) -> Path:
        if not repository_path.startswith(_DOCS_PREFIX):
            raise DiplodocBuildError(("ERR candidate path is outside ydb/docs",))
        relative = PurePosixPath(repository_path.removeprefix(_DOCS_PREFIX))
        candidate = self.docs_root.joinpath(*relative.parts)
        if not candidate.resolve(strict=False).is_relative_to(self.docs_root):
            raise DiplodocBuildError(("ERR candidate path escapes ydb/docs",))
        return candidate

    @staticmethod
    def _issues(output: str, returncode: int, /) -> tuple[str, ...]:
        issues = tuple(line for line in output.splitlines() if _ERROR.fullmatch(line))
        if issues:
            return issues[:_MAX_ISSUES]
        if returncode:
            return (f"ERR Diplodoc exited with status {returncode}",)
        return ()

    def __call__(self, plan: PublicationPlan, /) -> None:
        changes = tuple(item for item in plan.files if item.before != item.after)
        if not changes:
            return
        originals: list[tuple[Path, bytes | None]] = []
        try:
            for change in changes:
                path = self._candidate_path(change.path.value)
                originals.append((path, path.read_bytes() if path.is_file() else None))
                if change.after is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(change.after)
            with tempfile.TemporaryDirectory(prefix="ydbdoc-diplodoc-") as output_root:
                environment = {
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "NO_COLOR": "1",
                    "PATH": os.environ.get("PATH", ""),
                }
                completed = subprocess.run(
                    (*self.command, "-i", str(self.docs_root), "-o", output_root),
                    cwd=self.docs_root.parents[1],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
                issues = self._issues(completed.stdout, completed.returncode)
                if issues:
                    for issue in issues:
                        print(f"YDBDOC_DIPLODOC {issue}", flush=True)
                    raise DiplodocBuildError(issues)
        except DiplodocBuildError:
            raise
        except (OSError, subprocess.SubprocessError) as error:
            issue = f"ERR Diplodoc invocation failed: {type(error).__name__}"
            raise DiplodocBuildError((issue,)) from None
        finally:
            for path, original in reversed(originals):
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(original)
