from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from ydbdoc_review_ng.continuation import (
    ContinuationStage,
    ContinuationState,
    SourceChangeInventory,
)
from ydbdoc_review_ng.domain import GitSha, Mode, ModelRole, RepoPath
from ydbdoc_review_ng.models import (
    AttemptResult,
    AttemptStatus,
    ModelRequest,
    ModelUsage,
)
from ydbdoc_review_ng.persistence import (
    AttemptCostRecord,
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
    source_sha = uuid4().hex + uuid4().hex[:8]
    job_id = ""
    try:
        job_id = store.start_job(
            Mode.DOC_TRANSLATE,
            pr_number=source_pr,
            source_sha=source_sha,
            target_sha=None,
            started_at=now,
        )
        request = ModelRequest(
            ModelRole.TRANSLATE,
            "integration-model",
            "integration request",
            {"type": "object"},
            target_path=RepoPath("docs/integration.md"),
        )
        store(
            AttemptResult(
                attempt_number=1,
                request_role=ModelRole.TRANSLATE,
                request_model=request.model,
                request=request,
                request_payload=b'{"request":"integration"}',
                started_at=now,
                finished_at=now,
                status=AttemptStatus.SUCCEEDED,
                error=None,
                http_status=200,
                raw_response=b'{"response":"integration"}',
                response_status="final",
                response_model=request.model,
                response_role="assistant",
                text="integration",
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                cost_rub=Decimal("0.123456789"),
            ),
            job_id=job_id,
        )
        checkpoint = ContinuationCheckpoint(
            continuation_id=job_id,
            job_id=job_id,
            source_pr=source_pr,
            trigger_pr=source_pr,
            source_sha=GitSha(source_sha),
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
        assert store.attempt_costs_for_source(GitSha(source_sha)) == (
            AttemptCostRecord(
                RepoPath("docs/integration.md"),
                ModelRole.TRANSLATE,
                Decimal("0.123456789"),
            ),
        )
    finally:
        try:
            if job_id:
                executor.execute(
                    "DELETE FROM `ydbdoc_review/attempts` WHERE job_id = $job_id;",
                    {"job_id": job_id},
                )
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
