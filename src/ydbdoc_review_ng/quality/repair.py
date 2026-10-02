"""Whole-PR critic/arbiter with context-fitting file-pair chunks."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Literal, Protocol

import yaml

from ydbdoc_review_ng.domain import RepoPath
from ydbdoc_review_ng.models import AttemptError, ModelCallResult, ModelRequest
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import ProtectedKind, SourcePlan, fields_of
from ydbdoc_review_ng.quality.critic import (
    build_pr_arbiter_request,
    build_pr_critic_request,
    parse_pr_arbiter_response,
    parse_pr_critic_response,
)
from ydbdoc_review_ng.quality.types import CriticResult, Finding, Verdict
from ydbdoc_review_ng.translation import (
    ProtectedMismatch,
    TranslationRequest,
    build_translation_request,
    verify_document_candidate,
)
from ydbdoc_review_ng.translation.contract import field_request_text

_VERDICT_RANK = {Verdict.GREEN: 0, Verdict.YELLOW: 1, Verdict.RED: 2}
_UnreviewedReason = Literal["context", "provider", "contract", "missing"]


class ModelExecutor(Protocol):
    def invoke(self, request: ModelRequest, /) -> ModelCallResult: ...


class QualityInputError(ValueError):
    def __init__(self, reason: str = "inconsistent_candidate") -> None:
        self.reason = reason
        super().__init__(f"quality_input:{reason}")


class QualityExecutionError(RuntimeError):
    def __init__(self, stage: str, /) -> None:
        self.stage = stage
        super().__init__(f"quality_execution:{stage}")


def _locale_peer(path: str) -> str | None:
    if path.startswith("ru/"):
        return "en/" + path[3:]
    if path.startswith("en/"):
        return "ru/" + path[3:]
    if "/ru/" in path:
        return path.replace("/ru/", "/en/", 1)
    if "/en/" in path:
        return path.replace("/en/", "/ru/", 1)
    return None


def _review_pairs(
    source_files: Mapping[str, bytes],
    translated_files: Mapping[str, bytes | None],
) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for target_path in sorted(translated_files):
        source_path = _locale_peer(target_path)
        if source_path is not None and source_path in source_files:
            pairs.append((source_path, target_path))
    return tuple(pairs)


def _request_fits(
    executor: ModelExecutor,
    request: ModelRequest,
    override: Callable[[ModelRequest], bool] | None,
) -> bool:
    if override is not None:
        return override(request)
    prepare = getattr(executor, "prepare_request", None)
    if prepare is None:
        return True
    try:
        prepare(request)
    except ValueError:
        return False
    return True


def _pack_pair_chunks(
    pairs: Sequence[tuple[str, str]],
    *,
    build_request: Callable[[Sequence[tuple[str, str]]], ModelRequest],
    fits: Callable[[ModelRequest], bool],
) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Greedy whole-pair packing. Oversized single pairs are omitted (unreviewed)."""
    if not pairs:
        return ()
    if fits(build_request(pairs)):
        return (tuple(pairs),)
    chunks: list[tuple[tuple[str, str], ...]] = []
    current: list[tuple[str, str]] = []
    for pair in pairs:
        trial = (*current, pair)
        if fits(build_request(trial)):
            current.append(pair)
            continue
        if current:
            chunks.append(tuple(current))
            current = []
        if fits(build_request((pair,))):
            current = [pair]
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


def _subset_sources(
    source_files: Mapping[str, bytes], pairs: Sequence[tuple[str, str]]
) -> dict[str, bytes]:
    return {source: source_files[source] for source, _target in pairs}


def _subset_targets(
    translated_files: Mapping[str, bytes | None], pairs: Sequence[tuple[str, str]]
) -> dict[str, bytes | None]:
    return {target: translated_files[target] for _source, target in pairs}


def _worst_verdict(results: Sequence[CriticResult]) -> Verdict:
    worst = Verdict.GREEN
    for result in results:
        if _VERDICT_RANK[result.verdict] > _VERDICT_RANK[worst]:
            worst = result.verdict
    return worst


def _unreviewed_finding(target_path: str, reason: _UnreviewedReason) -> Finding:
    explanations = {
        "context": (
            "Файл не удалось проверить в доступном контексте модели.",
            "Уменьшите файл или продолжите проверку отдельно.",
        ),
        "provider": (
            "Файл не удалось проверить из-за сбоя модели или провайдера.",
            "Повторите проверку файла.",
        ),
        "contract": (
            "Ответ арбитра не соответствует формату проверки.",
            "Повторите проверку файла.",
        ),
        "missing": (
            "Обязательный итоговый файл отсутствует.",
            "Добавьте перевод этого файла и повторите проверку.",
        ),
    }
    public_reason, correction = explanations[reason]
    return Finding(
        False,
        public_reason,
        correction,
        None,
        target_path,
        None,
    )


