"""Thin unwrap must not eat a document that starts with a real code fence."""

from __future__ import annotations

from ydbdoc_review_ng.translation.thin import unwrap_thin_response


def test_unwrap_keeps_check_backup_file_starting_with_bash_fence() -> None:
    source = (
        "```bash\n"
        "ls /path/to/backup/snapshot/\n"
        "```\n\n"
        "```text\n"
        "manifest.json\n"
        "```\n"
    )
    assert unwrap_thin_response(source) == source


def test_unwrap_strips_markdown_wrapper_around_a_fence_file() -> None:
    inner = (
        "```bash\n"
        "ls /path/to/backup/snapshot/\n"
        "```\n"
    )
    wrapped = "```markdown\n" + inner + "```\n"
    assert unwrap_thin_response(wrapped) == inner


def test_unwrap_strips_unlabeled_wrapper_around_prose() -> None:
    assert unwrap_thin_response("```\n# Recovery\n\nPut the tablet back.\n```\n") == (
        "# Recovery\n\nPut the tablet back.\n"
    )
