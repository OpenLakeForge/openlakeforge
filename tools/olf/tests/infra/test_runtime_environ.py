"""The runtime commands and the e2e run read their settings from the contract environment they are handed.

Every variable lives only in the `ContractEnvironment` mapping: `os.environ` is scrubbed of them, so a read that
falls back to the ambient process environment sees defaults (or nothing) and the assertions below fail.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import e2e_cfg
from typer.testing import CliRunner

from olf.cli import app
from olf.infra.access import Route
from olf.infra.auth import aws_session
from olf.infra.deployment.contract_env import ContractEnvironment
from olf.infra.e2e import _assertions, _isolation, _preflight, _runner, _shell, _trino
from olf.infra.e2e._shell import E2EError

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[4]

_FAKE_KUBECTL = f"""#!{sys.executable}
import base64, json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_LOG"], "a") as log:
    log.write(json.dumps({{"argv": args, "kubeconfig": os.environ.get("KUBECONFIG"),
                          "marker": os.environ.get("STAGE_MARKER")}}) + "\\n")
if "secret" in args:
    secret = args[args.index("secret") + 1]
    key = args[-1].removeprefix("jsonpath={{.data.").removesuffix("}}")
    print(base64.b64encode(f"{{secret}}:{{key}}".encode()).decode(), end="")
elif "exec" in args:
    print("rows-from-trino")
else:
    print("found")
