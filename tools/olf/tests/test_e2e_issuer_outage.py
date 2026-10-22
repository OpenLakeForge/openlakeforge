from __future__ import annotations

from typing import Any

import pytest

from olf.e2e import _issuer_outage as drill
from olf.e2e._shell import E2EError


def _record(service: str, **extra: str) -> dict[str, str]:
    return {"service": service, "reason": drill.REASON, "user": "op", "ended": "t", "duration_seconds": "3", **extra}


class Fakes:
    def __init__(self, **overrides: Any) -> None:
        self.events: list[str] = []
        self.sign_ins = overrides.get("sign_ins", [False, True])
        self.statuses = overrides.get("statuses", {"trino": 200, "dagster": 200})
        self.recorded = overrides.get("recorded", [_record("trino"), _record("dagster")])

    def run(self, replicas: int = 2) -> drill.Timings:
        def signed_in() -> bool:
            self.events.append("sign-in")
            return self.sign_ins.pop(0)

        def reach(service: str) -> int | None:
            self.events.append(f"reach {service}")
            return self.statuses[service]

        return drill.run_drill(
            scale_dex=lambda n: self.events.append(f"scale {n}"),
            wait_dex=lambda up: self.events.append("wait up" if up else "wait down"),
            signed_in=signed_in,
            reach=reach,
            records=lambda: self.recorded,
            replicas=replicas,
            pause=lambda _s: None,
        )


def test_the_drill_runs_in_order_and_restores_the_original_replicas() -> None:
    fakes = Fakes()
    fakes.run(replicas=2)
    assert fakes.events == [
        "scale 0", "wait down", "sign-in", "reach trino", "reach dagster", "scale 2", "wait up", "sign-in",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"sign_ins": [True]}, "succeeded with Dex scaled to zero"),
        ({"sign_ins": [False], "statuses": {"trino": 200, "dagster": 503}}, "dagster did not answer"),
        ({"sign_ins": [False], "statuses": {"trino": None, "dagster": 200}}, "trino did not answer"),
        ({"sign_ins": [False], "recorded": [_record("trino")]}, "No complete break-glass record for dagster"),
        (
            {"sign_ins": [False], "recorded": [_record("trino"), {**_record("dagster"), "ended": ""}]},
            "No complete break-glass record for dagster",
        ),
    ],
)
def test_a_failed_step_still_restores_dex(overrides: dict[str, Any], message: str) -> None:
    fakes = Fakes(**overrides)
    with pytest.raises(E2EError, match=message):
        fakes.run(replicas=2)
    assert fakes.events[-1] == "scale 2"


def test_sign_in_must_work_again_after_the_restore() -> None:
    with pytest.raises(E2EError, match="did not work again"):
        Fakes(sign_ins=[False] + [False] * drill.RESTORE_ATTEMPTS).run()


def test_timings_are_measured_from_the_outage_and_from_the_restore() -> None:
    ticks = iter(range(0, 100, 5))
    fakes = Fakes(sign_ins=[False, False, True])
    timings = drill.run_drill(
        scale_dex=lambda _n: None,
        wait_dex=lambda _up: None,
        signed_in=lambda: fakes.sign_ins.pop(0),
        reach=lambda _s: 200,
        records=lambda: fakes.recorded,
        replicas=1,
        clock=lambda: float(next(ticks)),
        pause=lambda _s: None,
    )
    assert (timings.outage_to_break_glass_seconds, timings.restore_to_sign_in_seconds) == (5.0, 5.0)
