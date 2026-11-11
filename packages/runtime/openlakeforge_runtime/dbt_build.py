from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterator, Mapping
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any

from dagster_dbt.errors import DagsterDbtCliRuntimeError


def model_dependencies(manifest: Mapping[str, Any], selected: Collection[str]) -> dict[str, tuple[str, ...]]:
    """Each selected model of the manifest's project, with the selected models it reads from."""
    project = manifest.get("metadata", {}).get("project_name")
    models = {
        unique_id: node
        for unique_id, node in manifest.get("nodes", {}).items()
        if node.get("resource_type") == "model" and node.get("package_name") == project and node["name"] in selected
    }
    names = {unique_id: node["name"] for unique_id, node in models.items()}
    return {
        node["name"]: tuple(sorted(names[parent] for parent in node["depends_on"]["nodes"] if parent in names))
        for node in models.values()
    }


def build_model_by_model(
    dependencies: Mapping[str, Collection[str]], invoke: Callable[[list[str]], Iterator[Any]]
) -> Iterator[Any]:
    """Build every model in its own dbt invocation, upstream first, and yield the events.

    The Polaris token a dbt process sends to Trino is read from its environment once, when the profile
    renders, and Trino hands the same token to Polaris at every commit. A single `dbt build` therefore
    fails at the first commit after the token's lifetime. One process per model bounds the exposure to a
    single model's build instead of the whole product's, at the price of one dbt start-up per model.

    `invoke` receives the models to leave out (every other one) and runs a fresh-token `dbt build`. A model
    whose build fails, or whose upstream did, stops its dependents, as `dbt build` does; independent models
    still build, and the first failure is raised once the rest are done.
    """
    failed: set[str] = set()
    first_error: DagsterDbtCliRuntimeError | None = None
    order = TopologicalSorter({model: set(parents) for model, parents in dependencies.items()}).static_order()
    for name in order:
        if failed.intersection(dependencies[name]):
            failed.add(name)
            continue
        try:
            yield from invoke(sorted(set(dependencies) - {name}))
        except DagsterDbtCliRuntimeError as exc:
            failed.add(name)
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def merge_run_results(target_paths: list[Path]) -> Path:
    """Fold every invocation's `run_results.json` into the last one's, and return that directory.

    Each invocation writes its own file, and the last alone would report one model as the whole build.
    """
    last = target_paths[-1]
    path = last / "run_results.json"
    if len(target_paths) > 1 and path.exists():
        merged = json.loads(path.read_text())
        merged["results"] = [
            result
            for target in target_paths
            if (target / "run_results.json").exists()
            for result in json.loads((target / "run_results.json").read_text())["results"]
        ]
        path.write_text(json.dumps(merged))
    return last
