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
from olf.infra.deployment.contract_env import ContractEnvironment
from olf.infra.e2e import _isolation, _runner, _shell, _trino

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
        values["OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET"] = "trino-credential"
    return _contract(tmp_path, values)


def _apply(monkeypatch: pytest.MonkeyPatch, contract: ContractEnvironment) -> None:
    @contextmanager
    def applied(**_kwargs: Any) -> Iterator[ContractEnvironment]:
        yield contract

    monkeypatch.setattr("olf.infra.deployment.contract_env.applied_contract_environment", applied)


def _write_profile(path: Path) -> None:
    path.write_text(
        "apiVersion: openlakeforge.io/v1alpha1\nkind: DeploymentProfile\nmetadata:\n  name: runtime-test\n"
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
    password_lookup, execution = _calls(kubectl_log)
    # The contract names a credential Secret, so the password is read before the query runs.
    assert "secret" in password_lookup["argv"]
    assert execution["argv"][execution["argv"].index("--catalog") + 1] == "lakehouse_prod"
    for call in (password_lookup, execution):
        assert call["argv"][:2] == ["--context", "ctx-from-contract"]
        assert call["kubeconfig"] == str(tmp_path / "contract/kubeconfig")
        assert call["marker"] == "from-contract"


def test_query_sends_no_password_when_the_contract_names_no_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kubectl_log: Path
) -> None:
    _apply(monkeypatch, _runtime_contract(tmp_path, kubectl_log, credential=False))
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", "ambient-credential")
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
