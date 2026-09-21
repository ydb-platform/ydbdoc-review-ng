from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.errors import InvariantViolation
from ydbdoc_review_ng.provenance import (
    PathHistoryState,
    ProvenanceAssessment,
    ProvenancePath,
    ProvenanceRole,
    compare_provenance,
)
from ydbdoc_review_ng.repository import (
    BaseBranch,
    CommitNotFound,
    GitCommandResult,
    GitExecutionError,
    GitRepository,
    MutableRepositoryReadForbidden,
    PullRequestSnapshotRequest,
    PullRequestState,
    RepositoryMismatch,
    RepositoryPhaseError,
    RepositoryReadPhase,
    ResolvedRepositorySnapshots,
    ResolvedVerifySnapshots,
    UnexpectedTreeEntry,
    UnsupportedGitObjectFormat,
    VerifySnapshotRequest,
    resolve_pull_request_snapshots,
    resolve_verify_snapshots,
)

REPOSITORY = RepositoryId("owner/repo")
SHA1 = GitSha("1" * 40)
SHA2 = GitSha("2" * 40)
SHA3 = GitSha("3" * 40)
SNAP1 = SnapshotRef(REPOSITORY, SHA1)


class StringSubclass(str):
    pass


class BytesSubclass(bytes):
    pass


class RepositoryIdSubclass(RepositoryId):
    pass


class GitShaSubclass(GitSha):
    pass


class BaseBranchSubclass(BaseBranch):
    pass


class SnapshotRefSubclass(SnapshotRef):
    pass


class QueueRunner:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.calls: list[tuple[tuple[str, ...], Path, Mapping[str, str]]] = []

    def run(self, argv: tuple[str, ...], /, *, cwd: Path, env: Mapping[str, str]) -> GitCommandResult:
        self.calls.append((argv, cwd, env))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return cast(GitCommandResult, result)


class CanaryWrongRunnerResult:
    def __repr__(self) -> str:
        return "wrong-runner-result-secret-canary"


def ok(stdout: bytes = b"") -> GitCommandResult:
    return GitCommandResult(0, stdout, b"")


def repository(
    tmp_path: Path, results: list[object], **kwargs: object
) -> tuple[GitRepository, QueueRunner]:
    git_dir = tmp_path / "objects.git"
    git_dir.mkdir(parents=True)
    runner = QueueRunner([ok(b"sha1\n"), *results])
    repo = GitRepository(REPOSITORY, git_dir, command_runner=runner, **kwargs)  # type: ignore[arg-type]
    return repo, runner


def test_records_and_resolution_table(tmp_path: Path) -> None:
    repo, runner = repository(
        tmp_path,
        [ok(), ok(SHA2.value.encode() + b"\n")],
    )
    request = PullRequestSnapshotRequest(
        REPOSITORY, PullRequestState.OPEN, SHA1, BaseBranch("release/26-1"), None
    )
    resolved = resolve_pull_request_snapshots(repo, request)
    assert resolved == ResolvedRepositorySnapshots(
        PullRequestState.OPEN,
        request.base_branch,
        SNAP1,
        SNAP1,
        SNAP1,
        SNAP1,
        SnapshotRef(REPOSITORY, SHA2),
        None,
        None,
    )
    assert repo.phase is RepositoryReadPhase.PINNED
    assert sum("rev-parse" in call[0] and "--verify" in call[0] for call in runner.calls) == 1

    repo2, _ = repository(tmp_path / "merged", [ok(), ok(SHA2.value.encode() + b"\n")])
    merged = resolve_pull_request_snapshots(
        repo2,
        PullRequestSnapshotRequest(
            REPOSITORY, PullRequestState.MERGED, None, BaseBranch("main"), SHA1
        ),
    )
    current = SnapshotRef(REPOSITORY, SHA2)
    assert merged == ResolvedRepositorySnapshots(
        PullRequestState.MERGED,
        BaseBranch("main"),
        SNAP1,
        current,
        current,
        current,
        current,
        current,
        SNAP1,
    )

    repo3, runner3 = repository(tmp_path / "verify", [ok(), ok()])
    verified = resolve_verify_snapshots(repo3, VerifySnapshotRequest(SNAP1, SHA2))
    assert verified == ResolvedVerifySnapshots(SNAP1, current)
    assert verified.source_snapshot is SNAP1
    assert all("--verify" not in call[0] for call in runner3.calls)


