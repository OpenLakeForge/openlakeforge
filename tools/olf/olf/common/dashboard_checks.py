"""Parsed Superset dashboard asset validation for ``olf check structure``."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

_ALLOWED_VIZ_TYPES = {"echarts_timeseries_bar", "echarts_timeseries_line", "pie", "table"}


def validate_superset_assets(root: Path) -> list[str]:
    """Return relation errors between exported Superset datasets and charts."""
    dashboards = root / "lakehouse_code/dashboards/superset"
    datasets: dict[str, tuple[Path, set[str], set[str]]] = {}
    errors: list[str] = []
    for path in sorted(dashboards.glob("*/datasets/*/*.yaml")):
        data = _mapping(path, errors)
        uuid = data.get("uuid")
        columns = _names(data.get("columns"), "column_name")
        metrics = _names(data.get("metrics"), "metric_name")
        if not isinstance(uuid, str) or not uuid:
            errors.append(f"{path}: missing uuid")
            continue
        if uuid in datasets:
            errors.append(f"{path}: duplicate dataset uuid {uuid}")
            continue
        main_dttm_col = data.get("main_dttm_col")
        if main_dttm_col is not None and main_dttm_col not in columns:
            errors.append(f"{path}: main_dttm_col {main_dttm_col} is not declared as a column")
        datasets[uuid] = (path, columns, metrics)
    for path in sorted(dashboards.glob("*/charts/*.yaml")):
        chart = _mapping(path, errors)
        viz_type = chart.get("viz_type")
        dataset_uuid = chart.get("dataset_uuid")
        if viz_type not in _ALLOWED_VIZ_TYPES:
            errors.append(f"{path}: unsupported Superset viz_type {viz_type}")
        if not isinstance(dataset_uuid, str) or dataset_uuid not in datasets:
            errors.append(f"{path}: unknown dataset_uuid {dataset_uuid}")
            continue
        dataset_path, columns, metrics = datasets[dataset_uuid]
        params = chart.get("params")
        if not isinstance(params, dict):
            errors.append(f"{path}: missing chart params mapping")
            continue
        for field in _fields(params):
            if field not in columns:
                errors.append(f"{path}: field {field} is not declared in {dataset_path}")
        for metric in _metrics(params):
            if metric not in metrics:
                errors.append(f"{path}: metric {metric} is not declared in {dataset_path}")
    return errors


def _mapping(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        errors.append(f"{path}: invalid YAML: {exc}")
        return {}
    if not isinstance(data, dict):
        errors.append(f"{path}: YAML document must be a mapping")
        return {}
    return data


def _names(items: object, key: str) -> set[str]:
    if not isinstance(items, list):
        return set()
    return {item[key] for item in items if isinstance(item, dict) and isinstance(item.get(key), str)}


def _fields(params: dict[str, Any]) -> Iterable[str]:
    for key in ("x_axis",):
        value = params.get(key)
        if isinstance(value, str):
            yield value
    for key in ("groupby", "columns"):
        values = params.get(key, [])
        if isinstance(values, list):
            yield from (value for value in values if isinstance(value, str))
    yield from _subjects(params)


def _subjects(value: object) -> Iterable[str]:
    if isinstance(value, dict):
        subject = value.get("subject")
        if isinstance(subject, str):
            yield subject
        for child in value.values():
            yield from _subjects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _subjects(child)


def _metrics(params: dict[str, Any]) -> Iterable[str]:
    values = params.get("metrics", [])
    if isinstance(values, list):
        yield from (value for value in values if isinstance(value, str))
    metric = params.get("metric")
    if isinstance(metric, str):
        yield metric
