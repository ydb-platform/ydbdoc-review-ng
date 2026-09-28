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
if "WARNING" in text:
    print('WARN en/page.md: 2: YFM010 / unreachable-autotitle-anchor Existing warning')
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


def test_validator_accepts_nonfatal_diplodoc_warning(tmp_path: Path) -> None:
    module = _validator_module()
    docs = tmp_path / "ydb/docs"
    page = docs / "en/page.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"original\n")
    validator = module.DiplodocBuildValidator(
        docs,
        command=(sys.executable, str(_fake_yfm(tmp_path))),
    )

    validator(_plan(b"original\n", b"WARNING\n"))

    assert page.read_bytes() == b"original\n"


def test_existing_translation_overlays_unchanged_metadata_and_assets(tmp_path: Path) -> None:
    module = _validator_module()
    docs = tmp_path / "ydb/docs"
    page = docs / "en/page.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"base page\n")
    obsolete = docs / "en/obsolete.md"
    obsolete.write_bytes(b"base-only obsolete page\n")
    toc = b"items:\n- href: page.md\n"
    image = b"\x89PNG\x00\xff"
    script = tmp_path / "check_complete_candidate.py"
    script.write_text('''from pathlib import Path
import sys
root = Path(sys.argv[sys.argv.index('-i') + 1])
assert (root / 'en/page.md').read_bytes() == b'new translation\\n'
assert (root / 'en/toc.yaml').read_bytes() == b'items:\\n- href: page.md\\n'
assert (root / 'en/chart.png').read_bytes() == b'\\x89PNG\\x00\\xff'
assert not (root / 'en/obsolete.md').exists()
''')
    plan = PublicationPlan((
        FileChange(RepoPath("ydb/docs/en/page.md"), b"old translation\n", b"new translation\n"),
        FileChange(RepoPath("ydb/docs/en/toc.yaml"), toc, toc),
        FileChange(RepoPath("ydb/docs/en/chart.png"), image, image),
        FileChange(RepoPath("ydb/docs/en/obsolete.md"), None, None),
    ), ())
    module.DiplodocBuildValidator(docs, command=(sys.executable, str(script)))(plan)
    assert page.read_bytes() == b"base page\n"
    assert obsolete.read_bytes() == b"base-only obsolete page\n"
    assert not (docs / "en/toc.yaml").exists()
    assert not (docs / "en/chart.png").exists()


def test_unchanged_candidate_still_runs_build_and_restores_checkout(tmp_path: Path) -> None:
    module = _validator_module()
    docs = tmp_path / "ydb/docs"
    page = docs / "en/page.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"base page\n")
    validator = module.DiplodocBuildValidator(
        docs, command=(sys.executable, str(_fake_yfm(tmp_path))),
    )
    with pytest.raises(module.DiplodocBuildError):
        validator(_plan(b"BROKEN\n", b"BROKEN\n"))
    assert page.read_bytes() == b"base page\n"