"""


def _contract(tmp_path: Path, values: dict[str, str]) -> ContractEnvironment:
    return ContractEnvironment(
        environ=values,
        namespace=values.get("NAMESPACE", ""),
        kube_context=values.get("KUBE_CONTEXT", ""),
        kubeconfig_path=tmp_path / "kubeconfig",
        stage=None,
        unsets=(),
    )


@pytest.fixture(autouse=True)
def _scrub_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    scrubbed = {"NAMESPACE", "KUBE_CONTEXT", "KUBECONFIG", "CLUSTER_NAME", "DAGSTER_LOCAL_PORT", "STAGE_MARKER"}
    for name in list(os.environ):
        if name.startswith(("OPENLAKEFORGE_", "OLF_", "AWS_")) or name in scrubbed:
            monkeypatch.delenv(name)


@pytest.fixture
def kubectl_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text(_FAKE_KUBECTL)
    kubectl.chmod(0o755)
    # The executable search itself still reads the process PATH; everything else comes from the contract.
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path / "kubectl.log"


def _calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def _runtime_contract(tmp_path: Path, log: Path, *, credential: bool = True) -> ContractEnvironment:
    values = {
        "OLF_TOOLCHAIN_MODE": "host",
        "FAKE_KUBECTL_LOG": str(log),
        "STAGE_MARKER": "from-contract",
        "KUBE_CONTEXT": "ctx-from-contract",
        "KUBECONFIG": str(tmp_path / "contract/kubeconfig"),
        "OPENLAKEFORGE_QUERY_TRINO_CATALOG": "lakehouse_prod",
        "DAGSTER_LOCAL_PORT": "4242",
    }
    if credential:
        values["OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME"] = "polaris-prod-processing-creds"
    return _contract(tmp_path, values)


def _apply(monkeypatch: pytest.MonkeyPatch, contract: ContractEnvironment) -> None:
    @contextmanager
    def applied(**_kwargs: Any) -> Iterator[ContractEnvironment]:
        yield contract

    monkeypatch.setattr("olf.infra.deployment.contract_env.applied_contract_environment", applied)


def _write_profile(path: Path) -> None:
    path.write_text(
        "apiVersion: openlakeforge.io/v1beta1\nkind: DeploymentProfile\nmetadata:\n  name: runtime-test\n"
        "spec:\n  provider:\n    type: local\n  preset: slim\n  stages:\n    dev:\n      enabled: true\n",
        encoding="utf-8",
    )


def test_query_runs_kubectl_with_the_contract_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kubectl_log: Path
) -> None:
    _apply(monkeypatch, _runtime_contract(tmp_path, kubectl_log))
    profile = tmp_path / "openlakeforge.yaml"
    _write_profile(profile)

    result = runner.invoke(app, ["infra", "query", "-f", str(profile), "SELECT 1"])

    assert result.exit_code == 0, result.output
    assert "rows-from-trino" in result.output
    credential_lookup, execution = _calls(kubectl_log)
    # The contract names the stage processing credential, so it is read before the query runs.
    assert "polaris-prod-processing-creds" in credential_lookup["argv"]
    assert execution["argv"][execution["argv"].index("--catalog") + 1] == "lakehouse_prod"
    for call in (credential_lookup, execution):
        assert call["argv"][:2] == ["--context", "ctx-from-contract"]
        assert call["kubeconfig"] == str(tmp_path / "contract/kubeconfig")
        assert call["marker"] == "from-contract"


def test_query_reads_no_credential_when_the_contract_names_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kubectl_log: Path
) -> None:
    _apply(monkeypatch, _runtime_contract(tmp_path, kubectl_log, credential=False))
    monkeypatch.setenv("OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME", "ambient-credential")
    profile = tmp_path / "openlakeforge.yaml"
    _write_profile(profile)

    result = runner.invoke(app, ["infra", "query", "-f", str(profile), "SELECT 1"])

    assert result.exit_code == 0, result.output
    (execution,) = _calls(kubectl_log)
    assert "exec" in execution["argv"]


def test_product_run_builds_the_e2e_config_from_the_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kubectl_log: Path
) -> None:
    shutil.copytree(ROOT / "lakehouse_code", tmp_path / "lakehouse_code")
    _apply(monkeypatch, _runtime_contract(tmp_path, kubectl_log))
    monkeypatch.setattr("olf.infra.e2e._runner.prepare_kube_context", lambda _cfg, _kubeconfig: None)
    seen: list[Any] = []
    monkeypatch.setattr("olf.infra.e2e._dagster.launch_and_poll_dagster_jobs", lambda cfg, **_kw: seen.append(cfg))
    profile = tmp_path / "openlakeforge.yaml"
    _write_profile(profile)

    result = runner.invoke(app, ["infra", "product", "run", "order_revenue", "-f", str(profile)])

    assert result.exit_code == 0, result.output
    (cfg,) = seen
    assert (cfg.kube_context, cfg.dagster_local_port) == ("ctx-from-contract", 4242)
    assert _runner.configure_kubeconfig(cfg) == (tmp_path / "contract/kubeconfig").resolve()


def test_e2e_kubectl_and_probes_read_the_contract_environment(tmp_path: Path, kubectl_log: Path) -> None:
    cfg = replace(e2e_cfg(tmp_path), kube_context="ctx-from-contract", environ=_runtime_contract(tmp_path, kubectl_log))

    assert _trino.stage_catalog_name(cfg) == "lakehouse_prod"
    _shell.kubectl(cfg, ["get", "pods"], capture=True)
    assert _isolation._s3_identity("olf-dev", "creds", environ=cfg.environ) == (
        "creds:AWS_ACCESS_KEY_ID",
        "creds:AWS_SECRET_ACCESS_KEY",
    )
    assert _isolation._sibling_stage(cfg) == "dev"
    for call in _calls(kubectl_log):
        assert call["kubeconfig"] == str(tmp_path / "contract/kubeconfig")
        assert call["marker"] == "from-contract"


def test_e2e_probes_still_read_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CATALOG", "lakehouse_ambient")

    assert _trino.stage_catalog_name(e2e_cfg(tmp_path)) == "lakehouse_ambient"


def _stub_openmetadata(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The port-forward, HTTP wait and OpenMetadata checks are replaced; the secret lookups stay real."""
    seen: dict[str, Any] = {"stages": []}

    @contextmanager
    def service_route(stage_path: str, **_kwargs: Any) -> Iterator[Route]:
        seen["stages"].append(stage_path)
        yield Route("http://openmetadata.invalid")

    class Client:
        def __init__(self, _base_url: str, *, token: str | None = None, **_kwargs: Any) -> None:
            seen["token"] = token

    monkeypatch.setattr(_assertions.access, "service_route", service_route)
    monkeypatch.setattr(_assertions.k8s, "http_wait", lambda *_a, **_kw: True)
    monkeypatch.setattr(_assertions, "OpenMetadataClient", Client)
    monkeypatch.setattr(
        _assertions, "assert_openmetadata_assets", lambda _c, _i, password: seen.update(password=password)
    )
    monkeypatch.setattr(_assertions, "assert_openmetadata_stage_roots", lambda _c, **kw: seen.update(roots=kw))
    monkeypatch.setattr(_assertions, "assert_openmetadata_stage_lineage", lambda *_a, **_kw: None)
    monkeypatch.setattr(_assertions, "load_provider_contracts_or_raise", lambda _cfg: {"stages": {"prod": {}}})
    monkeypatch.setattr(_assertions, "product_gold_tables", lambda *_a, **_kw: [])
    return seen


