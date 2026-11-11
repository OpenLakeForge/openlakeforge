import json
from pathlib import Path

import pytest

pytest.importorskip("dagster_dbt")

from dagster_dbt.errors import DagsterDbtCliRuntimeError  # noqa: E402
from openlakeforge_runtime import dbt_build  # noqa: E402

MANIFEST = {
    "metadata": {"project_name": "gold"},
    "nodes": {
        "model.gold.a": {"name": "a", "resource_type": "model", "package_name": "gold", "depends_on": {"nodes": []}},
        "model.gold.b": {
            "name": "b",
            "resource_type": "model",
            "package_name": "gold",
            "depends_on": {"nodes": ["model.gold.a", "source.gold.s"]},
        },
        "model.gold.c": {"name": "c", "resource_type": "model", "package_name": "gold", "depends_on": {"nodes": []}},
        "model.pkg.shared": {
            "name": "shared",
            "resource_type": "model",
            "package_name": "pkg",
            "depends_on": {"nodes": []},
        },
        "test.gold.t": {"name": "t", "resource_type": "test", "package_name": "gold", "depends_on": {"nodes": []}},
    },
}


def test_dependencies_cover_the_selected_models_of_the_project_only() -> None:
    assert dbt_build.model_dependencies(MANIFEST, {"a", "b", "c", "shared", "t"}) == {
        "a": (),
        "b": ("a",),
        "c": (),
    }
    assert dbt_build.model_dependencies(MANIFEST, {"b"}) == {"b": ()}


def test_each_model_builds_alone_and_after_its_upstream() -> None:
    calls: list[list[str]] = []

    def invoke(exclude: list[str]):
        calls.append(exclude)
        yield f"built-without-{'-'.join(exclude)}"

    events = list(dbt_build.build_model_by_model({"b": ("a",), "a": (), "c": ()}, invoke))

    assert len(events) == 3
    kept = [({"a", "b", "c"} - set(exclude)).pop() for exclude in calls]
    assert kept.index("a") < kept.index("b")
    assert sorted(kept) == ["a", "b", "c"]


def test_a_failure_skips_dependents_but_not_independent_models_and_is_raised_last() -> None:
    kept: list[str] = []

    def invoke(exclude: list[str]):
        name = ({"a", "b", "c", "d"} - set(exclude)).pop()
        kept.append(name)
        if name == "a":
            raise DagsterDbtCliRuntimeError(description="boom")
        yield name

    stream = dbt_build.build_model_by_model({"a": (), "b": ("a",), "c": (), "d": ("b",)}, invoke)
    built = []
    with pytest.raises(DagsterDbtCliRuntimeError):
        for event in stream:
            built.append(event)

    assert built == ["c"]
    assert sorted(kept) == ["a", "c"]


def test_run_results_of_every_invocation_end_up_in_the_last(tmp_path: Path) -> None:
    targets = []
    for index, model in enumerate(["a", "b"]):
        target = tmp_path / str(index)
        target.mkdir()
        (target / "run_results.json").write_text(json.dumps({"metadata": {"n": index}, "results": [{"id": model}]}))
        targets.append(target)

    assert dbt_build.merge_run_results(targets) == targets[-1]

    merged = json.loads((targets[-1] / "run_results.json").read_text())
    assert [result["id"] for result in merged["results"]] == ["a", "b"]
    assert merged["metadata"] == {"n": 1}
