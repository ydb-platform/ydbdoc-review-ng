from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from .contract import ContractError, parse_and_validate_field_map
from .plan import Field, TranslationPlan


CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
FORBIDDEN_CHARACTER_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2060\ufeff]")
COMPACT_REFERENCE_RE = re.compile(r"__REF_\d+_[A-Za-z0-9_]+?__")
SAFE_SEPARATOR = r"[^<>\u0000-\u001F\u007F\u200B-\u200F\u2060\uFEFF]*"


def build_single_field_prompt(
    *,
    source_path: str,
    field: Field,
    before: str,
    after: str,
    allow_reference_reordering: bool = False,
) -> str:
    payload = {
        "source_path": source_path,
        "source_locale": "ru",
        "target_locale": "en",
        "translate_only": {field.field_id: field.model_text},
        "context_only_do_not_translate": {"before": before, "after": after},
    }
    reference_rule = (
        "Copy every __REF token, including its read-only semantic suffix, exactly once. "
        "You may reorder complete __REF tokens when English grammar requires it. "
        "Keep each LINK_OPEN/LINK_CLOSE pair intact, with its translated label between them. "
        if allow_reference_reordering
        else "Copy every __REF token, including its read-only semantic suffix, exactly once and in order. "
    )
    return (
        "Translate exactly the one value in translate_only. Use context_only_do_not_translate "
        "only to resolve terminology and references. "
        + reference_rule
        +
        "Do not copy or summarize context. Use plain visible UTF-8 text without control, "
        "ANSI, or zero-width characters. "
        "Return only the schema-bound map.\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def validate_single_field_translation(field: Field, translated: str) -> str:
    if not translated.strip():
        raise ContractError(f"empty translation for {field.field_id}")
    if CYRILLIC_RE.search(translated):
        raise ContractError(f"Cyrillic prose remains in {field.field_id}")
    if FORBIDDEN_CHARACTER_RE.search(translated):
        raise ContractError(f"forbidden character in {field.field_id}")
    return translated


def compact_model_field(field: Field) -> Field:
    model_text = field.model_text
    for atom, token in zip(field.atoms, compact_reference_tokens(field), strict=True):
        model_text = model_text.replace(atom.token, token)
    return replace(field, model_text=model_text)


def _reference_hint(source_text: str) -> str:
    code_link = re.match(r"`([^`]+)`\]\(", source_text)
    if code_link:
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", code_link.group(1)).strip("_")
        return "CODE_LINK_" + (normalized[:48] or "BYTES")
    if source_text in {"[", "!["}:
        return "LINK_OPEN"
    if source_text.startswith("]("):
        return "LINK_CLOSE"
    if source_text.startswith("`") and source_text.endswith("`"):
        prefix = "CODE_"
        source_text = source_text.strip("`")
    elif source_text.startswith("{{") and source_text.endswith("}}"):
        prefix = "TEMPLATE_"
        source_text = source_text[2:-2]
    else:
        prefix = "PROTECTED_"
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", source_text).strip("_")
    return prefix + (normalized[:48] or "BYTES")


def compact_reference_tokens(field: Field) -> list[str]:
    return [
        f"__REF_{index}_{_reference_hint(atom.source_text)}__"
        for index, atom in enumerate(field.atoms, start=1)
    ]


def _validate_link_pairs(field: Field, tokens: list[str]) -> None:
    expected = compact_reference_tokens(field)
    link_tokens = [
        token
        for token in expected
        if token.endswith("_LINK_OPEN__") or token.endswith("_LINK_CLOSE__")
    ]
    if len(link_tokens) < 2 or len(link_tokens) % 2:
        return
    expected_pairs = {
        (link_tokens[index], link_tokens[index + 1])
        for index in range(0, len(link_tokens), 2)
    }
    seen_link_tokens = [token for token in tokens if token in link_tokens]
    seen_pairs = [
        (seen_link_tokens[index], seen_link_tokens[index + 1])
        for index in range(0, len(seen_link_tokens), 2)
    ]
    if any(pair not in expected_pairs for pair in seen_pairs):
        raise ContractError(f"compact reference link pair invalid in {field.field_id}")


def restore_compact_references(
    field: Field, translated: str, *, require_order: bool = True
) -> str:
    expected = compact_reference_tokens(field)
    seen = COMPACT_REFERENCE_RE.findall(translated)
    unknown = [token for token in seen if token not in expected]
    if unknown:
        raise ContractError(f"unknown compact reference in {field.field_id}: {unknown[0]}")
    for token in expected:
        if translated.count(token) != 1:
            raise ContractError(f"compact reference count invalid in {field.field_id}: {token}")
    if require_order and seen != expected:
        raise ContractError(f"compact reference order invalid in {field.field_id}")
    if not require_order:
        _validate_link_pairs(field, seen)
    for token, atom in zip(expected, field.atoms, strict=True):
        translated = translated.replace(token, atom.token)
    return translated


def build_single_field_schema(
    field: Field, *, require_order: bool = True
) -> dict[str, Any]:
    tokens = compact_reference_tokens(field)
    if tokens and require_order:
        sequence = SAFE_SEPARATOR.join(re.escape(token) for token in tokens)
        pattern = f"^{SAFE_SEPARATOR}{sequence}{SAFE_SEPARATOR}$"
    else:
        pattern = f"^{SAFE_SEPARATOR}$"
    return {
        "type": "object",
        "properties": {field.field_id: {"type": "string", "pattern": pattern}},
        "required": [field.field_id],
        "additionalProperties": False,
    }


def split_prose_segments(field: Field) -> list[str]:
    segments: list[str] = []
    cursor = 0
    for atom in field.atoms:
        position = field.model_text.find(atom.token, cursor)
        if position < 0:
            raise ContractError(f"source reference missing from {field.field_id}: {atom.token}")
        segments.append(field.model_text[cursor:position])
        cursor = position + len(atom.token)
    segments.append(field.model_text[cursor:])
    return segments


def translate_field_by_segments(
    *,
    source_path: str,
    field: Field,
    api_key: str,
    folder_id: str,
    model_uri: str,
    complete_fn: Any,
) -> tuple[str, list[dict[str, Any]]]:
    output: list[str] = []
    responses: list[dict[str, Any]] = []
    segments = split_prose_segments(field)
    for index, segment in enumerate(segments):
        if CYRILLIC_RE.search(segment):
            part = Field(
                f"{field.field_id}_part_{index + 1:02d}",
                0,
                len(segment),
                segment,
                segment,
                (),
            )
            translated, response = translate_field(
                source_path=source_path,
                source=segment,
                field=part,
                api_key=api_key,
                folder_id=folder_id,
                model_uri=model_uri,
                complete_fn=complete_fn,
            )
            output.append(translated)
            responses.append(response)
        else:
            output.append(segment)
        if index < len(field.atoms):
            output.append(field.atoms[index].token)
    translated_field = "".join(output)
    validate_single_field_translation(field, translated_field)
    return translated_field, responses


def context_for_field(source: str, field: Field, *, window: int = 500) -> tuple[str, str]:
    before = source[max(0, field.char_start - window) : field.char_start]
    after = source[field.char_end : field.char_end + window]
    return before, after


def translate_field(
    *,
    source_path: str,
    source: str,
    field: Field,
    api_key: str,
    folder_id: str,
    model_uri: str,
    complete_fn: Any,
    allow_reference_reordering: bool = False,
) -> tuple[str, dict[str, Any]]:
    compact_field = compact_model_field(field)
    prompt = build_single_field_prompt(
        source_path=source_path,
        field=compact_field,
        before="",
        after="",
        allow_reference_reordering=allow_reference_reordering,
    )
    schema = build_single_field_schema(
        field, require_order=not allow_reference_reordering
    )
    raw_text, response = complete_fn(
        api_key=api_key,
        folder_id=folder_id,
        model_uri=model_uri,
        prompt=prompt,
        schema=schema,
        max_tokens=2_000,
        timeout_seconds=45,
    )
    result = parse_and_validate_field_map(raw_text, [field.field_id])
    restored = restore_compact_references(
        field,
        result[field.field_id],
        require_order=not allow_reference_reordering,
    )
    translated = validate_single_field_translation(field, restored)
    TranslationPlan(source=source, fields=(field,)).assemble(
        {field.field_id: translated},
        reorderable_fields={field.field_id} if allow_reference_reordering else None,
    )
    return translated, response
