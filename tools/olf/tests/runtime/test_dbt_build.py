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


def check_of(name: str, *parents: str, explicit: bool = True) -> dbt_build.DbtTest:
    return dbt_build.DbtTest(f"test.gold.{name}", f"gold.{name}", tuple(sorted(parents)), explicit)


# The names of the tests each generation of the last `run` was given.
test_batches: list[list[str]] = []


def run(dependencies, tokens, fail=(), failed_models=lambda models: [], tests=(), failed_tests=(), sources=None):
    """Run the generations; `fail` names models whose invocation raises, `failed_tests` tests that fail."""
    batches: list[list[str]] = []
    test_batches.clear()

    def invoke(models: list[str], run_tests: list[dbt_build.DbtTest]):
        assert tokens.open, "a generation must build under its own token"
        batches.append(models)
        test_batches.append([test.unique_id.rsplit(".", 1)[-1] for test in run_tests])
        if set(fail) & set(models) or {t.unique_id for t in run_tests} & {f"test.gold.{n}" for n in failed_tests}:
            raise DagsterDbtCliRuntimeError(description="boom")
        yield from models

    def failed_nodes(models: list[str], run_tests: list[dbt_build.DbtTest]):
        return [
            *failed_models(models),
            *(test.unique_id for test in run_tests if test.unique_id.rsplit(".", 1)[-1] in failed_tests),
        ]

    events: list[str] = []
    error = None
    try:
        events.extend(
            dbt_build.build_by_generation(
                dependencies, {t.unique_id: t for t in tests}, invoke, tokens, failed_nodes, sources=sources
            )
        )
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

    t = dbt_build.DbtTest("test.gold.t", "gold.t", ("b",), True)
    assert dbt_build.failed_nodes_in(tmp_path, ["a", "b"], [t]) == {"b", "test.gold.t"}
    assert dbt_build.failed_nodes_in(tmp_path, ["a", "b"], []) == {"b"}
    assert dbt_build.failed_nodes_in(tmp_path / "missing", ["a", "b"], [t]) == set()


def test_a_warning_test_is_not_a_failure(tmp_path: Path) -> None:
    (tmp_path / "run_results.json").write_text(
        json.dumps({"results": [{"unique_id": "test.gold.t", "status": "warn"}]})
    )
    t = dbt_build.DbtTest("test.gold.t", "gold.t", ("b",), True)

    assert dbt_build.failed_nodes_in(tmp_path, ["b"], [t]) == set()


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


TWO_GENERATIONS = {"a": (), "b": ("a",), "c": ("b",), "z": ()}


def test_a_relationships_test_across_generations_runs_once_in_the_later_one() -> None:
    relationships = check_of("relationships_b_a", "a", "b")
    own = check_of("unique_a", "a")

    batches, _, error = run(TWO_GENERATIONS, Tokens(), tests=[relationships, own])

    assert batches == [["a", "z"], ["b"], ["c"]]
    assert test_batches == [["unique_a"], ["relationships_b_a"], []]
    assert error is None


def test_a_singular_test_over_two_generations_runs_once() -> None:
    singular = check_of("singular_ac", "a", "c")

    run(TWO_GENERATIONS, Tokens(), tests=[singular])

    assert test_batches == [[], [], ["singular_ac"]]


def test_a_test_that_reads_no_selected_model_runs_in_the_first_generation() -> None:
    run(TWO_GENERATIONS, Tokens(), tests=[check_of("source_not_null")])

    assert test_batches == [["source_not_null"], [], []]


def test_a_failing_error_test_skips_the_dependents_of_its_model() -> None:
    check = check_of("not_null_b", "b")
    dependencies = {"a": (), "b": ("a",), "c": ("b",), "d": ("a",)}

    batches, _, error = run(dependencies, Tokens(), tests=[check], failed_tests={"not_null_b"})

    # b built and its test failed: c is skipped, d (independent of b) still builds.
    assert batches == [["a"], ["b", "d"]]
    assert error is not None


