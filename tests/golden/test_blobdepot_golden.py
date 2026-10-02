"""P2: offline BlobDepot golden — atoms, presentation map, critic gate.

No network, no live DeepSeek. Fixtures freeze the #50839 BlobDepot regression
surface under tests/golden/blobdepot/.
"""

from __future__ import annotations

import json
from pathlib import Path

from ydbdoc_review_ng.domain import GitSha, ModelRole, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.models import AttemptError, ModelCallResult, ModelRequest
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import ProtectedKind, fields_of
from ydbdoc_review_ng.quality.repair import review_pr
from ydbdoc_review_ng.quality.types import Verdict
from ydbdoc_review_ng.translation.document import prepare_document, restore_document
from ydbdoc_review_ng.translation.presentation import (
    apply_presentation_map,
    build_presentation_map,
)

GOLDEN_DIR = Path(__file__).resolve().parent / "blobdepot"
SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
RU_PATH = RepoPath("ydb/docs/ru/core/maintenance/manual/blobdepot.md")
EN_PATH = RepoPath("ydb/docs/en/core/maintenance/manual/blobdepot.md")


def _load_bytes(name: str) -> bytes:
    return (GOLDEN_DIR / name).read_bytes()


def _expectations() -> dict:
    return json.loads((GOLDEN_DIR / "expectations.json").read_text(encoding="utf-8"))


class _Scripted:
    def __init__(self, payloads: list[str | ModelCallResult]) -> None:
        self.payloads = list(payloads)
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest) -> ModelCallResult:
        self.calls.append(request)
        item = self.payloads.pop(0)
        if isinstance(item, ModelCallResult):
            return item
        return ModelCallResult(item, None, ())


def test_blobdepot_identifier_atoms_survive_prepare_and_restore() -> None:
    """RU escaped identifiers stay one atom; restore emits canonical bare forms."""
    source = _load_bytes("source.ru.md")
    expect = _expectations()
    plan = build_markdown_plan(SNAPSHOT, RU_PATH, source)

    atoms = [
        source[region.span.start : region.span.end].decode("utf-8")
        for field in fields_of(plan)
        for region in field.protected_regions
        if region.kind is ProtectedKind.IDENTIFIER
    ]
    for required in expect["required_escaped_atoms_in_source"]:
        assert required in atoms, f"missing identifier atom {required!r}"

    prepared = prepare_document(source, plan)
    prepared_text = "".join(chunk.text for chunk in prepared.chunks)
    for forbidden in ("CONTROLLER", "FAILED", "POOL_NAME", "page"):
        assert forbidden not in prepared_text

    restored = restore_document(
        source, plan, prepared, tuple(chunk.text for chunk in prepared.chunks)
    )
    for token in expect["canonical_atoms_after_restore"]:
        assert token.encode("utf-8") in restored
    for fragment in expect["forbidden_after_prepare_or_restore"]:
        assert fragment.encode("utf-8") not in restored
    assert b"page_CONTROLLER" not in restored
    assert b"\\_CONTROLLER" not in restored


def test_blobdepot_presentation_map_wraps_atoms_from_old_en() -> None:
    """When old EN is present, map wraps matching identifier atoms; CLI flag stays bare."""
    old_en = _load_bytes("old.en.md")
    expect = _expectations()
    styles = build_presentation_map(old_en, source_snapshot=SNAPSHOT, source_path=EN_PATH)
    for token in expect["presentation_inline_code"]:
        assert styles[token].inline_code is True

    draft = (
        b"# Blob Depot\n\n"
        b"Created through BS_CONTROLLER.\n\n"
        b"* --name unique name\n"
        b"* --storage-pool-name=POOL_NAME pool\n\n"
        b"* NEW - waiting\n"
        b"* WORKING - ready\n"
        b"* CREATE_FAILED - error\n"
        b"* CREATE\\_FAILED escaped\n\n"
        b"Monitoring page BS_CONTROLLER.\n"
    )
    applied = apply_presentation_map(
        draft, styles, source_snapshot=SNAPSHOT, source_path=EN_PATH
    )
    for token in expect["presentation_inline_code"]:
        assert (b"`" + token.encode("utf-8") + b"`") in applied
    assert b"CREATE\\_FAILED" not in applied
    assert b"* --name unique name\n" in applied
    assert b"* NEW - waiting\n" in applied
    assert b"* WORKING - ready\n" in applied


def test_blobdepot_absent_old_en_presentation_apply_is_noop() -> None:
    draft = (
        b"# Blob Depot\n\n"
        b"See BS_CONTROLLER, POOL_NAME, CREATE_FAILED and --name.\n"
    )
    styles = build_presentation_map(None, source_snapshot=SNAPSHOT, source_path=EN_PATH)
    assert styles == {}
    assert (
        apply_presentation_map(draft, styles, source_snapshot=SNAPSHOT, source_path=EN_PATH)
        == draft
    )


def test_blobdepot_critic_unavailable_is_red_not_raw_product() -> None:
    """Critic transport failure after retry → RED; arbiter must not GREEN the raw draft."""
    source = {"ydb/docs/ru/core/maintenance/manual/blobdepot.md": _load_bytes("source.ru.md")}
    # Raw translator dump still carries the regression surface (escaped / bare mix).
    translated = {
        "ydb/docs/en/core/maintenance/manual/blobdepot.md": (
            b"# Blob Depot\n\n"
            b"Created through BS_CONTROLLER.\n"
            b"* --name bare flag\n"
            b"* CREATE\\_FAILED still escaped\n"
        )
    }
    models = _Scripted(
        [
            ModelCallResult(None, AttemptError.TRANSPORT, ()),
            ModelCallResult(None, AttemptError.TRANSPORT, ()),
            # Poison: if arbiter ran on raw draft, this would wrongly succeed.
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files=source,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda files: None,
    )

    assert [call.role for call in models.calls] == [ModelRole.CRITIC, ModelRole.CRITIC]
    assert corrected == translated
    assert final.verdict is Verdict.RED
    assert final.findings
    assert any(
        item.target_path == "ydb/docs/en/core/maintenance/manual/blobdepot.md"
        for item in final.findings
    )