def review_pr(
    executor: ModelExecutor,
    *,
    critic_model: str,
    arbiter_model: str,
    source_files: Mapping[str, bytes],
    translated_files: Mapping[str, bytes | None],
    glossary_files: Mapping[str, bytes],
    validate_files: Callable[[Mapping[str, bytes]], None],
    operator_context: str | None = None,
    before_model_call: Callable[[], None] | None = None,
    on_successful_critic_chunk: Callable[[Mapping[str, bytes]], None] | None = None,
    request_fits: Callable[[ModelRequest], bool] | None = None,
    toc_snapshots: Mapping[str, Mapping[str, str | None]] | None = None,
    binary_manifest: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[dict[str, bytes], CriticResult]:
    """Correct/judge the PR in context-fitting whole source/target pair chunks."""
    pairs = _review_pairs(source_files, translated_files)
    corrected: dict[str, bytes] = {
        path: content for path, content in translated_files.items() if content is not None
    }
    unreviewed: dict[str, _UnreviewedReason] = {}
    # §4: empty Markdown inventory still reviews resources; NON_FINAL cannot invent GREEN.
    resource_review_reason: _UnreviewedReason | None = None

    def mark_unreviewed(paths: Sequence[str], reason: _UnreviewedReason) -> None:
        for path in paths:
            unreviewed.setdefault(path, reason)

    def build_critic(chunk_pairs: Sequence[tuple[str, str]]) -> ModelRequest:
        return build_pr_critic_request(
            model=critic_model,
            source_files=_subset_sources(source_files, chunk_pairs),
            translated_files=_subset_targets(translated_files, chunk_pairs),
            glossary_files=glossary_files,
            operator_context=operator_context,
            toc_snapshots=toc_snapshots,
            binary_manifest=binary_manifest,
        )

    def fits(request: ModelRequest) -> bool:
        return _request_fits(executor, request, request_fits)

    critic_chunks = _pack_pair_chunks(pairs, build_request=build_critic, fits=fits)
    packed_targets = {target for chunk in critic_chunks for _source, target in chunk}
    for _source, target in pairs:
        if target not in packed_targets:
            mark_unreviewed((target,), "context")
    if not pairs:
        critic_chunks = ((),)

    for chunk_pairs in critic_chunks:
        critic = build_critic(chunk_pairs)
        target_paths = tuple(target for _source, target in chunk_pairs)
        response = None
        saw_non_final = False
        for attempt in (1, 2):
            if before_model_call is not None:
                before_model_call()
            response = executor.invoke(critic)
            if response.success and response.text is not None:
                try:
                    chunk_corrected = parse_pr_critic_response(
                        response.text, target_paths=target_paths
                    )
                    validate_files(chunk_corrected)
                except Exception:
                    if attempt == 2:
                        # Critic is a hard quality gate: invalid reviewed bytes must
                        # not fall through to arbiter as a product success (§4.1).
                        mark_unreviewed(target_paths, "contract")
                        if not pairs:
                            resource_review_reason = "contract"
                        break
                    continue
                corrected.update(chunk_corrected)
                if on_successful_critic_chunk is not None:
                    on_successful_critic_chunk(chunk_corrected)
                break
            if response.failure is AttemptError.NON_FINAL:
                # §4: NON_FINAL survives a differently-failed retry → chunk unreviewed.
                saw_non_final = True
            if attempt == 2:
                # Provider/transport/503 after retry: raw translator dump is not a
                # reviewed product. Mark unreviewed RED; skip arbiter for the chunk.
                mark_unreviewed(target_paths, "provider")
                if not pairs:
                    resource_review_reason = "provider"
                break

    arbiter_targets: dict[str, bytes | None] = {
        path: corrected.get(path, translated_files.get(path)) for path in translated_files
    }

    def build_arbiter(chunk_pairs: Sequence[tuple[str, str]]) -> ModelRequest:
        return build_pr_arbiter_request(
            model=arbiter_model,
            source_files=_subset_sources(source_files, chunk_pairs),
            translated_files={target: arbiter_targets[target] for _source, target in chunk_pairs},
            glossary_files=glossary_files,
            operator_context=operator_context,
            toc_snapshots=toc_snapshots,
            binary_manifest=binary_manifest,
        )

    reviewed_pairs = tuple(pair for pair in pairs if pair[1] not in unreviewed)
    arbiter_chunks = _pack_pair_chunks(reviewed_pairs, build_request=build_arbiter, fits=fits)
    packed_arbiter = {target for chunk in arbiter_chunks for _source, target in chunk}
    for _source, target in reviewed_pairs:
        if target not in packed_arbiter:
            mark_unreviewed((target,), "context")

    results: list[CriticResult] = []
    if not pairs:
        # Zero-text still calls arbiter once, unless critic already marked NON_FINAL.
        arbiter_chunks = () if resource_review_reason is not None else ((),)

    for chunk_pairs in arbiter_chunks:
        arbiter = build_arbiter(chunk_pairs)
        if before_model_call is not None:
            before_model_call()
        response = executor.invoke(arbiter)
        chunk_files = {target: arbiter_targets[target] for _source, target in chunk_pairs}
        if not chunk_pairs and binary_manifest:
            # §4.1: resource-only arbiter findings reference manifest paths.
            chunk_files = {path: None for path in binary_manifest}
        if not response.success or response.text is None:
            # §4: NON_FINAL → unreviewed RED with report, never a bare abort.
            mark_unreviewed(tuple(target for _source, target in chunk_pairs), "provider")
            if not chunk_pairs:
                resource_review_reason = "provider"
            continue
        try:
            results.append(parse_pr_arbiter_response(response.text, target_files=chunk_files))
        except Exception:
            mark_unreviewed(tuple(target for _source, target in chunk_pairs), "contract")
            if not chunk_pairs:
                resource_review_reason = "contract"

    findings = [finding for result in results for finding in result.findings]
    finding_paths = {finding.target_path for finding in findings}
    # §4.2: missing required target is a hole → RED with null coordinates.
    for path, value in arbiter_targets.items():
        if value is None:
            unreviewed[path] = "missing"
    for path, reason in sorted(unreviewed.items()):
        if path not in finding_paths:
            findings.append(_unreviewed_finding(path, reason))
            finding_paths.add(path)
    if resource_review_reason is not None and not findings:
        # Resource-only / zero-text NON_FINAL still needs a publishable finding (§4/§7).
        marker = next(iter(sorted(binary_manifest or ())), "resource-review")
        findings.append(_unreviewed_finding(marker, resource_review_reason))
    if unreviewed or resource_review_reason is not None:
        verdict = Verdict.RED
    elif results:
        verdict = _worst_verdict(results)
    else:
        verdict = Verdict.GREEN
    if (verdict is Verdict.GREEN) != (not findings):
        verdict = Verdict.RED if findings else Verdict.GREEN
    final_corrected = {
        path: corrected[path] for path in translated_files if path in corrected
    }
    for path, content in translated_files.items():
        if content is not None and path not in final_corrected:
            final_corrected[path] = content
    return final_corrected, CriticResult(verdict, tuple(findings))


def _derive_target_translations(
    source: bytes,
    source_plan: SourcePlan,
    translation_request: TranslationRequest,
    target: bytes,
    target_path: RepoPath,
) -> dict[str, str]:
    if translation_request != build_translation_request(source, source_plan):
        raise QualityInputError
    # REQUIREMENTS §2 / §4.1: YAML/Markdown plan diagnostics are soft. Critic UTF-8
    # corrections must still publish; derive maps best-effort or empty.
    try:
        target_plan = build_markdown_plan(source_plan.source_snapshot, target_path, target)
        verify_document_candidate(source, source_plan, target, target_plan)
    except (ProtectedMismatch, TypeError, ValueError, UnicodeError, yaml.YAMLError):
        raise QualityInputError from None
    target_fields = fields_of(target_plan)
    if len(target_fields) != len(translation_request.fields):
        raise QualityInputError
    values: dict[str, str] = {}
    for request_field, target_field in zip(translation_request.fields, target_fields, strict=True):
        source_groups: dict[int, tuple[tuple[ProtectedKind, bytes], ...]] = {}
        for placeholder in request_field.placeholders:
            if placeholder.group is not None:
                source_groups.setdefault(placeholder.group, ())
                source_groups[placeholder.group] += ((placeholder.kind, placeholder.source_bytes),)
        target_groups: dict[int, tuple[tuple[ProtectedKind, bytes], ...]] = {}
        for region in target_field.protected_regions:
            if region.group is not None:
                target_groups.setdefault(region.group, ())
                target_groups[region.group] += (
                    (region.kind, target[region.span.start : region.span.end]),
                )
        group_map: dict[int, int] = {}
        unused_source_groups = set(source_groups)
        for target_group, signature in target_groups.items():
            group_match = next(
                (
                    source_group
                    for source_group in unused_source_groups
                    if source_groups[source_group] == signature
                ),
                None,
            )
            if group_match is not None:
                group_map[target_group] = group_match
                unused_source_groups.remove(group_match)

        unused = list(request_field.placeholders)
        chunks: list[bytes] = []
        cursor = target_field.span.start
        for region in target_field.protected_regions:
            chunks.append(target[cursor : region.span.start])
            region_bytes = target[region.span.start : region.span.end]
            expected_group = group_map.get(region.group) if region.group is not None else None
            if region.group is not None and expected_group is None:
                chunks.append(region_bytes)
                cursor = region.span.end
                continue
            placeholder_match = next(
                (
                    placeholder
                    for placeholder in unused
                    if placeholder.kind is region.kind
                    and placeholder.source_bytes == region_bytes
                    and placeholder.group == expected_group
                ),
                None,
            )
            if placeholder_match is None:
                chunks.append(region_bytes)
                cursor = region.span.end
                continue
            chunks.append(placeholder_match.token.encode("ascii"))
            unused.remove(placeholder_match)
            cursor = region.span.end
        chunks.append(target[cursor : target_field.span.end])
        if unused:
            raise QualityInputError
        try:
            values[request_field.field_id] = field_request_text(
                target, target_plan, target_field, b"".join(chunks)
            )
        except UnicodeDecodeError:
            raise QualityInputError from None
    return values