def test_a_warning_test_does_not_block_the_dependents() -> None:
    check = check_of("warn_b", "b")
    dependencies = {"a": (), "b": ("a",), "c": ("b",)}

    # A warn-severity test makes dbt exit 0: nothing raises and nothing fails.
    batches, _, error = run(dependencies, Tokens(), tests=[check])

    assert batches == [["a"], ["b"], ["c"]]
    assert error is None


def test_a_failed_multi_parent_test_blocks_only_the_models_reading_all_its_parents() -> None:
    check = check_of("relationships_b_x", "b", "x")
    dependencies = {"a": (), "b": ("a",), "x": (), "both": ("b", "x"), "only_b": ("b",)}

    batches, _, error = run(dependencies, Tokens(), tests=[check], failed_tests={"relationships_b_x"})

    # `both` reads b and x and waits for the test; `only_b` does not.
    assert batches == [["a", "x"], ["b"], ["only_b"]]
    assert error is not None


def test_a_test_over_a_skipped_model_does_not_run() -> None:
    check = check_of("t_ac", "a", "c")
    dependencies = {"a": (), "b": ("a",), "c": ("b",)}

    batches, _, _ = run(dependencies, Tokens(), fail={"a"}, failed_models=lambda models: ["a"], tests=[check])

    assert batches == [["a"]]
    assert test_batches == [[]]


def test_the_generation_arguments_select_its_models_and_tests_and_exclude_the_rest() -> None:
    relationships = check_of("relationships_b_a", "a", "b")
    own = check_of("unique_a", "a")
    unselected = check_of("on_unselected", "ghost", explicit=False)
    tests = [relationships, own, unselected]

    first = dbt_build.generation_args(["a", "z"], [own], ["a", "b", "z"], tests)
    second = dbt_build.generation_args(["b"], [relationships], ["a", "b", "z"], tests)

    assert first == [
        "--indirect-selection",
        "empty",
        "--select",
        "a z gold.unique_a",
        "--exclude",
        "b gold.relationships_b_a gold.on_unselected",
    ]
    assert second[second.index("--select") + 1] == "b gold.relationships_b_a"
    assert dbt_build.generation_args([], [], [], tests) == []


def _node(resource_type: str, unique_id: str, fqn: list[str], parents: list[str]) -> dict:
    return {
        "unique_id": unique_id,
        "name": fqn[-1],
        "resource_type": resource_type,
        "package_name": "gold",
        "fqn": fqn,
        "depends_on": {"nodes": parents},
    }


def test_tests_of_the_manifest_carry_their_selected_parents() -> None:
    manifest = {
        "metadata": {"project_name": "gold"},
        "nodes": {
            "model.gold.a": _node("model", "model.gold.a", ["gold", "a"], []),
            "model.gold.b": _node("model", "model.gold.b", ["gold", "b"], ["model.gold.a"]),
            "test.gold.rel.1": _node("test", "test.gold.rel.1", ["gold", "rel"], ["model.gold.a", "model.gold.b"]),
            "test.gold.src.2": _node("test", "test.gold.src.2", ["gold", "src"], ["source.gold.s"]),
            "test.gold.other.3": _node("test", "test.gold.other.3", ["gold", "other"], ["model.gold.ghost"]),
        },
        "unit_tests": {
            "unit_test.gold.b.ut": _node("unit_test", "unit_test.gold.b.ut", ["gold", "b", "ut"], ["model.gold.b"])
        },
    }

    tests = dbt_build.dbt_tests(manifest, {"a", "b"})

    assert {t.selector: (t.parents, t.explicit) for t in tests.values()} == {
        "gold.rel": (("a", "b"), True),
        "gold.src": ((), True),
        "gold.other": ((), False),
        "gold.b.ut": (("b",), True),
    }


