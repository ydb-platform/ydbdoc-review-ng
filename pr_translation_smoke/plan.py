from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .contract import ContractError


CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
REFERENCE_RE = re.compile(r"<S\d+_\d+>")
LINK_RE = re.compile(r"(!?)\[([^\]\n]*)\]\(([^)\n]+)\)")
ATOM_RE = re.compile(
    r"`+[^`\n]+`+"
    r"|\{\{[^{}\n]+\}\}"
    r"|https?://[^\s)]+"
    r"|\{#[^{}\n]+\}"
    r"|</?[A-Za-z][^>\n]*>"
    r"|<[A-Za-z_][^>\n]*>"
    r"|\s\|\s"
)


@dataclass(frozen=True)
class SourceAtom:
    token: str
    source_text: str
    byte_start: int
    byte_end: int
    sha256: str


@dataclass(frozen=True)
class Field:
    field_id: str
    char_start: int
    char_end: int
    source_text: str
    model_text: str
    atoms: tuple[SourceAtom, ...]


@dataclass(frozen=True)
class TranslationPlan:
    source: str
    fields: tuple[Field, ...]

    def assemble(
        self,
        translations: dict[str, str],
        *,
        reorderable_fields: set[str] | None = None,
    ) -> str:
        expected = {field.field_id for field in self.fields}
        actual = set(translations)
        if expected != actual:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ContractError(f"field map mismatch; missing={missing}, extra={extra}")

        source_bytes = self.source.encode("utf-8")
        output: list[str] = []
        cursor = 0
        for field in self.fields:
            value = translations[field.field_id]
            if not isinstance(value, str):
                raise ContractError(f"value for {field.field_id} must be a string")
            output.append(self.source[cursor : field.char_start])
            output.append(
                _restore_atoms(
                    value,
                    field,
                    source_bytes,
                    require_order=not (
                        reorderable_fields and field.field_id in reorderable_fields
                    ),
                )
            )
            cursor = field.char_end
        output.append(self.source[cursor:])
        return "".join(output)


def _restore_atoms(
    value: str,
    field: Field,
    source_bytes: bytes,
    *,
    require_order: bool = True,
) -> str:
    expected_tokens = [atom.token for atom in field.atoms]
    seen_tokens = REFERENCE_RE.findall(value)
    unknown = [token for token in seen_tokens if token not in expected_tokens]
    if unknown:
        raise ContractError(f"unknown source reference in {field.field_id}: {unknown[0]}")
    for token in expected_tokens:
        if value.count(token) != 1:
            raise ContractError(f"source reference count invalid in {field.field_id}: {token}")
    if require_order and seen_tokens != expected_tokens:
        raise ContractError(f"source reference order invalid in {field.field_id}")

    restored = value
    for atom in field.atoms:
        exact_bytes = source_bytes[atom.byte_start : atom.byte_end]
        if hashlib.sha256(exact_bytes).hexdigest() != atom.sha256:
            raise ContractError(f"source bytes changed for {atom.token}")
        if exact_bytes.decode("utf-8") != atom.source_text:
            raise ContractError(f"source byte range is invalid for {atom.token}")
        restored = restored.replace(atom.token, atom.source_text)
    return restored


def _byte_offset(source: str, char_offset: int) -> int:
    return len(source[:char_offset].encode("utf-8"))


def _make_atom(source: str, char_start: int, char_end: int) -> SourceAtom:
    source_text = source[char_start:char_end]
    byte_start = _byte_offset(source, char_start)
    raw = source_text.encode("utf-8")
    byte_end = byte_start + len(raw)
    digest = hashlib.sha256(raw).hexdigest()
    token = f"<S{byte_start}_{len(raw)}>"
    return SourceAtom(token, source_text, byte_start, byte_end, digest)


