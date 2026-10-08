"""Offline first-stage CLI. Paid runs require future admission and persistent-audit wiring."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ydbdoc_review_ng.policy_review.engine import review_snapshot
from ydbdoc_review_ng.policy_review.types import ReviewError, ReviewSnapshot


def main(snapshot_path: str, output_path: str) -> int:
    try:
        # These paths are explicit operator inputs, never paths extracted from PR content.
        with Path(snapshot_path).open("rb") as stream:
            body = stream.read(25_000_001)
        if len(body) > 25_000_000:
            raise ReviewError("snapshot_text_limit_exceeded")
        snapshot = ReviewSnapshot.from_json(json.loads(body))
        report = review_snapshot(snapshot)
        Path(output_path).write_text(
            json.dumps(report.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    except ReviewError as error:
        print(f"Documentation review failed: {error.code}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - public errors must not echo file contents or paths.
        print("Documentation review input or output is unavailable", file=sys.stderr)
        return 2
    print(f"Documentation review: {report.status}")
    return 0
