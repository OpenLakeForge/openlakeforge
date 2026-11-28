from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from openlakeforge_core import PROFILING_CRON, ProfilingTarget, profiling_metrics

from olf.infra import k8s
from olf.infra.openmetadata import _profiler
from olf.infra.openmetadata._ingestion import ingestion_job
from olf.infra.openmetadata._profiler import (
    PROFILER_JOB_NAME,
    profiler_config,
    profiler_cronjob,
    profiler_job,
    profiling_targets,
    run_trino_profiling,
)

REPO_ROOT = Path(__file__).resolve().parents[4]

DEV = {
    "OPENLAKEFORGE_REPO_ROOT": str(REPO_ROOT),
    "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino.olf-dev",
    "OPENLAKEFORGE_QUERY_TRINO_PORT": "8080",
    "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev",
    "OPENLAKEFORGE_CATALOG_TOKEN_URI": "http://polaris/api/catalog/v1/oauth/tokens",
    "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME": "openmetadata-ingestion-bot",
    "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY": "OPENMETADATA_INGESTION_BOT_JWT",
}
PROD = {
    **DEV,
    "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino-prod.olf-prod",
    "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_prod",
}

TARGET = ProfilingTarget(
    "sales_core",
    "sales_core_gold",
    ("dim_account", "fct_order_lines"),
    profiling_metrics("Internal") or (),
)

# The `sourceConfig.config` keys of DatabaseServiceProfilerPipeline in OpenMetadata 1.13.6 (the model forbids others).
PROFILER_KEYS_1_13_6 = {
    "type",
    "classificationFilterPattern",
    "computeColumnMetrics",
    "computeTableMetrics",
    "databaseFilterPattern",
    "schemaFilterPattern",
    "tableFilterPattern",
    "includeViews",
    "metrics",
    "processingEngine",
    "profileSampleConfig",
    "randomizedSample",
    "threadCount",
    "timeoutSeconds",
    "useFqnForFiltering",
    "useStatistics",
}


def _workflows(job: dict) -> list[dict]:
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    return [yaml.safe_load(e["value"]) for e in env if e["name"].startswith("OM_WORKFLOW_")]


def _job(environ=DEV, targets=(TARGET,)) -> dict:
    return profiler_job(
        environ,
        image="ingestion@sha256:abc",
        namespace="olf-dev",
        service="polaris",
        om_api="http://om:8585/api",
        targets=targets,
    )


def test_the_profile_workflow_sends_only_keys_the_1_13_6_model_accepts_and_no_sampling():
    config = profiler_config(DEV, service="polaris", om_api="http://om:8585/api", target=TARGET)

    source = config["source"]["sourceConfig"]["config"]
    assert set(source) <= PROFILER_KEYS_1_13_6
    assert config["processor"] == {"type": "orm-profiler", "config": {}}
    assert config["sink"] == {"type": "metadata-rest", "config": {}}
    assert set(config) == {"source", "processor", "sink", "workflowConfig"}
    assert not {"generateSampleData", "profileSample", "profileSampleType", "profileSampleConfig"} & set(source)


def test_the_profile_is_bounded_and_reads_only_the_eligible_tables_of_this_stage():
    source = profiler_config(DEV, service="polaris", om_api="http://om", target=TARGET)["source"]["sourceConfig"][
        "config"
    ]

    assert (source["threadCount"], source["includeViews"]) == (1, False)
    assert 0 < source["timeoutSeconds"] <= 900
    assert source["databaseFilterPattern"] == {"includes": ["^lakehouse_dev$"]}
    assert source["schemaFilterPattern"] == {"includes": ["^sales_core_gold$"]}
    assert source["tableFilterPattern"] == {"includes": ["^dim_account$", "^fct_order_lines$"]}
    for pattern in source["tableFilterPattern"]["includes"]:
        assert re.fullmatch(r"\^[a-z_]+\$", pattern), pattern


def test_an_internal_product_stores_values_and_a_confidential_one_does_not():
    confidential = ProfilingTarget("p", "p_gold", ("fct_x",), profiling_metrics("Confidential") or ())
    metrics = {
        name: profiler_config(DEV, service="polaris", om_api="http://om", target=t)["source"]["sourceConfig"]["config"][
            "metrics"
        ]
        for name, t in (("internal", TARGET), ("confidential", confidential))
    }

    assert {"min", "max", "mean", "rowCount", "nullCount", "distinctCount"} <= set(metrics["internal"])
    assert not {"min", "max", "mean"} & set(metrics["confidential"])


def test_the_profiler_pod_is_the_crawls_pod_with_a_profile_command():
    crawl = ingestion_job(DEV, image="ingestion@sha256:abc", namespace="olf-dev", service="polaris", om_api="http://om")
    profile = _job()
    crawl_container = crawl["spec"]["template"]["spec"]["containers"][0]
    container = profile["spec"]["template"]["spec"]["containers"][0]

    assert container["resources"] == crawl_container["resources"]
    assert container["image"] == crawl_container["image"]
    assert "metadata profile -c" in container["command"][2] and "metadata ingest" not in container["command"][2]
    secret_refs = {e["name"]: e["valueFrom"] for e in container["env"] if "valueFrom" in e}
    crawl_refs = {e["name"]: e["valueFrom"] for e in crawl_container["env"] if "valueFrom" in e}
    assert secret_refs == crawl_refs
    assert "POLARIS_JWT=$(python" in container["command"][2]
    assert profile["metadata"]["labels"] == {"openlakeforge.io/job": PROFILER_JOB_NAME}
    assert profile["spec"]["backoffLimit"] == 0
    assert profile["spec"]["activeDeadlineSeconds"] <= 3600


