"""`olf infra dbt parse` reads its settings from the contract environment it is handed.

Every variable lives only in the `ContractEnvironment` mapping: `os.environ` is scrubbed of them, so a read that
falls back to the ambient process environment resolves the wrong project root and toolchain, and the dbt
subprocess never sees the contract's values.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from openlakeforge_runtime.dbt.render_profiles import PACKAGES_DIR_ENV
from typer.testing import CliRunner

from olf.cli import app
from olf.infra.deployment.contract_env import ContractEnvironment

REPO_ROOT = Path(__file__).resolve().parents[4]
runner = CliRunner()

_FAKE_DBT = f"""#!{sys.executable}
import json, os, sys

args = sys.argv[1:]
profiles = args[args.index("--profiles-dir") + 1] if "--profiles-dir" in args else None
with open(os.environ["FAKE_DBT_LOG"], "a") as log:
    log.write(json.dumps({{
        "command": args[0],
        "cwd": os.getcwd(),
        "stage_marker": os.environ.get("STAGE_MARKER"),
        "packages_dir": os.environ.get("{PACKAGES_DIR_ENV}"),
        "profiles": open(os.path.join(profiles, "profiles.yml")).read() if profiles else None,
    }}) + "\\n")
"""


@pytest.fixture(autouse=True)
def _scrub_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("OPENLAKEFORGE_", "OLF_")) or name in {"STAGE_MARKER", "FAKE_DBT_LOG"}:
            monkeypatch.delenv(name)


def test_dbt_parse_runs_in_the_contract_project_with_the_contract_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dbt = bindir / "dbt"
    dbt.write_text(_FAKE_DBT)
    dbt.chmod(0o755)
    dbt_log = tmp_path / "dbt.log"
    project_root = tmp_path / "customer"
    product = project_root / "lakehouse_code" / "gold"
    product.mkdir(parents=True)
    (product / "dbt_project.yml").write_text("name: gold\nprofile: gold\n")
    # The executable search itself still reads the process PATH; everything else comes from the contract.
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    contract = ContractEnvironment(
        environ={
            "OLF_TOOLCHAIN_MODE": "host",
            "OPENLAKEFORGE_REPO_ROOT": str(REPO_ROOT),
            "OPENLAKEFORGE_PROJECT_ROOT": str(project_root),
            "FAKE_DBT_LOG": str(dbt_log),
            "STAGE_MARKER": "from-contract",
        },
        namespace="stage-ns",
        kube_context="",
        kubeconfig_path=tmp_path / "kubeconfig",
        stage=None,
        unsets=(),
    )

    @contextmanager
    def provider_contract_environment(**_kwargs: str) -> Iterator[ContractEnvironment]:
        yield contract

    monkeypatch.setattr("olf.infra.commands.runtime.provider_contract_environment", provider_contract_environment)

    result = runner.invoke(app, ["infra", "dbt", "parse"])

    assert result.exit_code == 0, result.output
    calls = [json.loads(line) for line in dbt_log.read_text().splitlines()]
    assert [call["command"] for call in calls] == ["deps", "parse"]
    for call in calls:
        assert Path(call["cwd"]).resolve() == project_root.resolve()
        assert call["stage_marker"] == "from-contract"
        assert call["packages_dir"]
    assert calls[1]["profiles"].startswith("gold:\n")
