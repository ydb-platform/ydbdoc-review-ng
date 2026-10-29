"""Publication routing with production composition and distinct GitHub repositories."""
import base64
from decimal import Decimal

from _runtime_services import RuntimeServices

from ydbdoc_review_ng.application import TranslateWorkflowInput, VerifyWorkflowInput
from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.runtime import RuntimeSource, create_runtime
from ydbdoc_review_ng.runtime_github import GitHubBackend

UPSTREAM = "/repos/ydb-platform/ydb"
FORK = "/repos/sintjuri/ydb"
ENV = {"GITHUB_ACTOR": "maintainer", "YDBDOC_ALLOWED_ACTORS": "maintainer",
       "YANDEX_API_KEY": "offline", "YANDEX_FOLDER_ID": "offline",
       "YDBDOC_PUBLICATION_REPOSITORY": "sintjuri/ydb"}


class ForkServices(RuntimeServices):
    def __init__(self):
        super().__init__()
        self.wire = []

    def github(self, method, path, payload):
        self.wire.append((method, path, payload))
        if path == FORK:
            return {"fork": True, "source": {"full_name": "ydb-platform/ydb"}}
        if path.startswith(FORK + "/"):
            path = UPSTREAM + path[len(FORK):]
        value = super().github(method, path, payload)
        if method == "GET" and path == UPSTREAM + "/pulls/43":
            value["head"]["repo"]["full_name"] = "sintjuri/ydb"
        return value


def test_translate_uses_fork_for_all_git_mutations_and_keeps_pr_upstream():
    services = ForkServices()
    result = create_runtime(environment=ENV, ydb_executor=services,
        github_transport=services.github, model_transport=services.model).doc_translate(
            TranslateWorkflowInput(42, GitSha(services.source), Decimal(10)))
    assert result.verdict.value == "GREEN"
    assert services.pr_exists
    git_writes = [(m,p,b) for m,p,b in services.wire if m != "GET" and "/git/" in p]
    assert git_writes and all(p.startswith(FORK) for _,p,_ in git_writes)
    pulls = [(p,b) for m,p,b in services.wire if m == "POST" and p.endswith("/pulls")]
    assert pulls[0][0] == UPSTREAM + "/pulls"
    assert pulls[0][1]["head"] == "sintjuri:robots/translation/pr-42"
    assert any(p.startswith(FORK + "/contents/") for _,p,_ in services.wire)


def test_verify_accepts_only_configured_fork_and_freezes_fork_target():
    services = ForkServices()
    services.pr_exists, services.branch_head = True, services.translated
    github = GitHubBackend(services.github, publication_repository="sintjuri/ydb")
    source = RuntimeSource(ENV, github)
    auth = source.authorize_verify(VerifyWorkflowInput(43, GitSha(services.source), GitSha(services.translated)))
    source.snapshot_verify(auth)
    assert source.metadata_snapshot.repository.value == "sintjuri/ydb"
    assert ("GET", UPSTREAM + "/git/ref/heads/main", None) in services.wire
    assert any(p == FORK + "/git/ref/heads/robots/translation/pr-42" for _,p,_ in services.wire)


def test_read_same_path_and_sha_from_explicit_source_and_target_repositories():
    calls = []
    def transport(method, path, payload):
        calls.append(path)
        data = b"fork" if path.startswith(FORK) else b"upstream"
        return {"type":"file", "encoding":"base64", "content":base64.b64encode(data).decode()}
    github = GitHubBackend(transport, publication_repository="sintjuri/ydb")
    path = RepoPath("page.md")
    assert github.read_bytes(SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a"*40)), path) == b"upstream"
    assert github.read_bytes(SnapshotRef(RepositoryId("sintjuri/ydb"), GitSha("a"*40)), path) == b"fork"
    assert len(calls) == 2


def test_continue_checks_fork_identity_and_reads_fork_branch():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from ydbdoc_review_ng.runtime_continue import resolve_continue_checkpoint
    services = ForkServices()
    services.pr_exists, services.branch_head = True, services.translated
    def transport(method, path, payload):
        value = services.github(method, path, payload)
        if path == UPSTREAM + "/pulls/43":
            value["number"] = 43
        return value
    checkpoint = SimpleNamespace(source_pr=42, source_sha=GitSha(services.source),
        target_sha=GitSha(services.translated), translation_branch="robots/translation/pr-42")
    reader = SimpleNamespace(load_checkpoint=lambda *args, **kwargs: checkpoint)
    backend = GitHubBackend(transport, publication_repository="sintjuri/ydb")
    assert resolve_continue_checkpoint(backend, reader, 43, now=datetime.now(UTC)) is checkpoint
    assert any(p == FORK + "/git/ref/heads/robots/translation/pr-42" for _,p,_ in services.wire)


def test_deployed_action_publishes_to_upstream():
    from pathlib import Path

    import yaml
    action = yaml.safe_load((Path(__file__).parents[2] / ".github/actions/doc-review/action.yml").read_text())
    step = next(s for s in action["runs"]["steps"] if "-m ydbdoc_review_ng.cli" in s.get("run", ""))
    services = RuntimeServices()
    wire = []
    def transport(method, path, payload):
        wire.append((method, path, payload))
        return services.github(method, path, payload)
    env = {**ENV, "YDBDOC_PUBLICATION_REPOSITORY": step["env"]["YDBDOC_PUBLICATION_REPOSITORY"]}
    result = create_runtime(environment=env, ydb_executor=services,
        github_transport=transport, model_transport=services.model).doc_translate(
            TranslateWorkflowInput(42, GitSha(services.source), Decimal(10)))
    assert result.verdict.value == "GREEN"
    writes = [(p, b) for m, p, b in wire if m != "GET" and "/git/" in p]
    assert writes and all(p.startswith(UPSTREAM + "/") for p, _ in writes)
    assert not any(p.startswith(FORK + "/") for _, p, _ in wire)
    assert any(m == "POST" and p == UPSTREAM + "/pulls"
               and b["head"] == "robots/translation/pr-42" for m, p, b in wire)
