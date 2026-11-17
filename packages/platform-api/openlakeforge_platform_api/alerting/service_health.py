"""A stage's service health, read from the platform API's services route (ADR 0023, decision 11).

The evaluator runs no probe of its own: one place observes the services, and the evaluator reads what it saw. The call
carries the pod's projected ServiceAccount token, which the API checks with a TokenReview (ADR 0021, decision 3). Any
failure to get a clean answer is `HealthReadError`: the caller must not read it as every service being healthy.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from openlakeforge_core.health import HealthState

from openlakeforge_platform_api.service_probes import PROBE_TIMEOUT

# Above PROBE_TIMEOUT, the most the services route takes, so a slow probe is an answer and not a failed read.
READ_TIMEOUT = PROBE_TIMEOUT.total_seconds() + 5.0


class HealthReadError(Exception):
    """The API did not give a usable answer: a timeout, a 5xx, a refused token or a document of the wrong shape."""


@dataclass(frozen=True)
class ServiceState:
    component: str
    state: HealthState
    reason: str


# One read per stage; raises HealthReadError.
StageHealthReader = Callable[[str], list[ServiceState]]


def stage_health_reader(
    base_url: str,
    token_file: str,
    *,
    timeout: float = READ_TIMEOUT,
    get: Callable[..., Any] = requests.get,
) -> StageHealthReader:
    def read(stage: str) -> list[ServiceState]:
        try:
            # Read per call: the kubelet rotates the projected token.
            token = Path(token_file).read_text(encoding="utf-8").strip()
            response = get(
                f"{base_url.rstrip('/')}/api/v1/stages/{quote(stage, safe='')}/health/services",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=timeout,
            )
        except (OSError, requests.RequestException) as exc:
            # The type only: a transport error's text can carry the URL, and the token never appears in a log.
            raise HealthReadError(f"the platform API could not be read: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise HealthReadError(f"the platform API answered HTTP {response.status_code}")
        try:
            return _services(stage, response.json())
        except ValueError as exc:
            raise HealthReadError(f"the platform API's answer for stage {stage} is unusable: {exc}") from exc

    return read


def _services(stage: str, document: Any) -> list[ServiceState]:
    if (
        not isinstance(document, Mapping)
        or document.get("stage") != stage
        or not isinstance(document.get("services"), list)
    ):
        raise ValueError("not a stage health document for this stage")
    services = []
    for item in document["services"]:
        if not isinstance(item, Mapping) or not isinstance(item.get("component"), str):
            raise ValueError("a service entry without a component")
        services.append(
            ServiceState(item["component"], HealthState(str(item.get("state"))), str(item.get("reason", "")))
        )
    return services
