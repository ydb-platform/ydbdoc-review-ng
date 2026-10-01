"""Missing document images travel through the real publication and verify paths."""

import pytest
from test_checkpoint_capture import CaptureServices

from ydbdoc_review_ng.application import VerifyWorkflowInput, WorkflowError
from ydbdoc_review_ng.domain import GitSha


@pytest.mark.parametrize('damage', [None, 'delete', 'change'])
def test_translate_publishes_asset_and_verify_checks_source_owned_bytes(damage):
    services = CaptureServices(names=('a',))
    source_asset = 'ydb/docs/ru/_assets/chart.png'
    target_asset = source_asset.replace('/ru/', '/en/')
    content = b'\x89PNG\x00\xff'
    for tree in [services.files, *services.snapshots.values()]:
        tree['ydb/docs/ru/core/a.md'] = b'# Source\n\n![Source chart](../_assets/chart.png)\n'
        tree[source_asset] = content
    services.translate()
    assert services.files[target_asset] == content
    assert target_asset in services.snapshots[services.translated]
    assert services.roles == ['direction', 'translate', 'critic', 'arbiter']
    services.roles.clear()
    if damage is not None:
        if damage == 'delete':
            del services.snapshots[services.translated][target_asset]
        else:
            services.snapshots[services.translated][target_asset] = b'wrong bytes'
    request = VerifyWorkflowInput(43, GitSha(services.source), GitSha(services.translated))
    if damage is None:
        services.runtime().doc_verify(request)
        assert services.roles == ['critic', 'arbiter']
    else:
        with pytest.raises(WorkflowError):
            services.runtime().doc_verify(request)
        assert services.roles == []


def test_continue_reconstructs_assets_without_retranslating_accepted_document():
    from test_continue_translation import ContinueServices

    services = ContinueServices(names=('a', 'b'), stop='translation')
    for tree in [services.files, *services.snapshots.values()]:
        tree['ydb/docs/ru/core/a.md'] = b'# Source\n\n![Source](../_assets/chart.svg)\n'
        tree['ydb/docs/ru/_assets/chart.svg'] = b'<svg>frozen</svg>'
    saved = services.stop_and_continue()
    assert saved.state.pending_paths
    services.resume()
    assert services.files['ydb/docs/en/_assets/chart.svg'] == b'<svg>frozen</svg>'
    assert services.roles.count('translate') >= 1