def test_base_branch_contract_and_enum_wire_values_are_exact() -> None:
    branch = BaseBranch("release/26-1")
    assert branch.value == "release/26-1"
    assert tuple(state.value for state in PullRequestState) == ("open", "merged")
    assert tuple(phase.value for phase in RepositoryReadPhase) == (
        "capture",
        "pinned",
        "applied",
    )
    for invalid in (
        "",
        "HEAD",
        "@",
        "-main",
        "/main",
        "main/",
        "a//b",
        ".hidden",
        "a.lock",
        "a..b",
        "a@{b",
        "a~b",
        "a b",
        "a\\b",
        StringSubclass("main"),
    ):
        with pytest.raises(InvariantViolation):
            BaseBranch(invalid)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PullRequestSnapshotRequest(
            REPOSITORY, PullRequestState.OPEN, None, BaseBranch("main"), None
        ),
        lambda: PullRequestSnapshotRequest(
            REPOSITORY, PullRequestState.OPEN, SHA1, BaseBranch("main"), SHA2
        ),
        lambda: PullRequestSnapshotRequest(
            REPOSITORY, PullRequestState.MERGED, SHA1, BaseBranch("main"), SHA2
        ),
        lambda: PullRequestSnapshotRequest(
            REPOSITORY, PullRequestState.MERGED, None, BaseBranch("main"), None
        ),
    ],
)
def test_pull_request_request_rejects_every_open_merged_xor_violation(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(InvariantViolation):
        factory()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PullRequestSnapshotRequest(
            RepositoryIdSubclass("owner/repo"),
            PullRequestState.OPEN,
            SHA1,
            BaseBranch("main"),
            None,
        ),
        lambda: PullRequestSnapshotRequest(
            REPOSITORY, "open", SHA1, BaseBranch("main"), None
        ),
        lambda: PullRequestSnapshotRequest(
            REPOSITORY,
            PullRequestState.OPEN,
            GitShaSubclass("1" * 40),
            BaseBranch("main"),
            None,
        ),
        lambda: PullRequestSnapshotRequest(
            REPOSITORY,
            PullRequestState.OPEN,
            SHA1,
            BaseBranchSubclass("main"),
            None,
        ),
        lambda: PullRequestSnapshotRequest(
            REPOSITORY,
            PullRequestState.MERGED,
            None,
            BaseBranch("main"),
            GitShaSubclass("2" * 40),
        ),
        lambda: VerifySnapshotRequest(
            SnapshotRefSubclass(REPOSITORY, SHA1), SHA2
        ),
        lambda: VerifySnapshotRequest(SNAP1, GitShaSubclass("2" * 40)),
        lambda: ResolvedVerifySnapshots(
            SnapshotRefSubclass(REPOSITORY, SHA1),
            SnapshotRef(REPOSITORY, SHA2),
        ),
        lambda: ResolvedVerifySnapshots(
            SNAP1, SnapshotRefSubclass(REPOSITORY, SHA2)
        ),
    ],
)
def test_snapshot_request_records_reject_non_exact_runtime_types(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(InvariantViolation):
        factory()


def resolved_kwargs(state: PullRequestState) -> dict[str, object]:
    branch = BaseBranch("main")
    current = SnapshotRef(REPOSITORY, SHA2)
    if state is PullRequestState.OPEN:
        return {
            "state": state,
            "base_branch": branch,
            "pr_snapshot": SNAP1,
            "source_snapshot": SNAP1,
            "target_snapshot": SNAP1,
            "scope_snapshot": SNAP1,
            "translation_base_snapshot": current,
            "merge_base_with": None,
            "provenance_baseline": None,
        }
    return {
        "state": state,
        "base_branch": branch,
        "pr_snapshot": SNAP1,
        "source_snapshot": current,
        "target_snapshot": current,
        "scope_snapshot": current,
        "translation_base_snapshot": current,
        "merge_base_with": current,
        "provenance_baseline": SNAP1,
    }


@pytest.mark.parametrize(
    "state,field_name,bad",
    [
        (PullRequestState.OPEN, "merge_base_with", SNAP1),
        (PullRequestState.OPEN, "provenance_baseline", SNAP1),
        (PullRequestState.OPEN, "pr_snapshot", SnapshotRef(REPOSITORY, SHA3)),
        (PullRequestState.OPEN, "source_snapshot", SnapshotRef(REPOSITORY, SHA3)),
        (PullRequestState.OPEN, "target_snapshot", SnapshotRef(REPOSITORY, SHA3)),
        (PullRequestState.OPEN, "scope_snapshot", SnapshotRef(REPOSITORY, SHA3)),
        (PullRequestState.MERGED, "merge_base_with", None),
        (PullRequestState.MERGED, "provenance_baseline", None),
        (
            PullRequestState.MERGED,
            "provenance_baseline",
            SnapshotRef(REPOSITORY, SHA3),
        ),
        (PullRequestState.MERGED, "source_snapshot", SnapshotRef(REPOSITORY, SHA3)),
        (PullRequestState.MERGED, "target_snapshot", SnapshotRef(REPOSITORY, SHA3)),
        (PullRequestState.MERGED, "scope_snapshot", SnapshotRef(REPOSITORY, SHA3)),
        (
            PullRequestState.MERGED,
            "translation_base_snapshot",
            SnapshotRef(REPOSITORY, SHA3),
        ),
        (PullRequestState.MERGED, "merge_base_with", SnapshotRef(REPOSITORY, SHA3)),
    ],
)
def test_resolved_snapshot_records_reject_every_state_table_contradiction(
    state: PullRequestState, field_name: str, bad: object
) -> None:
    kwargs = resolved_kwargs(state)
    kwargs[field_name] = bad
    with pytest.raises(InvariantViolation):
        ResolvedRepositorySnapshots(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "state,field_name",
    [
        *(
            (PullRequestState.OPEN, field_name)
            for field_name in (
                "pr_snapshot",
                "source_snapshot",
                "target_snapshot",
                "scope_snapshot",
                "translation_base_snapshot",
            )
        ),
        *(
            (PullRequestState.MERGED, field_name)
            for field_name in (
                "pr_snapshot",
                "source_snapshot",
                "target_snapshot",
                "scope_snapshot",
                "translation_base_snapshot",
                "merge_base_with",
                "provenance_baseline",
            )
        ),
    ],
)
def test_resolved_snapshot_records_reject_every_repository_contradiction(
    state: PullRequestState, field_name: str
) -> None:
    kwargs = resolved_kwargs(state)
    kwargs[field_name] = SnapshotRef(RepositoryId("other/repo"), SHA3)
    with pytest.raises(InvariantViolation):
        ResolvedRepositorySnapshots(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field_name,bad",
    [
        ("state", "open"),
        ("base_branch", BaseBranchSubclass("main")),
        ("pr_snapshot", SnapshotRefSubclass(REPOSITORY, SHA1)),
        ("source_snapshot", SnapshotRefSubclass(REPOSITORY, SHA1)),
        ("target_snapshot", SnapshotRefSubclass(REPOSITORY, SHA1)),
        ("scope_snapshot", SnapshotRefSubclass(REPOSITORY, SHA1)),
        (
            "translation_base_snapshot",
            SnapshotRefSubclass(REPOSITORY, SHA2),
        ),
        ("merge_base_with", SnapshotRefSubclass(REPOSITORY, SHA1)),
        ("provenance_baseline", SnapshotRefSubclass(REPOSITORY, SHA1)),
    ],
)
def test_resolved_snapshot_record_rejects_non_exact_runtime_types(
    field_name: str, bad: object
) -> None:
    kwargs = resolved_kwargs(PullRequestState.OPEN)
    kwargs[field_name] = bad
    with pytest.raises(InvariantViolation):
        ResolvedRepositorySnapshots(**kwargs)  # type: ignore[arg-type]


def test_resolved_verify_snapshots_reject_mixed_repositories() -> None:
    other = SnapshotRef(RepositoryId("other/repo"), SHA2)
    with pytest.raises(InvariantViolation):
        ResolvedVerifySnapshots(SNAP1, other)


@pytest.mark.parametrize(
    "record,field_name",
    [
        (BaseBranch("main"), "value"),
        (
            PullRequestSnapshotRequest(
                REPOSITORY, PullRequestState.OPEN, SHA1, BaseBranch("main"), None
            ),
            "state",
        ),
        (
            ResolvedRepositorySnapshots(**resolved_kwargs(PullRequestState.OPEN)),
            "state",
        ),
        (VerifySnapshotRequest(SNAP1, SHA2), "target_sha"),
        (
            ResolvedVerifySnapshots(SNAP1, SnapshotRef(REPOSITORY, SHA2)),
            "target_snapshot",
        ),
        (GitCommandResult(0, b"", b""), "returncode"),
    ],
)
def test_every_repository_record_rejects_assignment(
    record: object, field_name: str
) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(record, field_name, object())


def test_git_result_and_error_value_invariants() -> None:
    for args in (
        (True, b"", b""),
        (0, bytearray(), b""),
        (0, b"", bytearray()),
        (0, BytesSubclass(b""), b""),
        (0, b"", BytesSubclass(b"")),
    ):
        with pytest.raises(InvariantViolation):
            GitCommandResult(*args)  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation):
        UnsupportedGitObjectFormat("sha1")  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation):
        GitExecutionError("x", "secret", None)


