"""Per-pair planning preserves independent bilingual decisions and immutable inputs."""

from dataclasses import replace
from types import SimpleNamespace

from tests.unit.test_scope import _snapshots
from ydbdoc_review_ng.continuation import (
    SourceChange,
    SourceChangeInventory,
    decode_source_inventory,
    encode_source_inventory,
)
from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import RepoPath
from ydbdoc_review_ng.runtime_content import RuntimeContent


def runtime(decisions):
    snapshots = _snapshots()
    paths = [f"ydb/docs/{locale}/core/{key}" for key, _, _ in decisions for locale in ("ru", "en")]
    values = {path: b"# Title\n\nText.\n" for path in paths}

    class Github:
        def read_bytes(self, snapshot, path):
            return values.get(path.value)

        def request(self, *args):
            return {}

        def list_comments(self, *args):
            return ()

        def create_comment(self, *args):
            pass

    inventory = SourceChangeInventory(
        tuple(SourceChange(RepoPath(path), "modified", None, None) for path in sorted(paths)),
        snapshots.source_snapshot.commit_sha,
        snapshots.source_snapshot.commit_sha,
    )
    source = SimpleNamespace(
        github=Github(),
        snapshots=snapshots,
        inventory=inventory,
        metadata_snapshot=snapshots.scope_snapshot,
        source_base_snapshot=snapshots.source_snapshot,
        source_change_snapshot=snapshots.source_snapshot,
        source_pr=1,
    )
    content = RuntimeContent(
        source,
        SimpleNamespace(
            invoke=lambda request: (_ for _ in ()).throw(AssertionError("unexpected model call"))
        ),
        {},
    )
    preparation = content.prepare_source(SimpleNamespace())
    preparation = replace(
        preparation, inventory=replace(inventory, pair_decisions=tuple(decisions))
    )
    return content, preparation


def test_pair_decisions_roundtrip_and_no_translation_has_no_outputs():
    content, preparation = runtime([("a.md", "no_translation", "Already synchronized")])
    plans = content.select_source(preparation)
    assert plans.manifest is None
    assert plans.documents == ()
    assert plans.fixed_files == ()
    assert plans.translation_plan.outputs == ()
    assert (
        decode_source_inventory(encode_source_inventory(plans.preparation.inventory)).pair_decisions
        == preparation.inventory.pair_decisions
    )


def test_mixed_pairs_and_partial_target_edits_are_planned_independently():
    content, preparation = runtime(
        [
            ("a.md", "ru_to_en", "RU leads"),
            ("b.md", "en_to_ru", "EN leads"),
            ("c.md", "no_translation", "Already synchronized"),
        ]
    )
    plans = content.select_source(preparation)
    assert plans.manifest.direction.value == "mixed"
    assert {
        (d.entry.pair.source_path.value, d.entry.pair.target_path.value) for d in plans.documents
    } == {
        ("ydb/docs/ru/core/a.md", "ydb/docs/en/core/a.md"),
        ("ydb/docs/en/core/b.md", "ydb/docs/ru/core/b.md"),
    }
    assert all("c.md" not in p.value for p in plans.translation_plan.outputs)
    restored = replace(
        preparation,
        inventory=decode_source_inventory(encode_source_inventory(plans.preparation.inventory)),
    )
    replay = content.select_source(restored)
    assert replay.translation_plan == plans.translation_plan
    assert replay.manifest == plans.manifest


def test_mixed_review_inputs_do_not_include_skipped_or_reverse_sources():
    from ydbdoc_review_ng.application import WorkflowCandidate
    from ydbdoc_review_ng.runtime_content import pack

    content, preparation = runtime(
        [
            ("a.md", "ru_to_en", "RU leads"),
            ("b.md", "en_to_ru", "EN leads"),
            ("c.md", "no_translation", "Synced"),
        ]
    )
    plans = content.select_source(preparation)
    files = {doc.entry.pair.target_path.value: b"# Translated\n" for doc in plans.documents}
    source, target, *_ = content._pr_review_inputs(WorkflowCandidate(pack(files), None))
    assert set(source) == {"ydb/docs/ru/core/a.md", "ydb/docs/en/core/b.md"}
    assert set(target) == set(files)


