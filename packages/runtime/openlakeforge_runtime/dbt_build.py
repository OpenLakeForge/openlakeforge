from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any

from dagster_dbt.errors import DagsterDbtCliRuntimeError


@dataclass(frozen=True)
class DbtTest:
    """A dbt test (data, singular or unit) and the selected models it reads."""

    unique_id: str
    selector: str
    parents: tuple[str, ...]
    # False for a test that reaches no selected model: it does not run in this build.
    explicit: bool
    # The sources the test reads directly (unique ids): a failed source test blocks the models reading them.
    sources: tuple[str, ...] = ()
    # The test node's dbt name, which dagster-dbt uses as its asset check's name.
    name: str = ""


def _project_models(manifest: Mapping[str, Any], selected: Collection[str]) -> dict[str, Mapping[str, Any]]:
    project = manifest.get("metadata", {}).get("project_name")
    return {
        unique_id: node
        for unique_id, node in manifest.get("nodes", {}).items()
        if node.get("resource_type") == "model" and node.get("package_name") == project and node["name"] in selected
    }


def _is_ephemeral(node: Mapping[str, Any]) -> bool:
    return node.get("config", {}).get("materialized") == "ephemeral"


def _selected_upstream(
    manifest: Mapping[str, Any],
    models: Mapping[str, Mapping[str, Any]],
    parents: Collection[str],
    *,
    ephemeral_only: bool = False,
) -> set[str]:
    """The selected models among `parents`, looking through non-selected nodes (only ephemeral ones if asked)."""
    nodes = manifest.get("nodes", {})
    found: set[str] = set()
    seen: set[str] = set()
    pending = list(parents)
    while pending:
        parent = pending.pop()
        if parent in seen:
            continue
        seen.add(parent)
        if parent in models:
            found.add(models[parent]["name"])
        elif parent in nodes and (not ephemeral_only or _is_ephemeral(nodes[parent])):
            pending.extend(nodes[parent].get("depends_on", {}).get("nodes", []))
    return found


def model_dependencies(manifest: Mapping[str, Any], selected: Collection[str]) -> dict[str, tuple[str, ...]]:
    """Each selected model of the manifest's project, with the selected models it reads from."""
    models = _project_models(manifest, selected)
    return {
        node["name"]: tuple(sorted(_selected_upstream(manifest, models, node["depends_on"]["nodes"])))
        for node in models.values()
    }


def dbt_tests(manifest: Mapping[str, Any], selected: Collection[str]) -> dict[str, DbtTest]:
    """The project's tests by unique id, each with the selected models it reads (unit tests included)."""
    models = _project_models(manifest, selected)
    project = manifest.get("metadata", {}).get("project_name")
    candidates = [*manifest.get("nodes", {}).values(), *manifest.get("unit_tests", {}).values()]
    tests: dict[str, DbtTest] = {}
    for node in candidates:
        if node.get("resource_type") not in ("test", "unit_test") or node.get("package_name") != project:
            continue
        direct = node.get("depends_on", {}).get("nodes", [])
        # Eager selection runs a test when any of its parents is selected, and only those decide its generation.
        # A materialized model outside the selection is not looked through: its test is not this run's just
        # because that model reads a selected one. An ephemeral model, which has no table, is.
        parents = tuple(sorted(_selected_upstream(manifest, models, direct, ephemeral_only=True)))
        tests[node["unique_id"]] = DbtTest(
            unique_id=node["unique_id"],
            selector=".".join(node["fqn"]),
            parents=parents,
            # Only a test that reaches a selected model runs, as under eager selection of the models: a test on
            # sources alone never did, since the product job selects models.
            explicit=bool(parents),
            sources=tuple(sorted(parent for parent in direct if parent.startswith("source."))),
            name=node.get("name", ""),
        )
    return tests


