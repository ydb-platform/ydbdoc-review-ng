"""Repair model-emitted split-backtick identifier mangling.

Translators sometimes "escape" underscores as either:

- `` `log`_`config` `` (each segment backticked), or
- `` log`_`config `` / `` `_`includes `` (only the underscore backticked).

Both break YFM includes, YAML examples, and table parameter columns. This module
is a deterministic post-restore repair: no model call.
"""

from __future__ import annotations

import re

# `log`_`config`, `default`_`sampling`_`level`
_TICKED_SEGMENTS = re.compile(rb"`([A-Za-z][A-Za-z0-9]*)`(_`([A-Za-z0-9]+)`)+")

# log`_`config or TABLET`_`MAIN (bare words, backticked underscore)
_BARE_JOINED = re.compile(
    rb"(?<![A-Za-z0-9`])([A-Za-z][A-Za-z0-9]*)`_`([A-Za-z][A-Za-z0-9]*)(?![A-Za-z0-9`])"
)

# `_`includes (leading underscore path/token mangling)
_LEADING_UNDERSCORE = re.compile(rb"`_`([A-Za-z][A-Za-z0-9]*)")


def _collapse_ticked(match: re.Match[bytes]) -> bytes:
    parts = [match.group(1), *re.findall(rb"_`([A-Za-z0-9]+)`", match.group(0))]
    return b"`" + b"_".join(parts) + b"`"


def _collapse_bare(match: re.Match[bytes]) -> bytes:
    return match.group(1) + b"_" + match.group(2)


def _collapse_leading(match: re.Match[bytes]) -> bytes:
    return b"_" + match.group(1)


def normalize_split_backtick_identifiers(draft: bytes, /) -> bytes:
    """Collapse split-backtick identifier runs; healthy markup is unchanged."""
    if type(draft) is not bytes:
        raise TypeError("draft must be exact bytes")
    text = draft
    # Repeat bare joins so a`_`b`_`c becomes a_b_c across passes.
    for _ in range(8):
        updated = _TICKED_SEGMENTS.sub(_collapse_ticked, text)
        updated = _BARE_JOINED.sub(_collapse_bare, updated)
        updated = _LEADING_UNDERSCORE.sub(_collapse_leading, updated)
        if updated == text:
            break
        text = updated
    return text


def count_split_backtick_identifiers(draft: bytes, /) -> int:
    """Count mangled runs still present (0 after a successful normalize)."""
    if type(draft) is not bytes:
        raise TypeError("draft must be exact bytes")
    return (
        len(_TICKED_SEGMENTS.findall(draft))
        + len(_BARE_JOINED.findall(draft))
        + len(_LEADING_UNDERSCORE.findall(draft))
    )