def test_conflict_checkpoints_with_pair_reason_and_preserves_other_decisions():
    import pytest

    from ydbdoc_review_ng.application import ImmutableRunSnapshot
    from ydbdoc_review_ng.application.workflows import SemanticCheckpointStop
    from ydbdoc_review_ng.domain import Mode

    content, preparation = runtime(
        [
            ("a.md", "ru_to_en", "RU leads"),
            ("b.md", "conflict", "Both changed the limit differently"),
        ]
    )
    comments = []
    content.source.github.create_comment = lambda pr, text: comments.append(text)
    content.source.github.head = lambda branch: None
    content.publisher = SimpleNamespace(pr_number=None)
    preparation = replace(
        preparation,
        snapshot=ImmutableRunSnapshot(
            Mode.DOC_TRANSLATE,
            preparation.snapshots.source_snapshot.commit_sha,
            None,
            "translation/pr-1",
            None,
        ),
    )
    with pytest.raises(SemanticCheckpointStop) as caught:
        content.select_source(preparation)
    assert "b.md: conflict: Both changed the limit differently" in comments[0]
    capture = caught.value.capture
    assert capture.source_inventory.pair_decisions == preparation.inventory.pair_decisions
    assert content.documents == ()


def test_mixed_verification_uses_target_paths_without_classifier():
    content, preparation = runtime(
        [("a.md", "ru_to_en", "RU leads"), ("b.md", "en_to_ru", "EN leads")]
    )
    content.source.verification_direction = Direction.MIXED
    content.source.verification_target_paths = (
        RepoPath("ydb/docs/en/core/a.md"),
        RepoPath("ydb/docs/ru/core/b.md"),
    )
    preparation = replace(
        preparation,
        for_translation=False,
        inventory=replace(preparation.inventory, pair_decisions=()),
    )
    plans = content.select_source(preparation)
    assert plans.manifest.direction is Direction.MIXED
    assert {path for path, _ in plans.fixed_files} == {
        path.value for path in content.source.verification_target_paths
    }


def test_skipped_markdown_keeps_deterministic_resource_work():
    content, preparation = runtime([("a.md", "no_translation", "Synchronized")])
    path = RepoPath("ydb/docs/ru/core/image.png")
    original_read = content.source.github.read_bytes
    content.source.github.read_bytes = lambda snapshot, name: (
        b"PNG" if name == path else original_read(snapshot, name)
    )
    files = tuple(
        sorted(
            (*preparation.inventory.files, SourceChange(path, "added", None, None)),
            key=lambda item: item.path.value,
        )
    )
    preparation = replace(preparation, inventory=replace(preparation.inventory, files=files))
    plans = content.select_source(preparation)
    assert plans.documents == ()
    assert plans.fixed_files == (("ydb/docs/en/core/image.png", b"PNG"),)


def test_continue_replay_keeps_mixed_scope_and_never_reclassifies():
    from ydbdoc_review_ng.continuation import (
        STATE_VERSION,
        ContinuationStage,
        ContinuationState,
        checkpoint_scope_sha256,
    )
    from ydbdoc_review_ng.runtime_continue import replay_continue
    from ydbdoc_review_ng.translation_plan import translation_plan_sha256

    content, preparation = runtime(
        [("a.md", "ru_to_en", "RU leads"), ("b.md", "en_to_ru", "EN leads")]
    )
    plans = content.select_source(preparation)
    inventory = decode_source_inventory(encode_source_inventory(plans.preparation.inventory))
    targets = tuple(entry.pair.target_path for entry in plans.manifest.entries)
    state = ContinuationState(
        STATE_VERSION,
        ContinuationStage.TRANSLATION,
        Direction.MIXED,
        checkpoint_scope_sha256(
            plans.manifest, inventory, translation_plan_sha256(plans.translation_plan)
        ),
        None,
        targets,
        (),
    )
    checkpoint = SimpleNamespace(
        state=state, source_inventory=inventory, scope_target_paths=targets
    )
    content.source.inventory = inventory
    content.source.snapshot_continue = lambda saved: preparation.snapshot
    replay = replay_continue(content, checkpoint)
    assert replay.plans.manifest == plans.manifest
    assert replay.plans.translation_plan == plans.translation_plan
    import pytest

    from ydbdoc_review_ng.continuation import ContinuationStateError

    checkpoint.state = replace(state, direction=Direction.RU_TO_EN)
    with pytest.raises(ContinuationStateError):
        replay_continue(content, checkpoint)


def test_workflow_synchronized_pair_never_invokes_publication_or_translator():
    from decimal import Decimal

    from tests.unit.test_translate_reset import (
        SOURCE_SHA,
        FakeClock,
        FakeReporter,
        FakeSource,
        RecordingPersistence,
    )
    from ydbdoc_review_ng.application.workflows import LinearWorkflows, TranslateWorkflowInput

    content, preparation = runtime(
        [("a.md", "no_translation", "Both versions already state the new limit")]
    )
    content.source.inventory = preparation.inventory
    comments = []
    content.source.github.create_comment = lambda pr, text: comments.append(text)

    class Forbidden:
        def publish(self, *args):
            raise AssertionError("no-op must not publish")

        def review(self, *args):
            raise AssertionError("no-op must not review")

    forbidden = Forbidden()
    workflows = LinearWorkflows(
        clock=FakeClock(),
        persistence=RecordingPersistence(),
        source=FakeSource(),
        content=content,
        reviewer=forbidden,
        publisher=forbidden,
        reporter=FakeReporter(),
    )
    result = workflows.doc_translate(TranslateWorkflowInput(42, SOURCE_SHA, Decimal(100)))
    assert result.verdict.value == "GREEN"
    assert len(comments) == 1
    assert comments[0].split("\n", 1)[0] == "Перевод не требуется."
    assert "Both versions already state the new limit" not in comments[0]