def test_phase_guard_and_repository_mismatch_precede_spawn(tmp_path: Path) -> None:
    repo, runner = repository(tmp_path, [ok(), ok(), ok(), ok()])
    with pytest.raises(MutableRepositoryReadForbidden):
        repo.read_bytes(SNAP1, RepoPath("a.md"))
    with pytest.raises(MutableRepositoryReadForbidden):
        repo.assert_worktree_read_allowed()
    repo.pin_commit(SHA1)
    repo.seal()
    before = len(runner.calls)
    with pytest.raises(RepositoryPhaseError):
        repo.capture_base_tip(BaseBranch("main"))
    with pytest.raises(RepositoryMismatch):
        repo.read_bytes(SnapshotRef(RepositoryId("other/repo"), SHA1), RepoPath("a.md"))
    assert len(runner.calls) == before
    repo.seal()
    repo.mark_applied()
    repo.assert_worktree_read_allowed()
    with pytest.raises(RepositoryPhaseError):
        repo.seal()
    with pytest.raises(RepositoryPhaseError):
        repo.mark_applied()


@pytest.mark.parametrize(
    "status,stdout,error",
    [(128, b"", CommitNotFound), (2, b"", GitExecutionError), (-1, b"", GitExecutionError), (0, b"canary", GitExecutionError)],
)
def test_pin_commit_status_matrix(tmp_path: Path, status: int, stdout: bytes, error: type[Exception]) -> None:
    repo, _ = repository(tmp_path, [GitCommandResult(status, stdout, b"stderr-canary")])
    with pytest.raises(error) as caught:
        repo.pin_commit(SHA1)
    assert "canary" not in (str(caught.value) + repr(caught.value))


