from __future__ import annotations

import time

import pytest


@pytest.mark.unit
@pytest.mark.timeout(0.1, method="signal")
def test_timeout_canary() -> None:
    time.sleep(2)
