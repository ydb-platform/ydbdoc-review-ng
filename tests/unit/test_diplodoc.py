from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from ydbdoc_review_ng.domain import RepoPath
from ydbdoc_review_ng.publication import FileChange, PublicationPlan

pytestmark = pytest.mark.unit


def _validator_module():
    return importlib.import_module("ydbdoc_review_ng.diplodoc")


def _fake_yfm(tmp_path: Path) -> Path:
    script = tmp_path / "fake_yfm.py"
    script.write_text(
        """\
from pathlib import Path
import sys

root = Path(sys.argv[sys.argv.index("-i") + 1])
text = (root / "en/page.md").read_text()
if "BROKEN" in text:
    print('ERR en/page.md: 2: MD042 / no-empty-links No empty links')
    raise SystemExit(1)
print("INFO build complete")
""",
        encoding="utf-8",
    )
    return script


def _plan(before: bytes, after: bytes) -> PublicationPlan:
    return PublicationPlan(
        (FileChange(RepoPath("ydb/docs/en/page.md"), before, after),),
        (),
    )


def test_validator_reports_diplodoc_issue_and_restores_checkout(tmp_path: Path) -> None:
    module = _validator_module()
    docs = tmp_path / "ydb/docs"
    page = docs / "en/page.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"original\n")
    validator = module.DiplodocBuildValidator(
        docs,
        command=(sys.executable, str(_fake_yfm(tmp_path))),
    )

    with pytest.raises(module.DiplodocBuildError) as raised:
        validator(_plan(b"original\n", b"BROKEN\n"))

    assert raised.value.issues == (
        "ERR en/page.md: 2: MD042 / no-empty-links No empty links",
    )
    assert page.read_bytes() == b"original\n"


def test_validator_accepts_clean_build_and_restores_checkout(tmp_path: Path) -> None:
    module = _validator_module()
    docs = tmp_path / "ydb/docs"
    page = docs / "en/page.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"original\n")
    validator = module.DiplodocBuildValidator(
        docs,
        command=(sys.executable, str(_fake_yfm(tmp_path))),
    )

    validator(_plan(b"original\n", b"translated\n"))

    assert page.read_bytes() == b"original\n"
