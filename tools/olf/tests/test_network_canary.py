from __future__ import annotations

import pytest

from olf.deployment.local.network_canary import PolicyNotEnforcedError, verdict


class _Pair:
    """A pod pair whose reachability depends on which deny policy is applied and which the cluster enforces."""

    def __init__(self, *, enforces: set[str], broken: bool = False, late: int = 0, lingers: int = 0) -> None:
        self.enforces, self.broken, self.late, self.lingers = enforces, broken, late, lingers
        self.applied: str | None = None
        self.lingering = 0

    def connects(self) -> bool:
        if self.broken:
            return False
        if self.lingering > 0:
            self.lingering -= 1
            return False
        if self.applied in self.enforces:
            if self.late > 0:
                self.late -= 1
                return True
            return False
        return True

    def deny(self, direction: str):  # noqa: ANN201
        return (direction, lambda: setattr(self, "applied", direction), self._remove)

    def _remove(self) -> None:
        if self.applied in self.enforces:
            self.lingering = self.lingers
        self.applied = None

    def run(self) -> None:
        verdict(self.connects, [self.deny("ingress"), self.deny("egress")], sleep=lambda _: None)


def test_both_directions_blocked_passes() -> None:
    _Pair(enforces={"ingress", "egress"}).run()


def test_policy_that_lands_late_still_passes() -> None:
    _Pair(enforces={"ingress", "egress"}, late=2).run()


def test_policy_that_lingers_after_removal_is_not_inconclusive() -> None:
    _Pair(enforces={"ingress", "egress"}, lingers=2).run()


def test_ingress_enforced_but_not_egress_names_egress() -> None:
    with pytest.raises(PolicyNotEnforcedError, match="does not enforce egress"):
        _Pair(enforces={"ingress"}).run()


def test_nothing_enforced_names_ingress() -> None:
    with pytest.raises(PolicyNotEnforcedError, match="does not enforce ingress"):
        _Pair(enforces=set()).run()


def test_pair_that_never_connects_is_inconclusive() -> None:
    with pytest.raises(PolicyNotEnforcedError, match="inconclusive"):
        _Pair(enforces={"ingress", "egress"}, broken=True).run()


def test_policy_is_removed_even_when_it_fails() -> None:
    pair = _Pair(enforces=set())
    with pytest.raises(PolicyNotEnforcedError):
        pair.run()
    assert pair.applied is None