@pytest.mark.parametrize("stdout", [b"", b"A" * 40 + b"\n", b"1" * 40, b"1" * 40 + b"\n2" * 40 + b"\n", b"1" * 40 + b"\nextra"])
def test_base_ref_strict_parser(tmp_path: Path, stdout: bytes) -> None:
    repo, _ = repository(tmp_path, [GitCommandResult(0, stdout, b"")])
    with pytest.raises(GitExecutionError):
        repo.capture_base_tip(BaseBranch("main"))


def tree_record(kind: bytes = b"blob", path: bytes = b"docs/a.md", oid: bytes = b"a" * 40, mode: bytes = b"100644") -> bytes:
    return mode + b" " + kind + b" " + oid + b"\t" + path + b"\0"


COMMAND_OPERATIONS = (
    "object_format",
    "pin_commit",
    "capture_base_tip",
    "ls_tree",
    "cat_file_blob",
    "is_ancestor",
    "commits_touching",
)


def invoke_with_result(tmp_path: Path, operation: str, result: object) -> None:
    if operation == "object_format":
        git_dir = tmp_path / "objects.git"
        git_dir.mkdir(parents=True)
        GitRepository(REPOSITORY, git_dir, command_runner=QueueRunner([result]))
        return

    setup: list[object] = []
    if operation in {"pin_commit", "capture_base_tip"}:
        setup = [result]
    elif operation == "ls_tree":
        setup = [ok(), result]
    elif operation == "cat_file_blob":
        setup = [ok(), ok(tree_record()), result]
    elif operation in {"is_ancestor", "commits_touching"}:
        setup = [ok(), result]
    else:
        raise AssertionError(f"unknown test operation {operation}")

    repo, _ = repository(tmp_path, setup)
    if operation == "pin_commit":
        repo.pin_commit(SHA1)
        return
    if operation == "capture_base_tip":
        repo.capture_base_tip(BaseBranch("main"))
        return
    repo.pin_commit(SHA1)
    repo.seal()
    if operation in {"ls_tree", "cat_file_blob"}:
        repo.read_bytes(SNAP1, RepoPath("docs/a.md"))
    elif operation == "is_ancestor":
        repo.is_ancestor(SNAP1, SnapshotRef(REPOSITORY, SHA2))
    else:
        repo.commits_touching(
            SNAP1, SnapshotRef(REPOSITORY, SHA2), RepoPath("docs/a.md")
        )


def assert_non_secret(error: BaseException) -> None:
    rendered = str(error) + repr(error)
    for canary in (
        "stdout-secret-canary",
        "stderr-secret-canary",
        "environment-secret-canary",
        "git-dir-secret-canary",
        "oserror-secret-canary",
        "wrong-runner-result-secret-canary",
    ):
        assert canary not in rendered


@pytest.mark.parametrize(
    "stdout,error",
    [
        (tree_record(b"tree", mode=b"040000"), UnexpectedTreeEntry),
        (tree_record(b"commit", mode=b"160000"), UnexpectedTreeEntry),
        (b"bad\0", GitExecutionError),
        (tree_record(path=b"other"), GitExecutionError),
        (tree_record(oid=b"Z" * 40), GitExecutionError),
        (tree_record() + tree_record(), GitExecutionError),
        (tree_record() + b"x", GitExecutionError),
    ],
)
def test_ls_tree_rejects_non_blob_and_malformed(tmp_path: Path, stdout: bytes, error: type[Exception]) -> None:
    repo, _ = repository(tmp_path, [ok(), ok(stdout)])
    repo.pin_commit(SHA1)
    repo.seal()
    with pytest.raises(error):
        repo.read_bytes(SNAP1, RepoPath("docs/a.md"))


