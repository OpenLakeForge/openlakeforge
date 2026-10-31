import json
from pathlib import Path

import pytest
import yaml

from olf.infra.openmetadata._ingestion import ingestion_cronjob, ingestion_job

BASE = {
    "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino.olf-system",
    "OPENLAKEFORGE_QUERY_TRINO_PORT": "8080",
    "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev",
    "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME": "openmetadata-ingestion-bot",
    "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY": "OPENMETADATA_INGESTION_BOT_JWT",
}
TLS = {**BASE, "OPENLAKEFORGE_QUERY_TRINO_TLS_PORT": "8443", "OPENLAKEFORGE_QUERY_TRINO_CA_CONFIGMAP": "olf-local-ca"}


def _build(environ):
    job = ingestion_job(
        environ, image="ingestion@sha256:abc", namespace="olf-system", service="polaris", om_api="http://om:8585/api"
    )
    pod = job["spec"]["template"]["spec"]
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    return job, pod, yaml.safe_load(env["OM_WORKFLOW"]["value"]), env


def test_http_contract_crawls_the_http_port_without_a_ca():
    job, pod, workflow, _ = _build(BASE)
    connection = workflow["source"]["serviceConnection"]["config"]
    assert connection["hostPort"] == "trino.olf-system:8080"
    assert "connectionArguments" not in connection
    assert "volumes" not in pod
    assert pod["containers"][0]["image"] == "ingestion@sha256:abc"


def test_tls_contract_verifies_against_the_mounted_ca():
    _, pod, workflow, _ = _build(TLS)
    connection = workflow["source"]["serviceConnection"]["config"]
    assert connection["hostPort"] == "trino.olf-system:8443"
    assert connection["connectionArguments"] == {"http_scheme": "https", "verify": "/etc/olf/query-ca/ca.crt"}
    assert pod["volumes"] == [{"name": "query-ca", "configMap": {"name": "olf-local-ca"}}]
    assert pod["containers"][0]["volumeMounts"][0]["mountPath"] == "/etc/olf/query-ca"


@pytest.mark.parametrize("environ", [BASE, TLS])
def test_bot_token_is_a_secret_reference_never_a_value(environ):
    job, _, workflow, env = _build(environ)
    assert env["OM_BOT_JWT"]["valueFrom"]["secretKeyRef"] == {
        "name": "openmetadata-ingestion-bot",
        "key": "OPENMETADATA_INGESTION_BOT_JWT",
    }
    assert "value" not in env["OM_BOT_JWT"]
    assert workflow["source"]["sourceConfig"]["config"]["databaseFilterPattern"] == {"includes": ["^lakehouse_dev$"]}
    assert "eyJ" not in json.dumps(job)


@pytest.mark.parametrize("environ", [BASE, TLS])
def test_hourly_refresh_runs_the_same_pod_as_the_job(environ):
    job, _, _, _ = _build(environ)
    cron = ingestion_cronjob(job, catalog="lakehouse_dev")
    assert cron["kind"] == "CronJob"
    assert cron["metadata"]["name"] == "openmetadata-trino-ingestion-lakehouse-dev"
    assert cron["spec"]["schedule"] == "0 * * * *"
    assert cron["spec"]["jobTemplate"]["spec"] == job["spec"]
    assert job["spec"]["template"]["metadata"]["labels"] == {"openlakeforge.io/job": "openmetadata-trino-ingestion"}


def test_nothing_triggers_the_pipeline_client_pipeline_any_more():
    module = Path(__file__).parents[4] / "infra/terraform/modules/governance/openmetadata"
    assert not (module / "refresh.tf").exists()
    assert "$catalog_service-metadata-ingestion" not in (module / "templates/bootstrap.sh.tftpl").read_text()


def test_a_password_contract_gives_the_crawl_the_openmetadata_principals_password_by_reference():
    _, pod, workflow, env = _build({**TLS, "OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET": "trino-credential"})

    assert env["TRINO_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "trino-credential-openmetadata",
        "key": "password",
    }
    assert workflow["source"]["serviceConnection"]["config"]["authType"] == {"password": "__TRINO_PASSWORD__"}
    assert "__TRINO_PASSWORD__" in pod["containers"][0]["command"][2]


def test_without_a_password_contract_the_crawl_carries_no_credential():
    _, _, workflow, env = _build(TLS)

    assert "authType" not in workflow["source"]["serviceConnection"]["config"]
    assert "TRINO_PASSWORD" not in env


@pytest.mark.parametrize("root", ["local", "aws-poc", "azure-poc"])
def test_every_root_maps_the_lineage_of_trinos_https_listener(root):
    # dbt names its lineage datasets by the Trino port it connects to, which is the TLS port once workload
    # authentication is on; OpenMetadata drops a dataset whose namespace it cannot map to the catalog service.
    terraform = Path(__file__).parents[4] / "infra/terraform"
    main = (terraform / "environments" / root / "main.tf").read_text()
    assert "trino_tls_lineage_namespace = module.trino.tls_port == null ? null :" in main
    template = (terraform / "modules/governance/openmetadata/templates/bootstrap.sh.tftpl").read_text()
    assert "{($trino_tls):$service}" in template
