"""Verification accepts a translation PR containing both target locales."""
from types import SimpleNamespace

import pytest

from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import GitSha, RepoPath
from ydbdoc_review_ng.runtime import RuntimeSource
from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError


@pytest.mark.parametrize('paths, expected', [
    (['ydb/docs/en/core/a.md'], 'ru_to_en'),
    (['ydb/docs/ru/core/b.md'], 'en_to_ru'),
    (['ydb/docs/en/core/a.md', 'ydb/docs/ru/core/b.md'], 'mixed'),
])
def test_verify_snapshot_infers_each_target_locale(paths, expected):
    sha = GitSha('a' * 40)
    pull = {'changed_files': len(paths), 'head': {'sha': sha.value}, 'base': {'ref': 'main'}}
    source = object.__new__(RuntimeSource)
    source.github = SimpleNamespace(request=lambda *args: pull)
    source._snapshot = lambda *args: 'immutable-snapshot'
    source._pr_files = lambda *args: [
        {'filename': path, 'status': 'modified', 'changes': 1} for path in paths
    ]
    auth = SimpleNamespace(context=(SimpleNamespace(
        pr_number=12, source_sha=sha, target_sha=sha,
    ), 10, 'main'))
    assert source.snapshot_verify(auth) == 'immutable-snapshot'
    assert source.verification_direction is Direction(expected)
    assert source.verification_target_paths == tuple(RepoPath(path) for path in sorted(paths))


def test_verify_snapshot_still_rejects_missing_localized_targets():
    sha = GitSha('a' * 40)
    pull = {'changed_files': 0, 'head': {'sha': sha.value}, 'base': {'ref': 'main'}}
    source = object.__new__(RuntimeSource)
    source.github = SimpleNamespace(request=lambda *args: pull)
    source._snapshot = lambda *args: 'immutable-snapshot'
    source._pr_files = lambda *args: []
    auth = SimpleNamespace(context=(SimpleNamespace(
        pr_number=12, source_sha=sha, target_sha=sha,
    ), 10, 'main'))
    with pytest.raises(RuntimeBoundaryError, match='verification_direction_missing'):
        source.snapshot_verify(auth)