def test_missing_empty_and_exact_blob_semantics(tmp_path: Path) -> None:
    repo, runner = repository(
        tmp_path,
        [ok(), ok(b""), ok(tree_record()), ok(b""), ok(tree_record(path=b"docs/b.md")), ok(b"exact\x00bytes")],
    )
    repo.pin_commit(SHA1)
    repo.seal()
    assert repo.read_bytes(SNAP1, RepoPath("docs/a.md")) is None
    assert repo.read_bytes(SNAP1, RepoPath("docs/a.md")) == b""
    assert repo.read_bytes(SNAP1, RepoPath("docs/b.md")) == b"exact\x00bytes"
    assert all(type(arg) is str for call, _, _ in runner.calls for arg in call)


def test_symlink_mode_reads_stored_link_target_bytes(tmp_path: Path) -> None:
    repo, _ = repository(
        tmp_path,
        [
            ok(),
            ok(tree_record(path=b"docs/link.md", mode=b"120000")),
            ok(b"target.md"),
        ],
    )
    repo.pin_commit(SHA1)
    repo.seal()
    assert repo.read_bytes(SNAP1, RepoPath("docs/link.md")) == b"target.md"


def test_literal_path_and_sanitized_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launch_values = {
        "PATH": "/literal/test/path",
        "SYSTEMROOT": "systemroot-value",
        "WINDIR": "windir-value",
        "PATHEXT": "pathext-value",
        "COMSPEC": "comspec-value",
    }
    for key, value in launch_values.items():
        monkeypatch.setenv(key, value)
    for key in (
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_REPLACE_REF_BASE",
        "GIT_CONFIG_GLOBAL",
        "GIT_ASKPASS",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CEILING_DIRECTORIES",
    ):
        monkeypatch.setenv(key, f"{key}-secret-canary")
    monkeypatch.setenv("OTHER_CANARY", "inherited-secret-canary")
    repo, runner = repository(tmp_path, [ok(), ok(b"")])
    repo.pin_commit(SHA1)
    repo.seal()
    path = RepoPath("-dir/$(touch SHOULD_NOT_EXIST)[*]\nname.md")
    assert repo.read_bytes(SNAP1, path) is None
    argv, cwd, env = runner.calls[-1]
    assert argv[-1] == path.value
    assert argv[:2] == ("git", "--literal-pathspecs")
    assert cwd.is_absolute()
    assert dict(env) == {
        "PATH": "/literal/test/path",
        "SYSTEMROOT": "systemroot-value",
        "WINDIR": "windir-value",
        "PATHEXT": "pathext-value",
        "COMSPEC": "comspec-value",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    }
    assert "secret-canary" not in repr(dict(env))
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
    with pytest.raises(TypeError):
        cast(dict[str, str], env)["X"] = "x"


def test_filesystem_encoding_failure_precedes_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runner = repository(tmp_path, [ok()])
    repo.pin_commit(SHA1)
    repo.seal()
    before = len(runner.calls)

    def fail_encoding(value: str) -> bytes:
        raise UnicodeEncodeError("ascii", value, 0, 1, "secret-canary")

    monkeypatch.setattr(os, "fsencode", fail_encoding)
    with pytest.raises(GitExecutionError) as caught:
        repo.read_bytes(SNAP1, RepoPath("canary.md"))
    assert (caught.value.operation, caught.value.category, caught.value.returncode) == (
        "ls_tree",
        "launch_failed",
        None,
    )
    assert "canary" not in (str(caught.value) + repr(caught.value))
    assert len(runner.calls) == before


def test_default_runner_forwards_exact_safe_subprocess_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = tmp_path / "bare.git"
    git_dir.mkdir()
    observed: dict[str, object] = {}

    def fake_run(
        argv: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        observed["argv"] = argv
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"sha1\n", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    GitRepository(REPOSITORY, git_dir)
    assert observed["argv"] == (
        "git",
        f"--git-dir={git_dir.resolve()}",
        "rev-parse",
        "--show-object-format",
    )
    assert observed["cwd"] == git_dir.resolve()
    assert observed["shell"] is False
    assert observed["check"] is False
    assert observed["stdin"] is subprocess.DEVNULL
    assert observed["capture_output"] is True
    assert observed.get("text") is None
    assert isinstance(observed["env"], dict)


def test_default_runner_has_no_ambient_git_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = tmp_path / "bare.git"
    git_dir.mkdir()
    empty_path = tmp_path / "empty-bin"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))
    with pytest.raises(GitExecutionError) as caught:
        GitRepository(REPOSITORY, git_dir)
    assert (caught.value.operation, caught.value.category, caught.value.returncode) == (
        "object_format",
        "launch_failed",
        None,
    )


