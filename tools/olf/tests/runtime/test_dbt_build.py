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

# a feeds b and c, which both feed d; e stands alone.
DIAMOND = {"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c"), "e": ()}


class Tokens:
    """Stands in for `polaris_token_env`: counts the tokens minted and tracks whether one is open."""

    def __init__(self) -> None:
        self.minted = 0
        self.open = False

    def __call__(self):
        tokens = self

        class Scope:
            def __enter__(self) -> None:
                tokens.minted += 1
                tokens.open = True

            def __exit__(self, *exc: object) -> None:
                tokens.open = False

        return Scope()


def run(dependencies, tokens, fail=(), failed_models=lambda models: []):
    batches: list[list[str]] = []

    def invoke(models: list[str]):
        assert tokens.open, "a generation must build under its own token"
        batches.append(models)
        if set(fail) & set(models):
            raise DagsterDbtCliRuntimeError(description="boom")
        yield from models

    events: list[str] = []
    error = None
    try:
        events.extend(dbt_build.build_by_generation(dependencies, invoke, tokens, failed_models))
    except DagsterDbtCliRuntimeError as exc:
        error = exc
    return batches, events, error


def test_dependencies_cover_the_selected_models_of_the_project_only() -> None:
    assert dbt_build.model_dependencies(MANIFEST, {"a", "b", "c", "shared", "t"}) == {
        "a": (),
        "b": ("a",),
        "c": (),
    }
    assert dbt_build.model_dependencies(MANIFEST, {"b"}) == {"b": ()}


def test_a_diamond_is_grouped_into_generations() -> None:
    batches, events, error = run(DIAMOND, Tokens())

    assert batches == [["a", "e"], ["b", "c"], ["d"]]
    assert events == ["a", "e", "b", "c", "d"]
    assert error is None


def test_exactly_one_token_is_minted_per_generation() -> None:
    tokens = Tokens()

    batches, _, _ = run(DIAMOND, tokens)

    assert tokens.minted == len(batches) == 3
    assert not tokens.open


def test_a_failure_in_the_first_generation_skips_only_its_dependents() -> None:
    tokens = Tokens()

    batches, _, error = run(DIAMOND, tokens, fail={"a"}, failed_models=lambda models: ["a"])

    # a failed: b, c and d are skipped. e is independent but shares generation 1 with a, so it ran there.
    assert batches == [["a", "e"]]
    assert isinstance(error, DagsterDbtCliRuntimeError)
    assert tokens.minted == 1


def test_independent_branches_still_build_after_a_failure_and_the_error_is_raised_last() -> None:
    dependencies = {"a": (), "b": ("a",), "c": (), "d": ("c",), "e": ("b",)}

    batches, events, error = run(dependencies, Tokens(), fail={"a"}, failed_models=lambda models: ["a"])

    # Generation 1 fails on a; d (via c) still builds, b and e (via a) are skipped; the error comes last.
    assert batches == [["a", "c"], ["d"]]
    assert events == ["d"]
    assert error is not None


def test_a_failure_naming_no_model_fails_the_whole_generation() -> None:
    batches, _, error = run(DIAMOND, Tokens(), fail={"e"}, failed_models=lambda models: [])

    assert batches == [["a", "e"]]
    assert error is not None


def test_failed_models_are_read_from_run_results(tmp_path: Path) -> None:
    (tmp_path / "run_results.json").write_text(
        json.dumps(
            {
                "results": [
                    {"unique_id": "model.gold.a", "status": "success"},
                    {"unique_id": "model.gold.b", "status": "error"},
                    {"unique_id": "test.gold.t", "status": "fail"},
                ]
            }
        )
    )

    assert dbt_build.failed_models_in(tmp_path, ["a", "b"]) == {"b"}
    assert dbt_build.failed_models_in(tmp_path / "missing", ["a", "b"]) == set()


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


def test_dependencies_look_through_models_that_are_not_selected() -> None:
    def model(name: str, parents: list[str]) -> dict:
        return {"name": name, "resource_type": "model", "package_name": "gold", "depends_on": {"nodes": parents}}

    manifest = {
        "metadata": {"project_name": "gold"},
        "nodes": {
            "model.gold.up": model("up", []),
            "model.gold.eph": model("eph", ["model.gold.up"]),
            "model.gold.eph2": model("eph2", ["model.gold.eph", "source.gold.s"]),
            "model.gold.down": model("down", ["model.gold.eph2"]),
        },
    }

    assert dbt_build.model_dependencies(manifest, {"up", "down"}) == {"up": (), "down": ("up",)}


def test_a_selection_of_checks_alone_still_runs_dbt_once_under_a_token() -> None:
    tokens = Tokens()

    batches, _, error = run({}, tokens)

    assert batches == [[]]
    assert tokens.minted == 1
    assert error is None


def test_the_minted_message_the_e2e_looks_for_is_the_one_the_runtime_logs() -> None:
    from openlakeforge_runtime import polaris_token

    from olf.infra.e2e import _token_lifetime

    assert _token_lifetime.TOKEN_MINTED_MESSAGE == polaris_token.TOKEN_MINTED_MESSAGE
