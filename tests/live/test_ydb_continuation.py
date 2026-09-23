from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ydbdoc_review_ng.continuation import (
    ContinuationStage,
    ContinuationState,
    SourceChangeInventory,
)
from ydbdoc_review_ng.domain import GitSha, Mode
from ydbdoc_review_ng.persistence import (
    ContinuationCheckpoint,
    JobStatus,
    YdbPersistence,
    semantic_stop_error,
)
from ydbdoc_review_ng.runtime_ydb import (
    DEFAULT_YDB_DATABASE,
    DEFAULT_YDB_ENDPOINT,
    SDKExecutor,
)

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.timeout(120)]


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} is required for the live YDB test")
    return value


def test_semantic_checkpoint_round_trip_in_deployed_ydb() -> None:
    """Exercise the exact production job/checkpoint handoff, then remove only test rows."""
    executor = SDKExecutor(
        os.environ.get("YDB_ENDPOINT", DEFAULT_YDB_ENDPOINT),
        os.environ.get("YDB_DATABASE", DEFAULT_YDB_DATABASE),
        os.environ.get("YDB_TOKEN", ""),
        _required("YDB_SA_KEY") if not os.environ.get("YDB_TOKEN") else "",
    )
    store = YdbPersistence(executor)
    now = datetime.now(UTC).replace(microsecond=0)
    source_pr = uuid4().int % (2**63 - 1) or 1
    job_id = ""
    try:
        job_id = store.start_job(
            Mode.DOC_TRANSLATE,
            pr_number=source_pr,
            source_sha="a" * 40,
            target_sha=None,
            started_at=now,
        )
        checkpoint = ContinuationCheckpoint(
            continuation_id=job_id,
            job_id=job_id,
            source_pr=source_pr,
            trigger_pr=source_pr,
            source_sha=GitSha("a" * 40),
            base_sha=GitSha("b" * 40),
            translation_branch=f"integration/ydb-checkpoint-{job_id}",
            target_sha=None,
            source_inventory=SourceChangeInventory(()),
            scope_target_paths=(),
            state=ContinuationState(
                1,
                ContinuationStage.DIRECTION,
                None,
                None,
                (),
                (),
                (),
                None,
            ),
            created_at=now,
        )

        pending = store.save_checkpoint(checkpoint, now=now)
        store.finish_job(
            job_id,
            JobStatus.FAILED,
            error=semantic_stop_error(ContinuationStage.DIRECTION),
            finished_at=now,
            target_sha=None,
        )
        opened = store.activate_checkpoint(pending, now=now)

        assert store.load_checkpoint(source_pr, now=now) == opened
        assert opened.expires_at > now
    finally:
        try:
            if job_id:
                executor.execute(
                    "DELETE FROM `ydbdoc_review/continuations` "
                    "WHERE continuation_id = $continuation_id;",
                    {"continuation_id": job_id},
                )
                executor.execute(
                    "DELETE FROM `ydbdoc_review/jobs` WHERE job_id = $job_id;",
                    {"job_id": job_id},
                )
        finally:
            executor.close()