def test_ancestry_and_log_strict_parsers(tmp_path: Path) -> None:
    repo, _ = repository(
        tmp_path,
        [ok(), GitCommandResult(1, b"", b""), ok(b"3" * 40 + b"\n" + b"4" * 40 + b"\n")],
    )
    repo.pin_commit(SHA1)
    repo.seal()
    assert repo.is_ancestor(SNAP1, SnapshotRef(REPOSITORY, SHA2)) is False
    assert repo.commits_touching(SNAP1, SnapshotRef(REPOSITORY, SHA2), RepoPath("a.md")) == (GitSha("3" * 40), GitSha("4" * 40))


def test_log_parser_accepts_explicit_empty_output(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path, [ok(), ok(b"")])
    repo.pin_commit(SHA1)
    repo.seal()
    assert (
        repo.commits_touching(
            SNAP1, SnapshotRef(REPOSITORY, SHA2), RepoPath("a.md")
        )
        == ()
    )


@pytest.mark.parametrize("status,stdout", [(2, b""), (-1, b""), (1, b"x")])
def test_ancestry_errors(tmp_path: Path, status: int, stdout: bytes) -> None:
    repo, _ = repository(tmp_path, [ok(), GitCommandResult(status, stdout, b"canary")])
    repo.pin_commit(SHA1)
    repo.seal()
    with pytest.raises(GitExecutionError):
        repo.is_ancestor(SNAP1, SnapshotRef(REPOSITORY, SHA2))


def test_ancestry_nonzero_status_takes_exit_status_category(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path, [ok(), GitCommandResult(2, b"secret", b"canary")])
    repo.pin_commit(SHA1)
    repo.seal()
    with pytest.raises(GitExecutionError) as caught:
        repo.is_ancestor(SNAP1, SnapshotRef(REPOSITORY, SHA2))
    assert caught.value.category == "exit_status"


@pytest.mark.parametrize("stdout", [b"3" * 40, b"\n", b"A" * 40 + b"\n", b"3" * 40 + b"\n" + b"3" * 40 + b"\n", b"3" * 40 + b"\nextra"])
def test_log_rejects_malformed_output(tmp_path: Path, stdout: bytes) -> None:
    repo, _ = repository(tmp_path, [ok(), GitCommandResult(0, stdout, b"")])
    repo.pin_commit(SHA1)
    repo.seal()
    with pytest.raises(GitExecutionError):
        repo.commits_touching(SNAP1, SnapshotRef(REPOSITORY, SHA2), RepoPath("a.md"))


@pytest.mark.parametrize("operation", COMMAND_OPERATIONS)
def test_every_command_family_classifies_nonzero_without_echoing_canaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setenv("INHERITED_SECRET", "environment-secret-canary")
    result = GitCommandResult(
        7, b"stdout-secret-canary", b"stderr-secret-canary"
    )
    with pytest.raises(GitExecutionError) as caught:
        invoke_with_result(tmp_path / "git-dir-secret-canary", operation, result)
    assert type(caught.value) is GitExecutionError
    assert (
        caught.value.operation,
        caught.value.category,
        caught.value.returncode,
    ) == (operation, "exit_status", 7)
    assert_non_secret(caught.value)


@pytest.mark.parametrize("operation", COMMAND_OPERATIONS)
def test_every_command_family_classifies_launch_failure_without_echoing_canaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setenv("INHERITED_SECRET", "environment-secret-canary")
    with pytest.raises(GitExecutionError) as caught:
        invoke_with_result(
            tmp_path / "git-dir-secret-canary",
            operation,
            OSError("oserror-secret-canary"),
        )
    assert type(caught.value) is GitExecutionError
    assert (
        caught.value.operation,
        caught.value.category,
        caught.value.returncode,
    ) == (operation, "launch_failed", None)
    assert_non_secret(caught.value)


@pytest.mark.parametrize("operation", COMMAND_OPERATIONS)
def test_every_command_family_rejects_wrong_runner_result_without_echoing_canaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setenv("INHERITED_SECRET", "environment-secret-canary")
    with pytest.raises(GitExecutionError) as caught:
        invoke_with_result(
            tmp_path / "git-dir-secret-canary",
            operation,
            CanaryWrongRunnerResult(),
        )
    assert type(caught.value) is GitExecutionError
    assert (
        caught.value.operation,
        caught.value.category,
        caught.value.returncode,
    ) == (operation, "invalid_runner_result", None)
    assert_non_secret(caught.value)