def without_deselected_checks(
    tests: Mapping[str, DbtTest], known: Collection[str], selected: Collection[str]
) -> dict[str, DbtTest]:
    """`tests` less those Dagster models as asset checks (`known`, by name) that the run did not select.

    Each generation selects its tests explicitly, so without this a check deselected in Dagster would still run.
    A test Dagster does not model as a check is kept.
    """
    deselected = set(known) - set(selected)
    return {unique_id: test for unique_id, test in tests.items() if test.name not in deselected}


def model_sources(manifest: Mapping[str, Any], selected: Collection[str]) -> dict[str, frozenset[str]]:
    """Each selected model with every source it reads, directly or through any upstream node."""
    nodes = manifest.get("nodes", {})
    result: dict[str, frozenset[str]] = {}
    for node in _project_models(manifest, selected).values():
        found: set[str] = set()
        seen: set[str] = set()
        pending = list(node.get("depends_on", {}).get("nodes", []))
        while pending:
            parent = pending.pop()
            if parent in seen:
                continue
            seen.add(parent)
            if parent.startswith("source."):
                found.add(parent)
            elif parent in nodes:
                pending.extend(nodes[parent].get("depends_on", {}).get("nodes", []))
        result[node["name"]] = frozenset(found)
    return result


def generations(dependencies: Mapping[str, Collection[str]]) -> list[list[str]]:
    """The models grouped by topological generation: each holds the models whose parents are all earlier."""
    sorter = TopologicalSorter({model: set(parents) for model, parents in dependencies.items()})
    sorter.prepare()
    result: list[list[str]] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        result.append(ready)
        sorter.done(*ready)
    return result


def tests_by_generation(
    dependencies: Mapping[str, Collection[str]], tests: Mapping[str, DbtTest]
) -> list[list[DbtTest]]:
    """Each test in the generation of its latest parent model: it runs once, after all its parents built.

    `dbt build` drops a test as soon as any parent is excluded, so a test over models of different
    generations would run in none of the invocations. A test that reads no selected model goes first.
    """
    groups = generations(dependencies)
    index = {model: number for number, group in enumerate(groups) for model in group}
    assigned: list[list[DbtTest]] = [[] for _ in groups]
    if assigned:
        for test in sorted(tests.values(), key=lambda t: t.unique_id):
            assigned[max((index[parent] for parent in test.parents), default=0)].append(test)
    return assigned


def generation_args(
    models: Collection[str],
    run: Collection[DbtTest],
    all_models: Collection[str],
    all_tests: Collection[DbtTest],
) -> list[str]:
    """The dbt arguments of one generation: its models and the tests assigned to it, and nothing else.

    Indirect selection is off. At dbt's default (eager), excluding a model also excludes every test that
    reads it, and selecting one pulls in the tests of other generations. Without it, `--select` and
    `--exclude` act on exactly the nodes they name.
    """
    if not all_models:
        return []
    running = {test.unique_id for test in run}
    select = [*sorted(models), *(test.selector for test in run if test.explicit)]
    exclude = [
        *sorted(set(all_models) - set(models)),
        *(test.selector for test in all_tests if test.unique_id not in running),
    ]
    args = ["--indirect-selection", "empty", "--select", " ".join(select)]
    return [*args, "--exclude", " ".join(exclude)] if exclude else args


