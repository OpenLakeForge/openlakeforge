"""The `olf:` recording rules of ADR 0024, evaluated by promtool over input series.

The rule file is rendered by Terraform from the module's own rules.tf, and promtool
is the one in the Prometheus image the catalog pins, so the test exercises the rules
and the evaluator that run in the cluster. Without Terraform or Docker it skips,
unless OLF_REQUIRE_RULE_TESTS is set, as the infrastructure job in checks.yml does.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
MODULE = REPO_ROOT / "infra/terraform/modules/observability/metrics"
PROMETHEUS_IMAGE = yaml.safe_load((REPO_ROOT / "release/component-catalog.yaml").read_text())["components"]["images"][
    "prometheus"
]


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


_MISSING = [
    reason
    for reason, missing in (
        ("terraform is not installed", shutil.which("terraform") is None),
        ("docker is not available to run promtool", not _docker_ready()),
    )
    if missing
]
if _MISSING and os.environ.get("OLF_REQUIRE_RULE_TESTS"):
    raise RuntimeError(f"OLF_REQUIRE_RULE_TESTS is set but {', '.join(_MISSING)}")
pytestmark = pytest.mark.skipif(bool(_MISSING), reason="; ".join(_MISSING))


def render_rules(work: Path) -> str:
    (work / "rules.tf").write_text((MODULE / "rules.tf").read_text())
    (work / "harness.tf").write_text('output "rules" { value = local.rules_yaml }\n')
    # rules.tf names no provider, so this runs offline.
    for args in (["init", "-backend=false", "-input=false"], ["apply", "-auto-approve"]):
        subprocess.run(["terraform", *args, "-no-color"], cwd=work, check=True, capture_output=True)
    out = subprocess.run(["terraform", "output", "-json"], cwd=work, check=True, capture_output=True, text=True)
    return str(json.loads(out.stdout)["rules"]["value"])


def promtool_test(tmp_path: Path, tests: list[dict[str, object]]) -> subprocess.CompletedProcess[str]:
    terraform_dir = tmp_path / "terraform"
    terraform_dir.mkdir()
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "rules.yml").write_text(render_rules(terraform_dir))
    suite = {"rule_files": ["rules.yml"], "evaluation_interval": "1m", "tests": tests}
    (rules_dir / "test.yml").write_text(yaml.safe_dump(suite, sort_keys=False))
    return subprocess.run(
        ["docker", "run", "--rm", "-v", f"{rules_dir}:/work:ro", "-w", "/work", "--entrypoint", "promtool"]
        + [PROMETHEUS_IMAGE, "test", "rules", "test.yml"],
        capture_output=True,
        text=True,
    )


def labels(name: str, pod: str, namespace: str, stage: str, component: str | None = None, **extra: str) -> str:
    pairs = {"namespace": namespace, "pod": pod, "stage": stage, **extra}
    if component is not None:
        pairs["label_app_kubernetes_io_name"] = component
    return name + "{" + ",".join(f'{key}="{value}"' for key, value in pairs.items()) + "}"


def pod(
    pod_name: str, namespace: str, stage: str, component: str | None, values: str, uid: str = "u"
) -> dict[str, str]:
    return {"series": labels("kube_pod_labels", pod_name, namespace, stage, component, uid=uid), "values": values}


def restarts(pod_name: str, namespace: str, stage: str, values: str) -> dict[str, str]:
    series = labels("kube_pod_container_status_restarts_total", pod_name, namespace, stage, container="main", uid="u")
    return {"series": series, "values": values}


def created(pod_name: str, namespace: str, values: str) -> dict[str, str]:
    """kube_pod_created, in seconds from the test's start; promtool's clock starts at 0."""
    return {"series": f'kube_pod_created{{namespace="{namespace}",pod="{pod_name}",uid="u"}}', "values": values}


def expect(series: str, at: str, samples: dict[tuple[str, str], float]) -> dict[str, object]:
    """The recorded series at `at`: exactly one sample per (stage, component), with no other label."""
    return {
        "expr": series,
        "eval_time": at,
        "exp_samples": [
            {"labels": f'{series}{{component="{component}",stage="{stage}"}}', "value": value}
            for (stage, component), value in samples.items()
        ],
    }


# Values at 0m, 1m, 2m, 3m, 4m; `_` is no sample and `stale` ends a series.
RESTART_INPUT = [
    pod("trino-0", "olf-dev", "dev", "trino", "1x4"),
    restarts("trino-0", "olf-dev", "dev", "0 0 1 1 3"),  # an existing container restarts
    pod("trino-crash", "olf-dev", "dev", "trino", "_ _ 1 1 1"),
    restarts("trino-crash", "olf-dev", "dev", "_ _ 3 4 4"),  # first seen at 2m, already restarted 3 times
    created("trino-crash", "olf-dev", "_ _ 110 110 110"),  # created 10 s before it was first seen
    pod("superset-0", "olf-dev", "dev", "superset", "1x4"),
    restarts("superset-0", "olf-dev", "dev", "4 4 0 0 1"),  # the counter goes back at 2m
    pod("polaris-0", "olf-system", "platform", "polaris", "1x4"),
    restarts("polaris-0", "olf-system", "platform", "0x4"),
    pod("polaris-old", "olf-system", "platform", "polaris", "1 1 stale"),
    restarts("polaris-old", "olf-system", "platform", "5 5 stale"),  # a pod that goes with 5 restarts
    pod("debug", "olf-dev", "dev", None, "1x4"),
    restarts("debug", "olf-dev", "dev", "0 1 1 1 1"),  # no app.kubernetes.io/name
]


def test_restarts_count_each_minute_per_container(tmp_path: Path) -> None:
    case: dict[str, object] = {
        "interval": "1m",
        "input_series": RESTART_INPUT,
        "promql_expr_test": [
            expect(
                "olf:container_restarts",
                "1m",
                {("dev", "trino"): 0, ("dev", "superset"): 0, ("platform", "polaris"): 0, ("dev", "unlabelled"): 1},
            ),
            # The new container's 3 restarts count in full; a reset counts none; the pod that went counts none.
            expect(
                "olf:container_restarts",
                "2m",
                {("dev", "trino"): 4, ("dev", "superset"): 0, ("platform", "polaris"): 0, ("dev", "unlabelled"): 0},
            ),
            expect(
                "olf:container_restarts",
                "3m",
                {("dev", "trino"): 1, ("dev", "superset"): 0, ("platform", "polaris"): 0, ("dev", "unlabelled"): 0},
            ),
            expect(
                "olf:container_restarts",
                "4m",
                {("dev", "trino"): 2, ("dev", "superset"): 1, ("platform", "polaris"): 0, ("dev", "unlabelled"): 0},
            ),
        ],
    }

    result = promtool_test(tmp_path, [case])

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_restart_just_before_the_pod_is_deleted_is_counted_once(tmp_path: Path) -> None:
    # Scraped every 30 s: 0 at 0:00, 1 at 0:30, and the pod is gone by the 1:00 scrape,
    # so an instant selector at the 1:00 evaluation sees neither the series nor its labels.
    case: dict[str, object] = {
        "interval": "30s",
        "input_series": [
            pod("trino-1", "olf-dev", "dev", "trino", "1 1 stale"),
            restarts("trino-1", "olf-dev", "dev", "0 1 stale"),
        ],
        "promql_expr_test": [
            expect("olf:container_restarts", "1m", {("dev", "trino"): 1}),
            expect("olf:container_restarts", "2m", {}),
        ],
    }

    result = promtool_test(tmp_path, [case])

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_pod_created_and_deleted_between_two_evaluations_keeps_its_restart(tmp_path: Path) -> None:
    # Created at 0:10, first scraped at 0:30 with one restart, gone by 1:00: no evaluation saw it.
    case: dict[str, object] = {
        "interval": "30s",
        "input_series": [
            pod("trino-2", "olf-dev", "dev", "trino", "_ 1 stale"),
            restarts("trino-2", "olf-dev", "dev", "_ 1 stale"),
            created("trino-2", "olf-dev", "_ 10 stale"),
        ],
        "promql_expr_test": [expect("olf:container_restarts", "1m", {("dev", "trino"): 1})],
    }

    result = promtool_test(tmp_path, [case])

    assert result.returncode == 0, result.stdout + result.stderr


def test_failed_queries_are_counted_per_minute_per_stage(tmp_path: Path) -> None:
    failed = "trino_execution_name_QueryManager_FailedQueries"
    # Scraped every 30 s from 0:00 to 3:00; the coordinator restarts between 2:00 and 2:30.
    case: dict[str, object] = {
        "interval": "30s",
        "input_series": [
            {
                "series": f'{failed}{{stage="dev",service="trino",instance="trino.olf-dev.svc:8080"}}',
                "values": "0 2 5 5 7 1 3",
            },
            {"series": f'{failed}{{stage="prod",service="trino",instance="trino.olf-prod.svc:8080"}}', "values": "4x6"},
        ],
        "promql_expr_test": [
            {
                "expr": "olf:trino_failed_queries",
                "eval_time": at,
                "exp_samples": [
                    {"labels": f'olf:trino_failed_queries{{stage="{stage}"}}', "value": value}
                    for stage, value in values.items()
                ],
            }
            for at, values in (
                ("1m", {"dev": 5, "prod": 0}),
                ("2m", {"dev": 2, "prod": 0}),
                # The minute of the restart counts 0, never the counter's drop.
                ("3m", {"dev": 0, "prod": 0}),
            )
        ],
    }

    result = promtool_test(tmp_path, [case])

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_container_seen_again_after_a_scrape_gap_replays_nothing(tmp_path: Path) -> None:
    # kube-state-metrics was unreachable at 1m and 2m: Prometheus marked its series stale,
    # and the old pod's container came back with the 20 restarts it had before.
    case: dict[str, object] = {
        "interval": "1m",
        "input_series": [
            pod("seaweedfs-0", "olf-system", "platform", "seaweedfs", "1 stale _ 1 1"),
            restarts("seaweedfs-0", "olf-system", "platform", "20 stale _ 20 20"),
            created("seaweedfs-0", "olf-system", "-86400 stale _ -86400 -86400"),
        ],
        "promql_expr_test": [
            expect("olf:container_restarts", "3m", {("platform", "seaweedfs"): 0}),
            expect("olf:container_restarts", "4m", {("platform", "seaweedfs"): 0}),
        ],
    }

    result = promtool_test(tmp_path, [case])

    assert result.returncode == 0, result.stdout + result.stderr


def test_memory_is_summed_per_component_through_the_pod_label_join(tmp_path: Path) -> None:
    memory = "container_memory_working_set_bytes"
    case: dict[str, object] = {
        "interval": "1m",
        "input_series": [
            pod("trino-0", "olf-dev", "dev", "trino", "1x2"),
            # A recreated pod briefly has two label series; the join keeps one.
            pod("trino-0", "olf-dev", "dev", "trino", "1x2", uid="v"),
            {"series": labels(memory, "trino-0", "olf-dev", "dev", container="trino"), "values": "100x2"},
            {"series": labels(memory, "trino-0", "olf-dev", "dev", container="jmx"), "values": "50x2"},
            pod("polaris-0", "olf-system", "platform", "polaris", "1x2"),
            {"series": labels(memory, "polaris-0", "olf-system", "platform", container="polaris"), "values": "70x2"},
            # A pod kube-state-metrics does not watch has no stage and no labels series: left out.
            {"series": f'{memory}{{namespace="kube-system",pod="coredns",container="coredns"}}', "values": "30x2"},
        ],
        "promql_expr_test": [
            expect("olf:memory_working_set_bytes", "1m", {("dev", "trino"): 150, ("platform", "polaris"): 70}),
        ],
    }

    result = promtool_test(tmp_path, [case])

    assert result.returncode == 0, result.stdout + result.stderr
