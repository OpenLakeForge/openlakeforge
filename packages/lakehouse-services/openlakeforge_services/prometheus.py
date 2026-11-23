"""A client for the Prometheus HTTP API, `prometheus-http-v1` in the provider contract (ADR 0024, D10).

Only the two reads the contract names: `GET /api/v1/query` and `GET /api/v1/query_range`. The caller builds every
query from values it validated; this module sends what it is given and parses the answer. Any backend that speaks the
same API (Thanos, Mimir, VictoriaMetrics) is read the same way.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import requests

from .base import RATE_LIMIT_RETRIES, JsonHttpClient, ServiceClientError, TransientServiceError

# The platform API's bound on every Prometheus call, as on its service probes: a chart that waits longer than this is
# worse than one that says `unavailable`.
PROMETHEUS_TIMEOUT_SECONDS = 5.0


class PrometheusError(ServiceClientError):
    """Prometheus answered, but not with a result: a refused query, or a body that is not the HTTP API's."""


@dataclass(frozen=True)
class PrometheusSeries:
    labels: Mapping[str, str]
    # (Unix seconds, value) pairs, oldest first. A value Prometheus reports as NaN or infinite is None: it cannot be
    # carried as a JSON number, and drawn as a number it would mislead.
    samples: list[tuple[float, float | None]]


def _value(raw: Any) -> float | None:
    number = float(raw)
    return number if math.isfinite(number) else None


def _labels(item: Any) -> dict[str, str]:
    metric = item.get("metric") if isinstance(item, Mapping) else None
    if not isinstance(metric, Mapping):
        raise PrometheusError(f"malformed result: a series without a metric mapping: {item!r}")
    return {str(k): str(v) for k, v in metric.items()}


def _series(data: Any, result_type: str) -> list[PrometheusSeries]:
    if not isinstance(data, Mapping) or data.get("resultType") != result_type:
        kind = data.get("resultType") if isinstance(data, Mapping) else None
        raise PrometheusError(f"expected a {result_type} result, got {kind!r}")
    result = data.get("result")
    if not isinstance(result, list):
        raise PrometheusError(f"malformed {result_type} result: result is {type(result).__name__}, not a list")
    try:
        if result_type == "matrix":
            return [
                PrometheusSeries(_labels(item), [(float(time), _value(value)) for time, value in item["values"]])
                for item in result
            ]
        return [
            PrometheusSeries(_labels(item), [(float(item["value"][0]), _value(item["value"][1]))]) for item in result
        ]
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise PrometheusError(f"malformed {result_type} result: {exc!r}") from exc


class PrometheusClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = PROMETHEUS_TIMEOUT_SECONDS,
        rate_limit_retries: int = RATE_LIMIT_RETRIES,
        http: JsonHttpClient | None = None,
    ) -> None:
        # 0 makes one attempt, for a caller that holds a deadline of its own.
        self._http = http or JsonHttpClient(
            base_url.rstrip("/"), timeout=timeout, rate_limit_retries=rate_limit_retries
        )

    def _get(self, path: str, params: Mapping[str, Any]) -> Any:
        # A refused query is a 400 or 422 whose body still carries the API's error; the base client raises on it with
        # that body in the message, which is the reason a caller logs.
        try:
            body: Any = self._http.request("GET", path, params=params)
        except requests.RequestException as exc:
            # The shared client classifies connection errors and timeouts; a body cut off mid-read
            # (ChunkedEncodingError) or any other transport failure is the same outage to a caller.
            raise TransientServiceError(f"{path} failed: {exc!r}") from exc
        # A proxy or a misconfigured backend can answer 200 with JSON that is not the API's envelope at all.
        if not isinstance(body, Mapping):
            raise PrometheusError(f"{path} answered {type(body).__name__}, not the HTTP API's envelope")
        if body.get("status") != "success":
            raise PrometheusError(f"{path} answered {body.get('errorType')!r}: {body.get('error')!r}")
        return body.get("data")

    def query(self, query: str, *, time: float) -> list[PrometheusSeries]:
        """An instant query at `time`: one sample per series. Raises ServiceClientError on any failure."""
        return _series(self._get("/api/v1/query", {"query": query, "time": time}), "vector")

    def query_range(self, query: str, *, start: float, end: float, step: float) -> list[PrometheusSeries]:
        """A range query, evaluated at `start` and every `step` seconds up to `end`; a step with no sample is absent.

        Raises ServiceClientError, or its TransientServiceError subclass when Prometheus is unreachable or failing.
        """
        params = {"query": query, "start": start, "end": end, "step": step}
        return _series(self._get("/api/v1/query_range", params), "matrix")