@pytest.mark.parametrize(
    "operation,stdout",
    [
        ("object_format", b"stdout-secret-canary"),
        ("pin_commit", b"stdout-secret-canary"),
        ("capture_base_tip", b"stdout-secret-canary"),
        ("ls_tree", b"stdout-secret-canary"),
        ("is_ancestor", b"stdout-secret-canary"),
        ("commits_touching", b"stdout-secret-canary"),
    ],
)
def test_every_parsed_command_family_classifies_malformed_output_without_echo(
    tmp_path: Path, operation: str, stdout: bytes
) -> None:
    result = GitCommandResult(0, stdout, b"stderr-secret-canary")
    with pytest.raises(GitExecutionError) as caught:
        invoke_with_result(tmp_path / "git-dir-secret-canary", operation, result)
    assert type(caught.value) is GitExecutionError
    assert (
        caught.value.operation,
        caught.value.category,
        caught.value.returncode,
    ) == (operation, "malformed_output", 0)
    assert_non_secret(caught.value)


@pytest.mark.parametrize(
    "probe",
    [
        b"",
        b"sha1",
        b"SHA1\n",
        b"sha1\nsha1\n",
        b"sha1\ntrailing",
        b"sha256",
        b"sha256\ntrailing",
        b"sha512\n",
        b"stdout-secret-canary\n",
    ],
)
def test_every_malformed_object_format_is_bounded_and_non_secret(
    tmp_path: Path, probe: bytes
) -> None:
    git_dir = tmp_path / "db.git"
    git_dir.mkdir()
    runner = QueueRunner([ok(probe)])
    with pytest.raises(GitExecutionError) as caught:
        GitRepository(REPOSITORY, git_dir, command_runner=runner)
    assert (
        caught.value.operation,
        caught.value.category,
        caught.value.returncode,
    ) == ("object_format", "malformed_output", 0)
    assert_non_secret(caught.value)


def test_sha256_object_format_has_the_exact_typed_safe_error(tmp_path: Path) -> None:
    git_dir = tmp_path / "db.git"
    git_dir.mkdir()
    with pytest.raises(UnsupportedGitObjectFormat) as caught:
        GitRepository(
            REPOSITORY, git_dir, command_runner=QueueRunner([ok(b"sha256\n")])
        )
    assert type(caught.value) is UnsupportedGitObjectFormat
    assert caught.value.object_format == "sha256"
    assert str(caught.value) == "unsupported Git object format: sha256"


def fixture_environment() -> dict[str, str]:
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.setdefault("PATH", os.defpath)
    environment["LC_ALL"] = "C"
    return environment


def run_git(command: list[str], cwd: Path, *, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=fixture_environment(),
        input=input_bytes,
        check=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def init_bare_repository(tmp_path: Path) -> Path:
    bare = tmp_path / "bare.git"
    run_git(["git", "init", "--bare", "-q", str(bare)], tmp_path)
    return bare


def write_blob(bare: Path, contents: bytes) -> str:
    return run_git(
        ["git", f"--git-dir={bare}", "hash-object", "-w", "--stdin"],
        bare.parent,
        input_bytes=contents,
    ).decode("ascii")


def write_tree(bare: Path, files: dict[str, tuple[str, bytes]]) -> str:
    grouped: dict[str, dict[str, tuple[str, bytes]]] = {}
    direct: dict[str, tuple[str, bytes]] = {}
    for path, value in files.items():
        first, separator, remainder = path.partition("/")
        if separator:
            grouped.setdefault(first, {})[remainder] = value
        else:
            direct[first] = value

    records: list[bytes] = []
    for name, (mode, contents) in sorted(direct.items()):
        object_id = write_blob(bare, contents)
        records.append(f"{mode} blob {object_id}\t{name}\n".encode())
    for name, children in sorted(grouped.items()):
        object_id = write_tree(bare, children)
        records.append(f"040000 tree {object_id}\t{name}\n".encode())
    return run_git(
        ["git", f"--git-dir={bare}", "mktree"],
        bare.parent,
        input_bytes=b"".join(records),
    ).decode("ascii")


def commit_tree(
    bare: Path,
    files: dict[str, tuple[str, bytes]],
    message: str,
    *,
    parent: GitSha | None = None,
) -> GitSha:
    tree = write_tree(bare, files)
    command = [
        "git",
        f"--git-dir={bare}",
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit-tree",
        tree,
    ]
    if parent is not None:
        command.extend(["-p", parent.value])
    command.extend(["-m", message])
    return GitSha(run_git(command, bare.parent).decode("ascii"))


def update_ref(bare: Path, ref_name: str, commit: GitSha) -> None:
    run_git(
        ["git", f"--git-dir={bare}", "update-ref", ref_name, commit.value],
        bare.parent,
    )


def test_real_bare_object_database_is_pinned_and_worktree_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG_GLOBAL",
        "GIT_ASKPASS",
    ):
        monkeypatch.setenv(key, f"{key}-hostile-canary")
    assert not any(key.startswith("GIT_") for key in fixture_environment())
    bare = init_bare_repository(tmp_path)
    first_files = {
        "docs.md": ("100644", b"first"),
        "empty.md": ("100644", b""),
        "link.md": ("120000", b"docs.md"),
    }
    first = commit_tree(bare, first_files, "first")
    second = commit_tree(
        bare,
        {**first_files, "docs.md": ("100644", b"second")},
        "second",
        parent=first,
    )
    update_ref(bare, "refs/remotes/origin/main", first)

    repo = GitRepository(REPOSITORY, bare)
    resolved = resolve_pull_request_snapshots(
        repo,
        PullRequestSnapshotRequest(REPOSITORY, PullRequestState.OPEN, second, BaseBranch("main"), None),
    )
    update_ref(bare, "refs/remotes/origin/main", second)
    assert repo.read_bytes(resolved.translation_base_snapshot, RepoPath("docs.md")) == b"first"
    assert repo.read_bytes(resolved.source_snapshot, RepoPath("docs.md")) == b"second"
    assert repo.read_bytes(resolved.source_snapshot, RepoPath("empty.md")) == b""
    assert repo.read_bytes(resolved.source_snapshot, RepoPath("link.md")) == b"docs.md"


