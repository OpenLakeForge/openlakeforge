from __future__ import annotations

import pytest

from olf.e2e import _perimeter_latency as lat
from olf.e2e._shell import E2EError


def test_summarize_reports_median_and_p95_in_ms() -> None:
    median, p95 = lat.summarize([i / 1000 for i in range(1, 21)])
    assert median == pytest.approx(10.5)
    assert 19 < p95 <= 20


def test_only_a_large_added_latency_fails() -> None:
    assert lat.require_acceptable((50, 80), (60, 120)) == (10, 40)
    with pytest.raises(E2EError, match="adds"):
        lat.require_acceptable((50, 80), (160, 120))
    with pytest.raises(E2EError, match="adds"):
        lat.require_acceptable((50, 80), (60, 400))