def build_by_generation(
    dependencies: Mapping[str, Collection[str]],
    tests: Mapping[str, DbtTest],
    invoke: Callable[[list[str], list[DbtTest]], Iterator[Any]],
    token: Callable[[], AbstractContextManager[Any]],
    failed_nodes: Callable[[list[str], list[DbtTest]], Collection[str]],
    sources: Mapping[str, Collection[str]] | None = None,
    between: Callable[[], None] | None = None,
) -> Iterator[Any]:
    """Build the models one topological generation per dbt invocation, and yield the events.

    The Polaris token a dbt process sends to Trino is read from its environment once, when the profile
    renders, and Trino hands the same token to Polaris at every commit. A single `dbt build` therefore
    fails at the first commit after the token's lifetime. A generation holds the models whose upstream
    models all sit in earlier generations, so each invocation keeps dbt's `threads` parallelism and pays
    dbt's start-up once per generation, not once per model. `token` opens a freshly minted token around
    each invocation; the limit left is one generation within the token's lifetime.

    Each test runs once, in the generation of its latest parent model (see `tests_by_generation`).
    `invoke` receives the models and tests to build and runs a `dbt build` of them. When it fails,
    `failed_nodes` names the models (by name) and tests (by unique id) that errored or failed; none
    known means all of the generation's models. Dependents are skipped, as `dbt build` does: those of a
    failed model, and those of a failed blocking test (the models that read all the test's parents). A
    failed test on sources blocks the models that read all its sources, given by `sources` (model name
    to the source ids it reads, directly or not). A test over a failed or skipped model does not run.
    Independent models still build, and the first failure is raised once the rest are done.

    `between`, when given, runs before each generation after the first and before its token is minted.
    The e2e uses it to outlast a shortened token lifetime deterministically (#1061).
    """
    if not dependencies:
        # A selection of checks alone names no model, and dbt still has to run them.
        with token():
            yield from invoke([], [])
        return
    failed: set[str] = set()
    blocked: set[str] = set()
    first_error: DagsterDbtCliRuntimeError | None = None
    children: dict[str, set[str]] = {model: set() for model in dependencies}
    for model, parents in dependencies.items():
        for parent in parents:
            children.setdefault(parent, set()).add(model)

    def descendants(model: str) -> set[str]:
        found: set[str] = set()
        pending = [model]
        while pending:
            for child in children.get(pending.pop(), ()):
                if child not in found:
                    found.add(child)
                    pending.append(child)
        return found

    started = False
    for ready, assigned in zip(generations(dependencies), tests_by_generation(dependencies, tests), strict=True):
        batch = [model for model in ready if model not in blocked and not failed.intersection(dependencies[model])]
        failed.update(set(ready) - set(batch))
        if not batch:
            continue
        run = [test for test in assigned if not failed.intersection(test.parents)]
        if started and between is not None:
            between()
        started = True
        try:
            with token():
                yield from invoke(batch, run)
        except DagsterDbtCliRuntimeError as exc:
            names = set(failed_nodes(batch, run))
            failed_tests = [test for test in run if test.unique_id in names]
            failed_models = names & set(batch)
            if not failed_models and not failed_tests:
                failed_models = set(batch)
            failed.update(failed_models)
            for test in failed_tests:
                if len(test.parents) == 1:
                    failed.add(test.parents[0])
                elif test.parents:
                    # dbt makes a model wait for a test only when it reads all of the test's parents.
                    blocked.update(set.intersection(*(descendants(parent) for parent in test.parents)))
                elif test.sources and sources is not None:
                    # A source test: block every model reading all its sources, and what reads those.
                    readers = {model for model, read in sources.items() if set(test.sources) <= set(read)}
                    blocked.update(readers)
                    for reader in readers:
                        blocked.update(descendants(reader))
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def failed_nodes_in(target_path: Path, models: Collection[str], tests: Collection[DbtTest]) -> set[str]:
    """What `target_path`'s `run_results.json` reports as failed among `models` and `tests`.

    A model that errored or was skipped is named by its name; a test whose status is `fail` or `error`,
    by its unique id. A test of severity `warn` reports `warn` and blocks nothing.
    """
    path = target_path / "run_results.json"
    if not path.exists():
        return set()
    test_ids = {test.unique_id for test in tests}
    failed: set[str] = set()
    for result in json.loads(path.read_text())["results"]:
        unique_id = result["unique_id"]
        if unique_id.startswith("model."):
            name = unique_id.rsplit(".", 1)[-1]
            if result["status"] in ("error", "skipped") and name in models:
                failed.add(name)
        elif unique_id in test_ids and result["status"] in ("fail", "error"):
            failed.add(unique_id)
    return failed


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
