"""Hermetic immutable Git object-database access."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.errors import InvariantViolation
from ydbdoc_review_ng.ports import SnapshotReader

__all__ = [
    "BaseBranch",
    "CommitNotFound",
    "GitCommandResult",
    "GitCommandRunner",
    "GitExecutionError",
    "GitRepository",
    "MutableRepositoryReadForbidden",
    "PullRequestSnapshotRequest",
    "PullRequestState",
    "RepositoryError",
    "RepositoryMismatch",
    "RepositoryObjectError",
    "RepositoryPhaseError",
    "RepositoryReadPhase",
    "ResolvedRepositorySnapshots",
    "ResolvedVerifySnapshots",
    "UnexpectedTreeEntry",
    "UnsupportedGitObjectFormat",
    "VerifySnapshotRequest",
    "resolve_pull_request_snapshots",
    "resolve_verify_snapshots",
]


def _invariant(type_name: str, field_name: str, expectation: str) -> InvariantViolation:
    return InvariantViolation(f"{type_name}.{field_name}: expected {expectation}")


def _exact(value: object, expected: type[object], type_name: str, field_name: str) -> None:
    if type(value) is not expected:
        raise _invariant(type_name, field_name, f"exact {expected.__name__}")


def _optional_exact(value: object, expected: type[object], type_name: str, field_name: str) -> None:
    if value is not None and type(value) is not expected:
        raise _invariant(type_name, field_name, f"None or exact {expected.__name__}")


def _valid_ref_name(value: object, *, allow_slash: bool) -> bool:
    if type(value) is not str or not value or not value.strip() or value != value.strip():
        return False
    if value in {"@", "HEAD"} or value.startswith("-"):
        return False
    if value.startswith("/") or value.endswith("/") or "//" in value:
        return False
    if not allow_slash and "/" in value:
        return False
    if value.endswith(".") or ".." in value or "@{" in value:
        return False
    if "\\" in value or any(character in value for character in "~^:?*["):
        return False
    if any(ord(character) <= 32 or ord(character) == 127 for character in value):
        return False
    components = value.split("/")
    return all(component and not component.startswith(".") and not component.endswith(".lock") for component in components)


class PullRequestState(str, Enum):
    OPEN = "open"
    MERGED = "merged"


class RepositoryReadPhase(str, Enum):
    CAPTURE = "capture"
    PINNED = "pinned"
    APPLIED = "applied"


@dataclass(frozen=True, slots=True)
class BaseBranch:
    value: str

    def __post_init__(self) -> None:
        if not _valid_ref_name(self.value, allow_slash=True):
            raise _invariant("BaseBranch", "value", "a safe GitHub branch name")


@dataclass(frozen=True, slots=True)
class PullRequestSnapshotRequest:
    repository: RepositoryId
    state: PullRequestState
    head_sha: GitSha | None
    base_branch: BaseBranch
    merged_commit_sha: GitSha | None

    def __post_init__(self) -> None:
        _exact(self.repository, RepositoryId, "PullRequestSnapshotRequest", "repository")
        _exact(self.state, PullRequestState, "PullRequestSnapshotRequest", "state")
        _optional_exact(self.head_sha, GitSha, "PullRequestSnapshotRequest", "head_sha")
        _exact(self.base_branch, BaseBranch, "PullRequestSnapshotRequest", "base_branch")
        _optional_exact(
            self.merged_commit_sha, GitSha, "PullRequestSnapshotRequest", "merged_commit_sha"
        )
        if self.state is PullRequestState.OPEN:
            if self.head_sha is None:
                raise _invariant("PullRequestSnapshotRequest", "head_sha", "exact GitSha for OPEN")
            if self.merged_commit_sha is not None:
                raise _invariant("PullRequestSnapshotRequest", "merged_commit_sha", "None for OPEN")
        else:
            if self.head_sha is not None:
                raise _invariant("PullRequestSnapshotRequest", "head_sha", "None for MERGED")
            if self.merged_commit_sha is None:
                raise _invariant(
                    "PullRequestSnapshotRequest", "merged_commit_sha", "exact GitSha for MERGED"
                )


def _snapshot_fields(record: object, names: tuple[str, ...], type_name: str) -> list[SnapshotRef]:
    snapshots: list[SnapshotRef] = []
    for name in names:
        value = getattr(record, name)
        _exact(value, SnapshotRef, type_name, name)
        snapshots.append(value)
    return snapshots


@dataclass(frozen=True, slots=True)
class ResolvedRepositorySnapshots:
    state: PullRequestState
    base_branch: BaseBranch
    pr_snapshot: SnapshotRef
    source_snapshot: SnapshotRef
    target_snapshot: SnapshotRef
    scope_snapshot: SnapshotRef
    translation_base_snapshot: SnapshotRef
    merge_base_with: SnapshotRef | None
    provenance_baseline: SnapshotRef | None

    def __post_init__(self) -> None:
        type_name = "ResolvedRepositorySnapshots"
        _exact(self.state, PullRequestState, type_name, "state")
        _exact(self.base_branch, BaseBranch, type_name, "base_branch")
        snapshots = _snapshot_fields(
            self,
            (
                "pr_snapshot",
                "source_snapshot",
                "target_snapshot",
                "scope_snapshot",
                "translation_base_snapshot",
            ),
            type_name,
        )
        _optional_exact(self.merge_base_with, SnapshotRef, type_name, "merge_base_with")
        _optional_exact(self.provenance_baseline, SnapshotRef, type_name, "provenance_baseline")
        optionals = tuple(
            value
            for value in (self.merge_base_with, self.provenance_baseline)
            if value is not None
        )
        if len({snapshot.repository for snapshot in (*snapshots, *optionals)}) != 1:
            raise _invariant(type_name, "repository", "one repository for every snapshot")
        if self.state is PullRequestState.OPEN:
            if self.merge_base_with is not None or self.provenance_baseline is not None:
                raise _invariant(type_name, "state", "OPEN optional-field table")
            if not (
                self.pr_snapshot
                == self.source_snapshot
                == self.target_snapshot
                == self.scope_snapshot
            ):
                raise _invariant(type_name, "state", "OPEN snapshot table")
        else:
            if self.merge_base_with is None or self.provenance_baseline is None:
                raise _invariant(type_name, "state", "MERGED optional-field table")
            if self.pr_snapshot != self.provenance_baseline:
                raise _invariant(type_name, "provenance_baseline", "the merged PR snapshot")
            if not (
                self.source_snapshot
                == self.target_snapshot
                == self.scope_snapshot
                == self.translation_base_snapshot
                == self.merge_base_with
            ):
                raise _invariant(type_name, "state", "MERGED current-base snapshot table")


@dataclass(frozen=True, slots=True)
class VerifySnapshotRequest:
    source_snapshot: SnapshotRef
    target_sha: GitSha

    def __post_init__(self) -> None:
        _exact(self.source_snapshot, SnapshotRef, "VerifySnapshotRequest", "source_snapshot")
        _exact(self.target_sha, GitSha, "VerifySnapshotRequest", "target_sha")


@dataclass(frozen=True, slots=True)
class ResolvedVerifySnapshots:
    source_snapshot: SnapshotRef
    target_snapshot: SnapshotRef

    def __post_init__(self) -> None:
        _exact(self.source_snapshot, SnapshotRef, "ResolvedVerifySnapshots", "source_snapshot")
        _exact(self.target_snapshot, SnapshotRef, "ResolvedVerifySnapshots", "target_snapshot")
        if self.source_snapshot.repository != self.target_snapshot.repository:
            raise _invariant("ResolvedVerifySnapshots", "target_snapshot", "the source repository")


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    def __post_init__(self) -> None:
        _exact(self.returncode, int, "GitCommandResult", "returncode")
        _exact(self.stdout, bytes, "GitCommandResult", "stdout")
        _exact(self.stderr, bytes, "GitCommandResult", "stderr")


class GitCommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        /,
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> GitCommandResult: ...


class RepositoryError(RuntimeError):
    """Base class for repository-boundary failures."""


class RepositoryPhaseError(RepositoryError):
    def __init__(self, operation: str, phase: RepositoryReadPhase) -> None:
        _exact(operation, str, "RepositoryPhaseError", "operation")
        _exact(phase, RepositoryReadPhase, "RepositoryPhaseError", "phase")
        self.operation = operation
        self.phase = phase
        super().__init__(f"repository operation {operation} is forbidden in {phase.value} phase")


class MutableRepositoryReadForbidden(RepositoryPhaseError):
    pass


class RepositoryObjectError(RepositoryError):
    pass


class CommitNotFound(RepositoryObjectError):
    def __init__(self, repository: RepositoryId, commit_sha: GitSha) -> None:
        _exact(repository, RepositoryId, "CommitNotFound", "repository")
        _exact(commit_sha, GitSha, "CommitNotFound", "commit_sha")
        self.repository = repository
        self.commit_sha = commit_sha
        super().__init__(f"commit {commit_sha.value} was not found in repository {repository.value}")


class UnexpectedTreeEntry(RepositoryObjectError):
    def __init__(self, path: RepoPath, actual_type: str) -> None:
        _exact(path, RepoPath, "UnexpectedTreeEntry", "path")
        if actual_type not in {"tree", "gitlink", "non_blob"}:
            raise _invariant("UnexpectedTreeEntry", "actual_type", "tree, gitlink, or non_blob")
        self.path = path
        self.actual_type = actual_type
        super().__init__(f"path {path.value} is a {actual_type}, not a blob")


class UnsupportedGitObjectFormat(RepositoryObjectError):
    def __init__(self, object_format: Literal["sha256"] = "sha256") -> None:
        if type(object_format) is not str or object_format != "sha256":
            raise _invariant("UnsupportedGitObjectFormat", "object_format", "the literal sha256")
        self.object_format = object_format
        super().__init__("unsupported Git object format: sha256")


class RepositoryMismatch(RepositoryError):
    def __init__(self, expected: RepositoryId, actual: RepositoryId) -> None:
        _exact(expected, RepositoryId, "RepositoryMismatch", "expected")
        _exact(actual, RepositoryId, "RepositoryMismatch", "actual")
        self.expected = expected
        self.actual = actual
        super().__init__(f"repository mismatch: expected {expected.value}, got {actual.value}")


_OPERATIONS = {
    "object_format",
    "pin_commit",
    "capture_base_tip",
    "ls_tree",
    "cat_file_blob",
    "is_ancestor",
    "commits_touching",
}
_CATEGORIES = {"launch_failed", "exit_status", "malformed_output", "invalid_runner_result"}


class GitExecutionError(RepositoryError):
    def __init__(self, operation: str, category: str, returncode: int | None) -> None:
        if type(operation) is not str or operation not in _OPERATIONS:
            raise _invariant("GitExecutionError", "operation", "an allowlisted operation")
        if type(category) is not str or category not in _CATEGORIES:
            raise _invariant("GitExecutionError", "category", "a supported safe category")
        if returncode is not None and type(returncode) is not int:
            raise _invariant("GitExecutionError", "returncode", "None or exact int")
        if category in {"launch_failed", "invalid_runner_result"}:
            if returncode is not None:
                raise _invariant("GitExecutionError", "returncode", "None for launch failure")
        elif returncode is None:
            raise _invariant("GitExecutionError", "returncode", "an exact child status")
        self.operation = operation
        self.category = category
        self.returncode = returncode
        status = "none" if returncode is None else str(returncode)
        super().__init__(f"Git operation {operation} failed: {category} (status {status})")


class _SubprocessGitCommandRunner:
    def run(
        self,
        argv: tuple[str, ...],
        /,
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> GitCommandResult:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env),
            shell=False,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        return GitCommandResult(completed.returncode, completed.stdout, completed.stderr)


_SHA_LINE = re.compile(rb"[0-9a-f]{40}\n\Z")
_TREE_RECORD = re.compile(
    rb"(?P<mode>[0-7]{6}) (?P<kind>blob|tree|commit) (?P<oid>[0-9a-f]{40})\t(?P<path>.*)\0\Z",
    re.DOTALL,
)


class GitRepository(SnapshotReader):
    def __init__(
        self,
        repository: RepositoryId,
        git_dir: Path,
        /,
        *,
        remote_name: str = "origin",
        command_runner: GitCommandRunner | None = None,
    ) -> None:
        _exact(repository, RepositoryId, "GitRepository", "repository")
        if not isinstance(git_dir, Path) or not git_dir.is_absolute():
            raise _invariant("GitRepository", "git_dir", "an absolute existing Git directory Path")
        try:
            resolved_git_dir = git_dir.resolve(strict=True)
        except OSError as error:
            raise _invariant(
                "GitRepository", "git_dir", "an absolute existing Git directory Path"
            ) from error
        if not resolved_git_dir.is_dir():
            raise _invariant("GitRepository", "git_dir", "an absolute existing Git directory Path")
        if not _valid_ref_name(remote_name, allow_slash=False):
            raise _invariant("GitRepository", "remote_name", "one safe Git ref component")
        if command_runner is not None and not hasattr(command_runner, "run"):
            raise _invariant("GitRepository", "command_runner", "a GitCommandRunner or None")

        self._repository = repository
        self._git_dir = resolved_git_dir
        self._remote_name = remote_name
        self._runner = command_runner if command_runner is not None else _SubprocessGitCommandRunner()
        self._phase = RepositoryReadPhase.CAPTURE
        self._base_captured = False
        self._env = self._build_environment()
        probe = self._run(
            "object_format",
            ("git", f"--git-dir={self._git_dir}", "rev-parse", "--show-object-format"),
        )
        if probe.returncode != 0:
            raise GitExecutionError("object_format", "exit_status", probe.returncode)
        if probe.stdout == b"sha256\n":
            raise UnsupportedGitObjectFormat()
        if probe.stdout != b"sha1\n":
            raise GitExecutionError("object_format", "malformed_output", probe.returncode)

    @staticmethod
    def _build_environment() -> Mapping[str, str]:
        allowed = ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC")
        environment = {key: os.environ[key] for key in allowed if key in os.environ}
        environment.setdefault("PATH", os.defpath)
        environment.update(
            {
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0",
                "LC_ALL": "C",
            }
        )
        return MappingProxyType(environment)

    @property
    def phase(self) -> RepositoryReadPhase:
        return self._phase

    def _run(self, operation: str, argv: tuple[str, ...]) -> GitCommandResult:
        if type(argv) is not tuple or any(type(value) is not str for value in argv):
            raise _invariant("GitRepository", "argv", "a tuple of exact strings")
        try:
            result = self._runner.run(argv, cwd=self._git_dir, env=self._env)
        except OSError as error:
            raise GitExecutionError(operation, "launch_failed", None) from error
        if type(result) is not GitCommandResult:
            raise GitExecutionError(operation, "invalid_runner_result", None)
        return result

    def _require_capture(self, operation: str) -> None:
        if self._phase is not RepositoryReadPhase.CAPTURE:
            raise RepositoryPhaseError(operation, self._phase)

    def _require_immutable(self, operation: str) -> None:
        if self._phase is RepositoryReadPhase.CAPTURE:
            raise MutableRepositoryReadForbidden(operation, self._phase)

    def _check_snapshot(self, snapshot: SnapshotRef) -> None:
        _exact(snapshot, SnapshotRef, "GitRepository", "snapshot")
        if snapshot.repository != self._repository:
            raise RepositoryMismatch(self._repository, snapshot.repository)

    def pin_commit(self, commit_sha: GitSha, /) -> SnapshotRef:
        self._require_capture("pin_commit")
        _exact(commit_sha, GitSha, "GitRepository", "commit_sha")
        result = self._run(
            "pin_commit",
            ("git", f"--git-dir={self._git_dir}", "cat-file", "-e", f"{commit_sha.value}^{{commit}}"),
        )
        if result.returncode == 128:
            raise CommitNotFound(self._repository, commit_sha)
        if result.returncode != 0:
            raise GitExecutionError("pin_commit", "exit_status", result.returncode)
        if result.stdout:
            raise GitExecutionError("pin_commit", "malformed_output", result.returncode)
        return SnapshotRef(self._repository, commit_sha)

    def capture_base_tip(self, branch: BaseBranch, /) -> SnapshotRef:
        self._require_capture("capture_base_tip")
        _exact(branch, BaseBranch, "GitRepository", "branch")
        if self._base_captured:
            raise RepositoryPhaseError("capture_base_tip", self._phase)
        full_ref = f"refs/remotes/{self._remote_name}/{branch.value}"
        result = self._run(
            "capture_base_tip",
            (
                "git",
                f"--git-dir={self._git_dir}",
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{full_ref}^{{commit}}",
            ),
        )
        if result.returncode != 0:
            raise GitExecutionError("capture_base_tip", "exit_status", result.returncode)
        if _SHA_LINE.fullmatch(result.stdout) is None:
            raise GitExecutionError("capture_base_tip", "malformed_output", result.returncode)
        self._base_captured = True
        return SnapshotRef(self._repository, GitSha(result.stdout[:-1].decode("ascii")))

    def seal(self, /) -> None:
        if self._phase is RepositoryReadPhase.CAPTURE:
            self._phase = RepositoryReadPhase.PINNED
        elif self._phase is RepositoryReadPhase.APPLIED:
            raise RepositoryPhaseError("seal", self._phase)

    def mark_applied(self, /) -> None:
        if self._phase is not RepositoryReadPhase.PINNED:
            raise RepositoryPhaseError("mark_applied", self._phase)
        self._phase = RepositoryReadPhase.APPLIED

    def assert_worktree_read_allowed(self, /) -> None:
        if self._phase is not RepositoryReadPhase.APPLIED:
            raise MutableRepositoryReadForbidden("worktree_read", self._phase)

    def read_bytes(self, snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None:
        self._require_immutable("read_bytes")
        self._check_snapshot(snapshot)
        _exact(path, RepoPath, "GitRepository", "path")
        try:
            encoded_path = os.fsencode(path.value)
        except (OSError, UnicodeError) as error:
            raise GitExecutionError("ls_tree", "launch_failed", None) from error
        result = self._run(
            "ls_tree",
            (
                "git",
                "--literal-pathspecs",
                f"--git-dir={self._git_dir}",
                "ls-tree",
                "-z",
                "--full-tree",
                snapshot.commit_sha.value,
                "--",
                path.value,
            ),
        )
        if result.returncode != 0:
            raise GitExecutionError("ls_tree", "exit_status", result.returncode)
        if result.stdout == b"":
            return None
        match = _TREE_RECORD.fullmatch(result.stdout)
        if match is None or match.group("path") != encoded_path:
            raise GitExecutionError("ls_tree", "malformed_output", result.returncode)
        kind = match.group("kind")
        if kind != b"blob":
            actual = "tree" if kind == b"tree" else "gitlink" if match.group("mode") == b"160000" else "non_blob"
            raise UnexpectedTreeEntry(path, actual)
        object_id = match.group("oid").decode("ascii")
        blob = self._run(
            "cat_file_blob",
            ("git", f"--git-dir={self._git_dir}", "cat-file", "blob", object_id),
        )
        if blob.returncode != 0:
            raise GitExecutionError("cat_file_blob", "exit_status", blob.returncode)
        return blob.stdout

    def is_ancestor(self, older: SnapshotRef, newer: SnapshotRef, /) -> bool:
        self._require_immutable("is_ancestor")
        self._check_snapshot(older)
        self._check_snapshot(newer)
        result = self._run(
            "is_ancestor",
            (
                "git",
                f"--git-dir={self._git_dir}",
                "merge-base",
                "--is-ancestor",
                older.commit_sha.value,
                newer.commit_sha.value,
            ),
        )
        if result.returncode not in {0, 1}:
            raise GitExecutionError("is_ancestor", "exit_status", result.returncode)
        if result.stdout:
            raise GitExecutionError("is_ancestor", "malformed_output", result.returncode)
        return result.returncode == 0

    def commits_touching(
        self,
        older: SnapshotRef,
        newer: SnapshotRef,
        path: RepoPath,
        /,
    ) -> tuple[GitSha, ...]:
        self._require_immutable("commits_touching")
        self._check_snapshot(older)
        self._check_snapshot(newer)
        _exact(path, RepoPath, "GitRepository", "path")
        result = self._run(
            "commits_touching",
            (
                "git",
                "--literal-pathspecs",
                f"--git-dir={self._git_dir}",
                "log",
                "--format=%H",
                "--topo-order",
                "--reverse",
                "--no-renames",
                f"{older.commit_sha.value}..{newer.commit_sha.value}",
                "--",
                path.value,
            ),
        )
        if result.returncode != 0:
            raise GitExecutionError("commits_touching", "exit_status", result.returncode)
        if result.stdout == b"":
            return ()
        if not result.stdout.endswith(b"\n"):
            raise GitExecutionError("commits_touching", "malformed_output", result.returncode)
        lines = result.stdout[:-1].split(b"\n")
        if any(re.fullmatch(rb"[0-9a-f]{40}", line) is None for line in lines):
            raise GitExecutionError("commits_touching", "malformed_output", result.returncode)
        decoded = tuple(GitSha(line.decode("ascii")) for line in lines)
        if len(set(decoded)) != len(decoded):
            raise GitExecutionError("commits_touching", "malformed_output", result.returncode)
        return decoded


def resolve_pull_request_snapshots(
    repository: GitRepository,
    request: PullRequestSnapshotRequest,
    /,
) -> ResolvedRepositorySnapshots:
    _exact(repository, GitRepository, "resolve_pull_request_snapshots", "repository")
    _exact(request, PullRequestSnapshotRequest, "resolve_pull_request_snapshots", "request")
    if request.repository != repository._repository:
        raise RepositoryMismatch(repository._repository, request.repository)
    if request.state is PullRequestState.OPEN:
        assert request.head_sha is not None
        pr_snapshot = repository.pin_commit(request.head_sha)
    else:
        assert request.merged_commit_sha is not None
        pr_snapshot = repository.pin_commit(request.merged_commit_sha)
    current_base = repository.capture_base_tip(request.base_branch)
    repository.seal()
    if request.state is PullRequestState.OPEN:
        return ResolvedRepositorySnapshots(
            request.state,
            request.base_branch,
            pr_snapshot,
            pr_snapshot,
            pr_snapshot,
            pr_snapshot,
            current_base,
            None,
            None,
        )
    return ResolvedRepositorySnapshots(
        request.state,
        request.base_branch,
        pr_snapshot,
        current_base,
        current_base,
        current_base,
        current_base,
        current_base,
        pr_snapshot,
    )


def resolve_verify_snapshots(
    repository: GitRepository,
    request: VerifySnapshotRequest,
    /,
) -> ResolvedVerifySnapshots:
    _exact(repository, GitRepository, "resolve_verify_snapshots", "repository")
    _exact(request, VerifySnapshotRequest, "resolve_verify_snapshots", "request")
    if request.source_snapshot.repository != repository._repository:
        raise RepositoryMismatch(repository._repository, request.source_snapshot.repository)
    repository.pin_commit(request.source_snapshot.commit_sha)
    target = repository.pin_commit(request.target_sha)
    repository.seal()
    return ResolvedVerifySnapshots(request.source_snapshot, target)
