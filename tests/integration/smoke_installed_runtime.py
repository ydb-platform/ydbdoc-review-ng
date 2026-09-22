"""Run outside the checkout using the installed wheel and no real service I/O."""

import importlib.metadata
import json
import os
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import ydb
from _runtime_services import InstalledContinueServices

import ydbdoc_review_ng.runtime
from ydbdoc_review_ng.cli import main
from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.runtime_github import GitHubBackend
from ydbdoc_review_ng.runtime_metadata import MetadataProducer


def denied(*args: object, **kwargs: object) -> None:
    raise AssertionError("installed smoke forbids network")


def run() -> None:
    assert Path(ydbdoc_review_ng.runtime.__file__).is_relative_to(sys.prefix)
    assert importlib.metadata.version("ydbdoc-review-ng") == "1.1.0"
    assert importlib.metadata.version("PyYAML").startswith("6.")
    credentials = ydb.iam.ServiceAccountCredentials.from_content(
        json.dumps(
            {
                "id": "offline-test-key",
                "service_account_id": "offline-test-account",
                "private_key": "not-used-without-network",
            }
        )
    )
    assert credentials is not None
    services = InstalledContinueServices()
    services.files["ydb/docs/ru/core/toc.yaml"] = b"items: [{name: Page, href: page.md}]\n"
    services.files["ydb/docs/en/core/toc.yaml"] = b"items: []\n"
    source = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha(services.source))
    target = SnapshotRef(source.repository, GitSha(services.base))
    metadata = MetadataProducer(GitHubBackend(services.github), source, target, ()).changes(
        RepoPath("ydb/docs/ru/core/page.md"), RepoPath("ydb/docs/en/core/page.md"), new=True
    )
    assert len(metadata) == 1 and b'"page.md"' in metadata[0].after
    environment = {
        "YDBDOC_RUNTIME_FACTORY": "ydbdoc_review_ng.runtime:create_runtime",
        "GITHUB_ACTOR": "maintainer",
        "YDBDOC_ALLOWED_ACTORS": "maintainer",
        "YANDEX_API_KEY": "offline-secret",
        "YANDEX_FOLDER_ID": "offline-folder",
    }
    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(socket, "create_connection", denied),
        patch(
            "ydbdoc_review_ng.runtime_github.GitHubHTTP.__call__",
            lambda self, *args: services.github(*args),
        ),
        patch(
            "ydbdoc_review_ng.runtime_ydb.SDKExecutor.execute",
            lambda self, *args: services.execute(*args),
        ),
        patch(
            "ydbdoc_review_ng.models.clients.UrllibTransport.__call__",
            lambda self, *args: services.model(*args),
        ),
    ):
        assert (
            main(["translate", "--pr", "42", "--source-sha", services.source, "--budget-rub", "10"])
            == 0
        )
        services.stop_review = True
        assert (
            main(
                [
                    "verify",
                    "--pr",
                    "43",
                    "--source-sha",
                    services.source,
                    "--target-sha",
                    services.translated,
                ]
            )
            == 1
        )
        assert len(services.checkpoints) == 1
        saved = next(iter(services.checkpoints.values()))
        assert saved["stage"] == "review" and saved["status"] == "open"
        services.stop_review = False
        services.continuing = True
        assert main(["continue", "--pr", "43"]) == 0
        assert saved["status"] == "closed"
    assert services.files["ydb/docs/en/core/page.md"] == b"# Translated\n"
    assert len(services.comments) == 1
    assert services.comments[0]["body"].startswith("GREEN\n")
    assert [job["mode"] for job in services.jobs.values()] == [
        "doc_translate",
        "doc_verify",
        "doc_continue",
    ]
    assert list(services.jobs.values())[-1]["status"] == "succeeded"
    attempts = [row for row in services.audit if "attempt_id" in row]
    assert len(attempts) == 4
    assert attempts[-1]["job_id"] == saved["consumed_by_job_id"]
    print(
        "INSTALLED_RUNTIME_SMOKE_PASS: translate + verify + continue, compact YAML, "
        "one comment, closed checkpoint, audited, no network"
    )


if __name__ == "__main__":
    run()