def test_each_stage_has_its_own_job_filters_and_daily_cronjob():
    dev, prod = _job(DEV), _job(PROD)
    dev_cron = profiler_cronjob(dev, catalog="lakehouse_dev")
    prod_cron = profiler_cronjob(prod, catalog="lakehouse_prod")

    assert [
        w["source"]["sourceConfig"]["config"]["databaseFilterPattern"]
        for w in (_workflows(dev)[0], _workflows(prod)[0])
    ] == [
        {"includes": ["^lakehouse_dev$"]},
        {"includes": ["^lakehouse_prod$"]},
    ]
    assert "lakehouse_prod" not in yaml.safe_dump(_workflows(dev))
    assert [c["metadata"]["name"] for c in (dev_cron, prod_cron)] == [
        "openmetadata-trino-profiler-lakehouse-dev",
        "openmetadata-trino-profiler-lakehouse-prod",
    ]
    assert prod_cron["spec"]["schedule"] == PROFILING_CRON
    assert prod_cron["spec"]["concurrencyPolicy"] == "Forbid"
    assert prod_cron["spec"]["jobTemplate"]["spec"] == prod["spec"]


def test_each_product_gets_its_own_workflow_and_the_job_fails_if_any_does():
    other = ProfilingTarget("other", "other_gold", ("fct_x",), profiling_metrics("Confidential") or ())

    job = _job(targets=(TARGET, other))
    command = job["spec"]["template"]["spec"]["containers"][0]["command"][2]

    workflows = _workflows(job)
    assert [w["source"]["sourceConfig"]["config"]["schemaFilterPattern"]["includes"] for w in workflows] == [
        ["^sales_core_gold$"],
        ["^other_gold$"],
    ]
    assert command.count("metadata profile -c") == 2
    assert command.endswith("exit $rc")


def test_the_repo_policy_selects_the_sales_core_facts_and_dimensions_and_no_mart():
    targets = profiling_targets(REPO_ROOT / "lakehouse_code")

    assert [t.product for t in targets] == ["sales_core"]
    assert set(targets[0].tables) == {
        "fct_order_lines",
        "dim_account",
        "dim_product",
        "dim_channel",
        "dim_promotion",
    }


class _Cluster:
    """The CronJobs and Jobs olf leaves behind, by name; `fail_job` makes the profile Job fail."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fail_job: bool = False) -> None:
        self.cronjobs: dict[str, dict] = {}
        self.jobs: list[str] = []
        monkeypatch.setattr(k8s, "apply_manifest", lambda m, **_: self.cronjobs.__setitem__(m["metadata"]["name"], m))
        monkeypatch.setattr(k8s, "delete_cronjob", lambda name, _ns, **_: self.cronjobs.pop(name, None))
        monkeypatch.setattr(_profiler, "copy_polaris_secret", lambda *_a, **_k: None)

        def run_job(job, **_):  # noqa: ANN001, ANN003, ANN202
            self.jobs.append(job["metadata"]["name"])
            if fail_job:
                raise k8s.KubectlError("Job openmetadata-trino-profiler failed:\nboom")

        monkeypatch.setattr(k8s, "run_job", run_job)


def test_the_daily_schedule_is_installed_even_when_the_first_profile_fails(monkeypatch: pytest.MonkeyPatch):
    cluster = _Cluster(monkeypatch, fail_job=True)

    assert run_trino_profiling(DEV, namespace="olf-system", service="polaris", targets=(TARGET,)) is False

    cron = cluster.cronjobs["openmetadata-trino-profiler-lakehouse-dev"]
    assert cron["spec"]["schedule"] == PROFILING_CRON


def test_a_profile_runs_the_job_and_leaves_the_schedule(monkeypatch: pytest.MonkeyPatch):
    cluster = _Cluster(monkeypatch)

    assert run_trino_profiling(DEV, namespace="olf-system", service="polaris", targets=(TARGET,)) is True

    assert cluster.jobs == [PROFILER_JOB_NAME]
    assert list(cluster.cronjobs) == ["openmetadata-trino-profiler-lakehouse-dev"]


def test_a_failed_profile_warns_and_does_not_raise(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    _Cluster(monkeypatch, fail_job=True)

    assert run_trino_profiling(DEV, namespace="olf-system", service="polaris", targets=(TARGET,)) is False

    output = capsys.readouterr()
    assert "did not complete" in output.out + output.err and "stale" in output.out + output.err


def test_turning_profiling_off_removes_the_schedule_and_runs_nothing(monkeypatch: pytest.MonkeyPatch):
    cluster = _Cluster(monkeypatch)
    run_trino_profiling(PROD, namespace="olf-system", service="polaris", targets=(TARGET,))
    assert cluster.cronjobs

    assert run_trino_profiling(PROD, namespace="olf-system", service="polaris", targets=()) is True

    assert cluster.cronjobs == {}
    assert cluster.jobs == [PROFILER_JOB_NAME]


def test_on_glue_the_filter_names_the_physical_schema_the_contract_maps_to():
    glue = {
        **DEV,
        "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON": json.dumps(
            {"sales_core": "polaris.lakehouse_dev.lakehouse_dev_sales_core_gold"}
        ),
    }

    source = profiler_config(glue, service="polaris", om_api="http://om", target=TARGET)["source"]["sourceConfig"]
    assert source["config"]["schemaFilterPattern"] == {"includes": ["^lakehouse_dev_sales_core_gold$"]}
