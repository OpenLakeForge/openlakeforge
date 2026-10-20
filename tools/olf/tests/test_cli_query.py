from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from olf import trino
from olf.cli import app

runner = CliRunner()

_AWS_IDENTITY = (
    "  identity:\n"
    "    issuer: external\n"
    "    issuer_url: https://login.example.com/\n"
    "    role_claim: groups\n"
    "    role_mapping:\n"
    "      platform-admin: [olf-admins]\n"
)


def _write_profile(path: Path, *, provider: str = "local", stages: tuple[str, ...] = ("dev",)) -> None:
    region = "    region: eu-west-3\n" if provider == "aws" else ""
    path.write_text(
        "apiVersion: openlakeforge.io/v1alpha1\nkind: DeploymentProfile\nmetadata:\n  name: query-test\n"
        f"spec:\n  provider:\n    type: {provider}\n{region}  preset: slim\n  stages:\n"
        + "".join(f"    {stage}:\n      enabled: true\n" for stage in stages)
        + (_AWS_IDENTITY if provider == "aws" else ""),
        encoding="utf-8",
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen: list[dict] = []

    def run_sql(target, sql, *, output_format, kubectl, runner=None):  # noqa: ANN001, ANN202, ARG001
        seen.append({"target": target, "sql": sql, "format": output_format})
        return "ok\n"

    monkeypatch.setattr(trino, "run_sql", run_sql)
    monkeypatch.setattr("olf.contracts.load_provider_contracts", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "olf.contracts.build_contract_env",
        lambda *_a, **_k: ({"OPENLAKEFORGE_QUERY_TRINO_CATALOG": "lakehouse_prod"}, []),
    )
    monkeypatch.setattr("olf.tooling.resolver.build_resolver", lambda: SimpleNamespace(resolve=lambda _name: "kubectl"))
    return seen


def test_query_runs_against_the_selected_stage_catalog(tmp_path: Path, calls: list[dict]) -> None:
    profile = tmp_path / "openlakeforge.yaml"
    _write_profile(profile, stages=("dev", "prod"))

    result = runner.invoke(app, ["query", "-f", str(profile), "--stage", "prod", "--format", "csv", "SELECT 1"])

    assert result.exit_code == 0, result.output
    assert "ok" in result.output
    (call,) = calls
    assert (call["target"].stage, call["target"].catalog, call["target"].namespace) == (
        "prod",
        "lakehouse_prod",
        "olf-system",
    )
    assert call["format"] == "CSV_HEADER"


def test_query_reads_the_sql_from_a_file(tmp_path: Path, calls: list[dict]) -> None:
    profile = tmp_path / "openlakeforge.yaml"
    _write_profile(profile)
    sql = tmp_path / "report.sql"
    sql.write_text("SELECT 2", encoding="utf-8")

    result = runner.invoke(app, ["query", "-f", str(profile), "--file", str(sql)])

    assert result.exit_code == 0, result.output
    assert calls[0]["sql"] == "SELECT 2"


def test_query_rejects_a_stage_the_profile_does_not_enable(tmp_path: Path, calls: list[dict]) -> None:
    profile = tmp_path / "openlakeforge.yaml"
    _write_profile(profile)

    result = runner.invoke(app, ["query", "-f", str(profile), "--stage", "prod", "SELECT 1"])

    assert result.exit_code == 1
    assert "stage 'prod' is not enabled" in result.output
    assert not calls


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--format", "xml", "SELECT 1"], "unknown --format"),
        ([], "either as an argument or with --file"),
        (["SELECT 1", "--file", "x.sql"], "either as an argument or with --file"),
    ],
)
def test_query_rejects_bad_arguments(args: list[str], message: str) -> None:
    result = runner.invoke(app, ["query", *args])

    assert result.exit_code == 1
    assert message in result.output


def test_query_refuses_aws_until_glue_prefixes_are_handled(tmp_path: Path, calls: list[dict]) -> None:
    profile = tmp_path / "openlakeforge.yaml"
    _write_profile(profile, provider="aws")

    result = runner.invoke(app, ["query", "-f", str(profile), "SELECT 1"])

    assert result.exit_code == 1
    assert "does not support the aws provider" in result.output
    assert not calls


def test_query_reports_an_invalid_provider_contract_without_a_traceback(
    tmp_path: Path, calls: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    from olf.provider_contracts import ProviderContractError

    def invalid(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise ProviderContractError("unsupported provider contract schema")

    monkeypatch.setattr("olf.contracts.load_provider_contracts", invalid)
    profile = tmp_path / "openlakeforge.yaml"
    _write_profile(profile)

    result = runner.invoke(app, ["query", "-f", str(profile), "SELECT 1"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert not calls