def test_openmetadata_assertions_read_the_contract_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kubectl_log: Path
) -> None:
    contract = _runtime_contract(tmp_path, kubectl_log)
    contract.environ["OPENLAKEFORGE_CATALOG_DATABASE_FQN"] = "trino.lakehouse_prod"
    cfg = replace(e2e_cfg(tmp_path), kube_context="ctx-from-contract", environ=contract, openmetadata_local_port=1)
    seen = _stub_openmetadata(monkeypatch)

    _assertions.check_openmetadata_assets(cfg)

    assert seen["token"].startswith("openmetadata-")
    assert seen["roots"]["database_fqn"] == "trino.lakehouse_prod"
    for call in _calls(kubectl_log):
        assert call["argv"][:2] == ["--context", "ctx-from-contract"]
        assert call["kubeconfig"] == str(tmp_path / "contract/kubeconfig")
        assert call["marker"] == "from-contract"


def test_openmetadata_assertions_fall_back_to_the_admin_password_through_the_contract_kubectl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kubectl_log: Path
) -> None:
    cfg = replace(
        e2e_cfg(tmp_path),
        kube_context="ctx-from-contract",
        environ=_runtime_contract(tmp_path, kubectl_log),
        openmetadata_local_port=1,
    )
    seen = _stub_openmetadata(monkeypatch)
    monkeypatch.setattr(_assertions, "automation_token", lambda *_a, **_kw: None)

    _assertions.check_openmetadata_assets(cfg)

    assert seen["password"].endswith(":admin-password")
    assert {call["kubeconfig"] for call in _calls(kubectl_log)} == {str(tmp_path / "contract/kubeconfig")}


def test_superset_assertions_use_the_contract_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = replace(
        e2e_cfg(tmp_path), environ=_contract(tmp_path, {"OPENLAKEFORGE_CONTRACT_STAGE": "prod"}), superset_local_port=1
    )
    seen = _stub_openmetadata(monkeypatch)
    monkeypatch.setattr(_assertions.k8s, "http_wait", lambda *_a, **_kw: False)

    with pytest.raises(E2EError, match="Superset endpoint did not become reachable"):
        _assertions.check_superset_dashboards(cfg)

    assert seen["stages"] == ["stage/prod/reporting"]


def test_aws_preflight_builds_its_session_from_the_contract_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    auth = tmp_path / "olf-home" / "auth"
    auth.mkdir(parents=True)
    (auth / "aws.json").write_text(json.dumps({"source": "profile", "profile": "saved-profile"}))
    config_file = tmp_path / "aws-config"
    config_file.write_text("[profile saved-profile]\nregion = us-west-2\n")
    # The SDK's own config lookup still reads the process; the profile choice must come from the contract.
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr("olf.infra.auth.aws._aws_instance_profile_available", lambda: False)
    monkeypatch.setattr(
        _shell, "terraform_output", lambda _dir, name, **_: "eu-central-1" if name == "aws_region" else ""
    )
    monkeypatch.setattr(
        _preflight,
        "load_provider_contracts_or_raise",
        lambda _cfg: {
            "shared": {"ops_storage": {"bucket_name": "ops"}},
            "stages": {"prod": {"catalog": {"catalog_name": "lakehouse_prod"}}},
        },
    )
    sessions: list[Any] = []

    def capture(environ: Any, *, region: str | None = None) -> Any:
        session = aws_session(environ, region=region)
        sessions.append(session)
        return session

    monkeypatch.setattr(_preflight, "aws_session", capture)
    cfg = replace(
        e2e_cfg(tmp_path, env="aws", suite="smoke"),
        environ=_contract(
            tmp_path, {"OLF_HOME": str(tmp_path / "olf-home"), "OPENLAKEFORGE_QUERY_TRINO_CATALOG": "lakehouse_prod"}
        ),
    )

    with pytest.raises(E2EError):  # the profile has no credentials; only the session it built is under test
        _preflight.check_aws_storage_and_glue(cfg)

    (session,) = sessions
    assert (session.profile_name, session.region_name) == ("saved-profile", "eu-central-1")


def test_the_kube_context_check_runs_kubectl_with_the_contract_environment(tmp_path: Path, kubectl_log: Path) -> None:
    """`olf infra product run` checks the context before launching, so the check must reach the contract's cluster."""
    cfg = replace(
        e2e_cfg(tmp_path), kube_context="ctx-from-contract", environ=_runtime_contract(tmp_path, kubectl_log)
    )

    _runner.prepare_kube_context(cfg, tmp_path / "contract/kubeconfig")

    (call,) = _calls(kubectl_log)
    assert call["argv"] == ["cluster-info", "--context", "ctx-from-contract"]
    assert (call["kubeconfig"], call["marker"]) == (str(tmp_path / "contract/kubeconfig"), "from-contract")
