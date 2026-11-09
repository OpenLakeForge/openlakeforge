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


def test_each_stage_is_crawled_against_its_own_coordinator_and_catalog():
    prod = {
        **BASE,
        "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino-prod.olf-prod",
        "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_prod",
    }

    sources = [_build(environ)[2]["source"] for environ in (BASE, prod)]

    assert [s["serviceConnection"]["config"]["hostPort"] for s in sources] == [
        "trino.olf-system:8080",
        "trino-prod.olf-prod:8080",
    ]
    assert [s["sourceConfig"]["config"]["databaseFilterPattern"] for s in sources] == [
        {"includes": ["^lakehouse_dev$"]},
        {"includes": ["^lakehouse_prod$"]},
    ]
    assert {s["serviceName"] for s in sources} == {"polaris"}


def test_the_crawl_runs_beside_the_stage_coordinator_and_addresses_openmetadata_by_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from olf.infra import k8s
    from olf.infra.openmetadata import _ingestion

    applied: list[dict] = []
    monkeypatch.setattr(k8s, "apply_manifest", lambda manifest, **_: applied.append(manifest))
    monkeypatch.setattr(k8s, "run_job", lambda job, **_: applied.append(job))
    environ = {**BASE, "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino.olf-dev"}

    _ingestion.run_trino_ingestion(environ, namespace="olf-system", service="polaris")

    cronjob, job = applied
    assert (cronjob["metadata"]["namespace"], job["metadata"]["namespace"]) == ("olf-dev", "olf-dev")
    workflow = yaml.safe_load(job["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"])
    assert workflow["workflowConfig"]["openMetadataServerConfig"]["hostPort"] == "http://openmetadata.olf-system:8585/api"


POLARIS = {
    **TLS,
    "OPENLAKEFORGE_CATALOG_TOKEN_URI": "http://polaris.olf-system:8181/api/catalog/v1/oauth/tokens",
    "OPENLAKEFORGE_CATALOG_OAUTH_SCOPE": "PRINCIPAL_ROLE:ALL",
}


def test_the_polaris_token_is_minted_in_the_pod_and_never_stored():
    job, pod, workflow, env = _build(POLARIS)
    container = pod["containers"][0]

    arguments = workflow["source"]["serviceConnection"]["config"]["connectionArguments"]
    assert arguments["extra_credential"] == [["token", "__POLARIS_TOKEN__"]]
    assert arguments["verify"] == "/etc/olf/query-ca/ca.crt"
    assert 'POLARIS_JWT=$(python -c "$OM_POLARIS_MINT")' in container["command"][2]
    assert "s|__POLARIS_TOKEN__|$POLARIS_JWT|" in container["command"][2]
    assert {n: e["valueFrom"]["secretKeyRef"] for n, e in env.items() if n.startswith("POLARIS_CLIENT")} == {
        "POLARIS_CLIENT_ID": {"name": "polaris-om-creds", "key": "POLARIS_OM_CLIENT_ID"},
        "POLARIS_CLIENT_SECRET": {"name": "polaris-om-creds", "key": "POLARIS_OM_CLIENT_SECRET"},
    }
    assert "eyJ" not in json.dumps(job)


def test_without_a_polaris_contract_the_crawl_mints_nothing():
    _, pod, workflow, env = _build(TLS)

    assert "extra_credential" not in workflow["source"]["serviceConnection"]["config"]["connectionArguments"]
    assert "POLARIS" not in pod["containers"][0]["command"][2]
    assert "POLARIS_CLIENT_ID" not in env


def test_the_workflow_reads_no_rows():
    workflow = _build(POLARIS)[2]
    text = json.dumps(workflow).lower()

    assert set(workflow) == {"source", "sink", "workflowConfig"}  # no processor: no profiler, sampler or DQ
    for feature in ("profiler", "sample", "extractjsonschema", "usage", "dataquality", "testsuite"):
        assert feature not in text


def test_the_polaris_secret_is_copied_into_the_crawl_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    from olf.infra import k8s
    from olf.infra.openmetadata import _ingestion

    written: list[tuple] = []
    monkeypatch.setattr(k8s, "secret_data", lambda *a, **k: {"POLARIS_OM_CLIENT_ID": base64.b64encode(b"id").decode()})
    monkeypatch.setattr(k8s, "delete_secret", lambda *a, **k: None)
    monkeypatch.setattr(k8s, "write_secret", lambda name, ns, data, **k: written.append((name, ns, data)))
    monkeypatch.setattr(k8s, "apply_manifest", lambda *a, **k: None)
    monkeypatch.setattr(k8s, "run_job", lambda *a, **k: None)

    _ingestion.run_trino_ingestion(
        {**POLARIS, "OPENLAKEFORGE_QUERY_TRINO_HOST": "trino.olf-dev"}, namespace="olf-system", service="polaris"
    )

    assert written == [("polaris-om-creds", "olf-dev", {"POLARIS_OM_CLIENT_ID": "id"})]