def _coalesce_adjacent_atoms(
    model_text: str, atoms: list[SourceAtom]
) -> tuple[str, tuple[SourceAtom, ...]]:
    coalesced: list[SourceAtom] = []
    for atom in atoms:
        if coalesced and coalesced[-1].byte_end == atom.byte_start:
            previous = coalesced[-1]
            adjacent_tokens = previous.token + atom.token
            if adjacent_tokens in model_text:
                source_text = previous.source_text + atom.source_text
                raw = source_text.encode("utf-8")
                combined = SourceAtom(
                    token=f"<S{previous.byte_start}_{len(raw)}>",
                    source_text=source_text,
                    byte_start=previous.byte_start,
                    byte_end=atom.byte_end,
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
                model_text = model_text.replace(adjacent_tokens, combined.token, 1)
                coalesced[-1] = combined
                continue
        coalesced.append(atom)
    return model_text, tuple(coalesced)


def _protect_plain_segment(
    source: str, text: str, absolute_start: int
) -> tuple[str, list[SourceAtom]]:
    output: list[str] = []
    atoms: list[SourceAtom] = []
    cursor = 0
    for match in ATOM_RE.finditer(text):
        output.append(text[cursor : match.start()])
        atom = _make_atom(
            source,
            absolute_start + match.start(),
            absolute_start + match.end(),
        )
        atoms.append(atom)
        output.append(atom.token)
        cursor = match.end()
    output.append(text[cursor:])
    return "".join(output), atoms


def _protect_field(source: str, text: str, absolute_start: int) -> tuple[str, tuple[SourceAtom, ...]]:
    output: list[str] = []
    atoms: list[SourceAtom] = []
    cursor = 0
    for match in LINK_RE.finditer(text):
        before, before_atoms = _protect_plain_segment(
            source, text[cursor : match.start()], absolute_start + cursor
        )
        output.append(before)
        atoms.extend(before_atoms)

        open_end = match.start(2)
        open_atom = _make_atom(source, absolute_start + match.start(), absolute_start + open_end)
        output.append(open_atom.token)
        atoms.append(open_atom)

        label, label_atoms = _protect_plain_segment(
            source, match.group(2), absolute_start + match.start(2)
        )
        output.append(label)
        atoms.extend(label_atoms)

        close_atom = _make_atom(source, absolute_start + match.end(2), absolute_start + match.end())
        output.append(close_atom.token)
        atoms.append(close_atom)
        cursor = match.end()

    tail, tail_atoms = _protect_plain_segment(source, text[cursor:], absolute_start + cursor)
    output.append(tail)
    atoms.extend(tail_atoms)
    return _coalesce_adjacent_atoms("".join(output), atoms)


def _content_bounds(line: str, in_frontmatter: bool) -> tuple[int, int] | None:
    body = line[:-1] if line.endswith("\n") else line
    if not body.strip():
        return None
    stripped = body.strip()
    if stripped in {"#|", "|#", "||"} or stripped.startswith("{%"):
        return None

    if in_frontmatter:
        match = re.match(r"^(title|description):(\s*)(.*)$", body)
        if not match:
            return None
        return match.start(3), match.end(3)

    heading = re.match(r"^#{1,6}\s+(.*?)(\s+\{#[^{}]+\})?$", body)
    if heading:
        return heading.start(1), heading.end(1)

    list_item = re.match(r"^\s*(?:[-*+]|\d+\.)\s+(.*)$", body)
    if list_item:
        return list_item.start(1), list_item.end(1)

    quote = re.match(r"^\s*>\s+(.*)$", body)
    if quote:
        return quote.start(1), quote.end(1)

    table = re.match(r"^\s*\|\|?\s+(.*?)(?:\s+\|\|)?$", body)
    if table:
        return table.start(1), table.end(1)

    return 0, len(body)


def build_plan(source: str) -> TranslationPlan:
    fields: list[Field] = []
    char_offset = 0
    in_fence = False
    fence_marker = ""
    in_frontmatter = False
    frontmatter_seen = False

    for line in source.splitlines(keepends=True):
        stripped = line.strip()
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)[0]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            char_offset += len(line)
            continue

        if char_offset == 0 and stripped == "---":
            in_frontmatter = True
            frontmatter_seen = True
            char_offset += len(line)
            continue
        if in_frontmatter and stripped == "---" and frontmatter_seen:
            in_frontmatter = False
            char_offset += len(line)
            continue

        if not in_fence:
            bounds = _content_bounds(line, in_frontmatter)
            if bounds is not None:
                relative_start, relative_end = bounds
                text = line[relative_start:relative_end]
                if CYRILLIC_RE.search(text):
                    absolute_start = char_offset + relative_start
                    absolute_end = char_offset + relative_end
                    model_text, atoms = _protect_field(source, text, absolute_start)
                    field_id = f"field_{len(fields) + 1:04d}"
                    fields.append(
                        Field(
                            field_id=field_id,
                            char_start=absolute_start,
                            char_end=absolute_end,
                            source_text=text,
                            model_text=model_text,
                            atoms=atoms,
                        )
                    )
        char_offset += len(line)

    return TranslationPlan(source=source, fields=tuple(fields))
