from __future__ import annotations

import pytest

from olf.infra.deployment.local.network_canary import PolicyNotEnforcedError, verdict


class _Pair:
    """A pod pair whose reachability depends on which deny policy is applied and which the cluster enforces."""

    def __init__(
        self,
        *,
        enforces: set[str],
        broken: bool = False,
        late: int = 0,
        lingers: int = 0,
        honours_allow: bool = True,
        allow_lingers: bool = False,
    ) -> None:
        self.enforces, self.broken, self.late, self.lingers = enforces, broken, late, lingers
        self.honours_allow, self.allow_lingers = honours_allow, allow_lingers
        self.applied: set[str] = set()
        self.lingering = 0

    def connects(self) -> bool:
        if self.broken:
            return False
        if self.lingering > 0:
            self.lingering -= 1
            return False
        if "allow" in self.applied and self.honours_allow:
            return True
        if self.applied & self.enforces:
            if self.late > 0:
                self.late -= 1
                return True
            return False
        return True

    def _policy(self, name: str):  # noqa: ANN202
        def remove() -> None:
            if name in self.enforces:
                self.lingering = self.lingers
            if not (name == "allow" and self.allow_lingers):
                self.applied.discard(name)

        return lambda: self.applied.add(name), remove

    def deny(self, direction: str):  # noqa: ANN201
        return (direction, *self._policy(direction))

    def run(self) -> None:
        verdict(
            self.connects,
            [self.deny("ingress"), self.deny("egress")],
            (self._policy("ingress"), self._policy("allow")),
            sleep=lambda _: None,
        )


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
    assert not pair.applied


def test_deny_enforced_but_allow_ignored_fails() -> None:
    with pytest.raises(PolicyNotEnforcedError, match="blocks traffic a NetworkPolicy allows"):
        _Pair(enforces={"ingress", "egress"}, honours_allow=False).run()


def test_allow_that_outlives_its_removal_fails() -> None:
    with pytest.raises(PolicyNotEnforcedError, match="after its NetworkPolicy allow rule was removed"):
        _Pair(enforces={"ingress", "egress"}, allow_lingers=True).run()


def test_allow_check_removes_its_policies_when_it_fails() -> None:
    pair = _Pair(enforces={"ingress", "egress"}, honours_allow=False)
    with pytest.raises(PolicyNotEnforcedError):
        pair.run()
    assert not pair.applied


def test_failure_no_longer_points_at_calico() -> None:
    with pytest.raises(PolicyNotEnforcedError) as raised:
        _Pair(enforces=set()).run()
    assert "Calico" not in str(raised.value)
    assert "not supported" in str(raised.value)