def test_merged_old_pr_resolution_and_provenance_use_current_base(tmp_path: Path) -> None:
    bare = init_bare_repository(tmp_path)
    files = {
        "ru/source.md": ("100644", b"source-old"),
        "en/target.md": ("100644", b"target-old"),
        "en/deleted.md": ("100644", b"delete-me"),
    }
    baseline_sha = commit_tree(bare, files, "merged baseline")
    changes = (
        ("ru/source.md", ("100644", b"source-current"), "source edit"),
        ("en/target.md", ("100644", b"target-current"), "target edit"),
        ("en/created.md", ("100644", b"created"), "target create"),
    )
    commits: list[GitSha] = []
    parent = baseline_sha
    for relative_path, value, message in changes:
        files[relative_path] = value
        parent = commit_tree(bare, files, message, parent=parent)
        commits.append(parent)
    del files["en/deleted.md"]
    parent = commit_tree(bare, files, "target delete", parent=parent)
    commits.append(parent)
    current_sha = commits[-1]
    update_ref(bare, "refs/remotes/origin/main", current_sha)
    repo = GitRepository(REPOSITORY, bare)
    resolved = resolve_pull_request_snapshots(
        repo,
        PullRequestSnapshotRequest(
            REPOSITORY,
            PullRequestState.MERGED,
            None,
            BaseBranch("main"),
            baseline_sha,
        ),
    )
    assert resolved.source_snapshot.commit_sha == current_sha
    assert resolved.target_snapshot == resolved.source_snapshot
    assert resolved.scope_snapshot == resolved.source_snapshot
    assert resolved.provenance_baseline == SnapshotRef(REPOSITORY, baseline_sha)

    result = compare_provenance(
        repo,
        resolved.provenance_baseline,
        resolved.source_snapshot,
        (
            ProvenancePath(ProvenanceRole.SOURCE, RepoPath("ru/source.md")),
            ProvenancePath(ProvenanceRole.TARGET, RepoPath("en/target.md")),
            ProvenancePath(ProvenanceRole.TARGET, RepoPath("en/created.md")),
            ProvenancePath(ProvenanceRole.TARGET, RepoPath("en/deleted.md")),
        ),
    )
    assert isinstance(result, ProvenanceAssessment)
    assert tuple(entry.state for entry in result.paths) == (
        PathHistoryState.NEWER,
        PathHistoryState.NEWER,
        PathHistoryState.CREATED,
        PathHistoryState.DELETED,
    )
    assert tuple(entry.intervening_commits for entry in result.paths) == tuple(
        (commit,) for commit in commits
    )


def test_real_touched_then_restored_path_keeps_both_commits(tmp_path: Path) -> None:
    bare = init_bare_repository(tmp_path)
    stable = {"ru/restored.md": ("100644", b"stable")}
    baseline = commit_tree(bare, stable, "baseline")
    changed = commit_tree(
        bare,
        {"ru/restored.md": ("100644", b"temporary")},
        "temporary change",
        parent=baseline,
    )
    restored = commit_tree(bare, stable, "restore", parent=changed)

    repo = GitRepository(REPOSITORY, bare)
    baseline_snapshot = repo.pin_commit(baseline)
    current_snapshot = repo.pin_commit(restored)
    repo.seal()
    result = compare_provenance(
        repo,
        baseline_snapshot,
        current_snapshot,
        (ProvenancePath(ProvenanceRole.SOURCE, RepoPath("ru/restored.md")),),
    )
    assert isinstance(result, ProvenanceAssessment)
    assert result.paths[0].state is PathHistoryState.UNCHANGED
    assert result.paths[0].old_content_id == result.paths[0].current_content_id
    assert result.paths[0].intervening_commits == (changed, restored)
