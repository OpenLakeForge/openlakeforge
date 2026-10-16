from __future__ import annotations

import pytest

from olf.deployment.local.network_canary import PolicyNotEnforcedError, verdict


def _verdict(control: bool, denied_results: list[bool]) -> None:
    outcomes = iter(denied_results)
    verdict(lambda: control, lambda: next(outcomes), attempts=len(denied_results), sleep=lambda _: None)


def test_enforcing_cluster_passes() -> None:
    _verdict(True, [False])


def test_policy_that_lands_late_still_passes() -> None:
    _verdict(True, [True, True, False])


def test_cluster_that_never_blocks_is_reported_as_not_enforcing() -> None:
    with pytest.raises(PolicyNotEnforcedError, match="does not enforce"):
        _verdict(True, [True, True, True])


def test_failing_positive_control_is_inconclusive_not_enforcing() -> None:
    with pytest.raises(PolicyNotEnforcedError, match="inconclusive"):
        _verdict(False, [False])
