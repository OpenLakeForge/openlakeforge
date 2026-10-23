from __future__ import annotations

from typing import Any

import pytest

from olf.e2e import _issuer_outage as drill
from olf.e2e._shell import E2EError

REASON = "e2e issuer-outage drill abc"


def _record(service: str, **extra: str) -> dict[str, str]:
    return {"service": service, "reason": REASON, "user": "op", "ended": "t", "duration_seconds": "3", **extra}


class Cluster:
    """A Dex with a replica count; the break-glass paths answer only while it is at zero."""

    def __init__(self, **overrides: Any) -> None:
        self.dex = 2
        self.waited_up = False
        self.refused = overrides.get("refused", True)
        self.sign_ins = overrides.get("sign_ins", [True])
        self.statuses = overrides.get("statuses", {"trino": 200, "dagster": 200})
        self.recorded = overrides.get("recorded", [_record("trino"), _record("dagster")])
        self.reached_while_down: list[str] = []

    def scale(self, n: int) -> None:
        self.dex = n

    def wait(self, up: bool) -> None:
        self.waited_up = self.waited_up or (up and self.dex > 0)

    def reach(self, service: str) -> int | None:
        if self.dex == 0:
            self.reached_while_down.append(service)
        return self.statuses[service]

    def run(self, ticks: Any = None) -> drill.Timings:
        extra: dict[str, Any] = {"clock": lambda: float(next(ticks))} if ticks else {}
        return drill.run_drill(
            scale_dex=self.scale,
            wait_dex=self.wait,
            signed_in=lambda: self.sign_ins.pop(0),
            refused=lambda: self.refused,
            reach=self.reach,
            records=lambda: self.recorded,
            replicas=2,
            reason=REASON,
            pause=lambda _s: None,
            **extra,
        )


def test_the_drill_reaches_both_services_during_the_outage_and_restores_dex() -> None:
    cluster = Cluster()
    cluster.run()
    assert cluster.reached_while_down == ["trino", "dagster"]
    assert cluster.dex == 2
    assert cluster.waited_up


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"refused": False}, "was not refused"),
        ({"statuses": {"trino": 200, "dagster": 503}}, "dagster did not answer"),
        ({"statuses": {"trino": None, "dagster": 200}}, "trino did not answer"),
        ({"recorded": [_record("trino")]}, "No complete break-glass record for dagster"),
        ({"recorded": [_record("trino"), _record("dagster", ended="")]}, "No complete break-glass record for dagster"),
        # An older drill's complete record must not vouch for this run.
        ({"recorded": [_record("trino"), {**_record("dagster"), "reason": "e2e issuer-outage drill old"}]}, "dagster"),
    ],
)
def test_a_failed_step_still_restores_dex(overrides: dict[str, Any], message: str) -> None:
    cluster = Cluster(**overrides)
    with pytest.raises(E2EError, match=message):
        cluster.run()
    assert cluster.dex == 2
    assert cluster.waited_up


def test_sign_in_must_work_again_after_the_restore() -> None:
    with pytest.raises(E2EError, match="did not work again"):
        Cluster(sign_ins=[False] * drill.RESTORE_ATTEMPTS).run()


def test_timings_are_measured_from_the_outage_and_from_the_restore() -> None:
    timings = Cluster(sign_ins=[False, True]).run(ticks=iter(range(0, 100, 5)))
    assert (timings.outage_to_break_glass_seconds, timings.restore_to_sign_in_seconds) == (5.0, 5.0)
