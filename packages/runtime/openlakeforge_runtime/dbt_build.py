from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import AbstractContextManager
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
    nodes = manifest.get("nodes", {})

    def selected_upstream(unique_id: str) -> set[str]:
        """The selected models `unique_id` reads, looking through any non-selected node (ephemeral or not)."""
        found: set[str] = set()
        seen: set[str] = set()
        pending = list(nodes[unique_id]["depends_on"]["nodes"])
        while pending:
            parent = pending.pop()
            if parent in seen:
                continue
            seen.add(parent)
            if parent in models:
                found.add(models[parent]["name"])
            elif parent in nodes:
                pending.extend(nodes[parent].get("depends_on", {}).get("nodes", []))
        return found

    return {node["name"]: tuple(sorted(selected_upstream(unique_id))) for unique_id, node in models.items()}


def build_by_generation(
    dependencies: Mapping[str, Collection[str]],
    invoke: Callable[[list[str]], Iterator[Any]],
    token: Callable[[], AbstractContextManager[Any]],
    failed_models: Callable[[list[str]], Collection[str]],
) -> Iterator[Any]:
    """Build the models one topological generation per dbt invocation, and yield the events.

    The Polaris token a dbt process sends to Trino is read from its environment once, when the profile
    renders, and Trino hands the same token to Polaris at every commit. A single `dbt build` therefore
    fails at the first commit after the token's lifetime. A generation holds the models whose upstream
    models all sit in earlier generations, so each invocation keeps dbt's `threads` parallelism and pays
    dbt's start-up once per generation, not once per model. `token` opens a freshly minted token around
    each invocation; the limit left is one generation within the token's lifetime.

    `invoke` receives the models to build and runs a `dbt build` of them. When it fails, `failed_models`
    names the generation's models that failed (none known means all of them). Their dependents are
    skipped, as `dbt build` does; independent models still build, and the first failure is raised once
    the rest are done.
    """
    if not dependencies:
        # A selection of checks alone names no model, and dbt still has to run them.
        with token():
            yield from invoke([])
        return
    failed: set[str] = set()
    first_error: DagsterDbtCliRuntimeError | None = None
    sorter = TopologicalSorter({model: set(parents) for model, parents in dependencies.items()})
    sorter.prepare()
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        batch = [model for model in ready if not failed.intersection(dependencies[model])]
        failed.update(set(ready) - set(batch))
        if batch:
            try:
                with token():
                    yield from invoke(batch)
            except DagsterDbtCliRuntimeError as exc:
                failed.update(set(failed_models(batch)) & set(batch) or batch)
                first_error = first_error or exc
        sorter.done(*ready)
    if first_error is not None:
        raise first_error


def failed_models_in(target_path: Path, generation: Collection[str]) -> set[str]:
    """The models of `generation` that `target_path`'s `run_results.json` reports as errored or skipped."""
    path = target_path / "run_results.json"
    if not path.exists():
        return set()
    return {
        result["unique_id"].rsplit(".", 1)[-1]
        for result in json.loads(path.read_text())["results"]
        if result["unique_id"].startswith("model.")
        and result["status"] in ("error", "skipped")
        and result["unique_id"].rsplit(".", 1)[-1] in generation
    }


def merge_run_results(target_paths: list[Path]) -> Path:
    """Fold every invocation's `run_results.json` into the last one's, and return that directory.

    Each invocation writes its own file, and the last alone would report one generation as the whole build.
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