def test_a_failed_source_test_blocks_the_models_reading_that_source() -> None:
    # a reads source x; b reads a and source y. The test on y fails alongside a, so b must not build.
    check = dbt_build.DbtTest("test.gold.not_null_y", "gold.not_null_y", (), True, ("source.gold.y",))
    dependencies = {"a": (), "b": ("a",), "c": ("a",)}
    sources = {"a": {"source.gold.x"}, "b": {"source.gold.x", "source.gold.y"}, "c": {"source.gold.x"}}

    batches, _, error = run(dependencies, Tokens(), tests=[check], failed_tests={"not_null_y"}, sources=sources)

    assert batches == [["a"], ["c"]]
    assert error is not None


def test_model_sources_reach_through_upstream_models() -> None:
    manifest = {
        "metadata": {"project_name": "gold"},
        "nodes": {
            "model.gold.a": {
                "name": "a", "resource_type": "model", "package_name": "gold",
                "depends_on": {"nodes": ["source.gold.x"]},
            },
            "model.gold.b": {
                "name": "b", "resource_type": "model", "package_name": "gold",
                "depends_on": {"nodes": ["model.gold.a", "source.gold.y"]},
            },
        },
    }

    assert dbt_build.model_sources(manifest, {"a", "b"}) == {
        "a": frozenset({"source.gold.x"}),
        "b": frozenset({"source.gold.x", "source.gold.y"}),
    }


def test_the_pause_tag_the_e2e_sets_is_the_one_the_runtime_reads() -> None:
    from openlakeforge_runtime import polaris_token

    from olf.infra.e2e import _token_lifetime

    assert _token_lifetime.GENERATION_PAUSE_TAG == polaris_token.GENERATION_PAUSE_TAG


def test_the_pause_runs_between_generations_before_the_next_token() -> None:
    order: list[str] = []
    tokens = Tokens()

    def invoke(models: list[str], _tests: list[dbt_build.DbtTest]):
        order.append(f"build {','.join(models)} (tokens {tokens.minted})")
        yield from models

    list(
        dbt_build.build_by_generation(
            {"a": (), "b": ("a",), "c": ("b",)},
            {},
            invoke,
            tokens,
            lambda *_: [],
            between=lambda: order.append(f"pause (tokens {tokens.minted})"),
        )
    )

    # No pause before the first generation; each pause comes before the next generation's token.
    assert order == [
        "build a (tokens 1)",
        "pause (tokens 1)",
        "build b (tokens 2)",
        "pause (tokens 2)",
        "build c (tokens 3)",
    ]


def test_a_test_on_an_unselected_materialized_model_is_not_this_runs() -> None:
    # Only `a` is selected; `b` (materialized, not selected) reads `a` and has its own test.
    manifest = {
        "metadata": {"project_name": "gold"},
        "nodes": {
            "model.gold.a": {
                "name": "a", "resource_type": "model", "package_name": "gold",
                "config": {"materialized": "table"}, "depends_on": {"nodes": []},
            },
            "model.gold.b": {
                "name": "b", "resource_type": "model", "package_name": "gold",
                "config": {"materialized": "table"}, "depends_on": {"nodes": ["model.gold.a"]},
            },
            "model.gold.e": {
                "name": "e", "resource_type": "model", "package_name": "gold",
                "config": {"materialized": "ephemeral"}, "depends_on": {"nodes": ["model.gold.a"]},
            },
            "test.gold.not_null_b": {
                "unique_id": "test.gold.not_null_b", "resource_type": "test", "package_name": "gold",
                "fqn": ["gold", "not_null_b"], "depends_on": {"nodes": ["model.gold.b"]},
            },
            "test.gold.not_null_e": {
                "unique_id": "test.gold.not_null_e", "resource_type": "test", "package_name": "gold",
                "fqn": ["gold", "not_null_e"], "depends_on": {"nodes": ["model.gold.e"]},
            },
        },
    }

    tests = dbt_build.dbt_tests(manifest, {"a"})

    # b's test stays out of the run; a test over an ephemeral model is looked through to `a`.
    assert (tests["test.gold.not_null_b"].parents, tests["test.gold.not_null_b"].explicit) == ((), False)
    assert (tests["test.gold.not_null_e"].parents, tests["test.gold.not_null_e"].explicit) == (("a",), True)