def test_delete_is_mirrored_without_semantic_classifier():
    content, preparation = runtime([("a.md", "ru_to_en", "placeholder")])
    path = RepoPath("ydb/docs/ru/core/a.md")
    original_read = content.source.github.read_bytes
    content.source.github.read_bytes = lambda snapshot, name: (
        None if name == path else original_read(snapshot, name)
    )
    content.source.inventory = replace(
        preparation.inventory, files=(SourceChange(path, "removed", None, None),), pair_decisions=()
    )
    plans = content.select_source(content.prepare_source(preparation.snapshot))
    assert plans.fixed_files == (("ydb/docs/en/core/a.md", None),)
    assert plans.documents == ()


def test_resource_only_mixed_verify_respects_exact_published_targets():
    content, preparation = runtime([])
    targets = (RepoPath("ydb/docs/en/core/a.png"), RepoPath("ydb/docs/ru/core/b.png"))
    values = {
        f"ydb/docs/{locale}/core/{name}.png": b"PNG"
        for locale in ("en", "ru")
        for name in ("a", "b", "extra")
    }
    content.source.github.read_bytes = lambda snapshot, path: values.get(path.value)
    content.source.verification_direction = Direction.MIXED
    content.source.verification_target_paths = targets
    content.source.inventory = replace(
        preparation.inventory,
        files=tuple(
            SourceChange(RepoPath(path), "modified", None, None)
            for path in sorted(
                ["ydb/docs/ru/core/a.png", "ydb/docs/en/core/b.png", "ydb/docs/ru/core/extra.png"]
            )
        ),
    )
    plans = content.select_source(content.prepare_source(preparation.snapshot, translate=False))
    assert {path for path, _ in plans.fixed_files} == {path.value for path in targets}


def test_fresh_bilingual_classification_reaches_real_pair_model_once():
    import json

    from ydbdoc_review_ng.direction import InventoryFile
    from ydbdoc_review_ng.models import ModelCallResult

    content, preparation = runtime([("a.md", "no_translation", "placeholder")])
    inventory = replace(preparation.inventory, pair_decisions=())
    content.source.inventory = inventory
    content.source.classifier_files = lambda roots: tuple(
        InventoryFile(change, b"Old text\n", b"New text\n", None, None)
        for change in inventory.files
    )
    requests = []

    def invoke(request):
        requests.append(request)
        return ModelCallResult(
            json.dumps(
                {
                    "key": "a.md",
                    "action": "no_translation",
                    "reason": "Both languages carry the new limit",
                }
            ),
            None,
            (),
        )

    content.models = SimpleNamespace(invoke=invoke)
    plans = content.select_source(replace(preparation, inventory=inventory))
    assert len(requests) == 1
    assert requests[0].role.value == "direction"
    assert plans.manifest is None and not plans.translation_plan.outputs
    assert plans.preparation.inventory.pair_decisions == (
        ("a.md", "no_translation", "Both languages carry the new limit"),
    )


def test_pure_rename_is_mirrored_without_semantic_classifier():
    content, preparation = runtime([("new.md", "ru_to_en", "placeholder")])
    new_ru = RepoPath("ydb/docs/ru/core/new.md")
    old_ru = RepoPath("ydb/docs/ru/core/old.md")
    values = {
        new_ru.value: b"# Current source\n",
        "ydb/docs/en/core/old.md": b"# Existing English\n",
    }
    content.source.github.read_bytes = lambda snapshot, path: values.get(path.value)
    content.source.inventory = replace(
        preparation.inventory,
        files=(SourceChange(new_ru, "renamed", old_ru, False),),
        pair_decisions=(),
    )
    plans = content.select_source(content.prepare_source(preparation.snapshot))
    assert plans.manifest.entries[0].operation.value == "rename_target"
    fixed = dict(plans.fixed_files)
    assert fixed["ydb/docs/en/core/old.md"] is None
    assert fixed["ydb/docs/en/core/new.md"] == b"# Existing English\n"


def test_legacy_model_parser_cannot_return_aggregate_mixed():
    import json

    import pytest

    from ydbdoc_review_ng.direction import parse_inventory_response

    _, preparation = runtime([])
    with pytest.raises(ValueError):
        parse_inventory_response(
            json.dumps(
                {"translation_required": True, "direction": "mixed", "reason": "invalid aggregate"}
            ),
            preparation.inventory,
        )
