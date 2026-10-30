"""`olf openmetadata list-domains` against a fake OpenMetadata, with the cluster stubbed out."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator

import pytest
from conftest import FakeOpenMetadata
from typer.testing import CliRunner

from olf.cli import app
from olf.infra import access, k8s
from olf.infra.commands import runtime
from olf.infra.openmetadata import _auth

runner = CliRunner()

_DOMAINS = [
    {"name": "supply_chain", "displayName": "Supply Chain"},
    {"name": "sales", "displayName": "Sales"},
]


@pytest.fixture
def stage(monkeypatch: pytest.MonkeyPatch, fake_openmetadata: FakeOpenMetadata) -> FakeOpenMetadata:
    """A governance-enabled stage whose OpenMetadata is the fake server."""
    monkeypatch.setattr(runtime, "provider_contract_environment", lambda **_kwargs: contextlib.nullcontext())
    monkeypatch.setenv("OPENLAKEFORGE_GOVERNANCE_ENABLED", "true")
    monkeypatch.setenv("OPENLAKEFORGE_SHARED_NAMESPACE", "olf-shared")

    @contextlib.contextmanager
    def service_url(ref: str, **_kwargs: object) -> Iterator[str]:
        assert ref == "shared/governance_service"
        yield fake_openmetadata.url

    monkeypatch.setattr(access, "service_url", service_url)
    monkeypatch.setattr(_auth, "automation_token", lambda namespace, **_kwargs: fake_openmetadata.token)
    fake_openmetadata.domains = list(_DOMAINS)
    return fake_openmetadata


def test_lists_domains_sorted_by_name(stage: FakeOpenMetadata) -> None:
    result = runner.invoke(app, ["infra", "openmetadata", "list-domains"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "NAME          DISPLAY NAME\nsales         Sales\nsupply_chain  Supply Chain\n"


def test_json_output_is_only_the_domains(stage: FakeOpenMetadata) -> None:
    result = runner.invoke(app, ["infra", "openmetadata", "list-domains", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {"name": "sales", "displayName": "Sales"},
        {"name": "supply_chain", "displayName": "Supply Chain"},
    ]


def test_reads_every_page_and_never_writes(stage: FakeOpenMetadata) -> None:
    stage.domains = [{"name": f"domain_{index:03}", "displayName": f"Domain {index}"} for index in range(150)]

    result = runner.invoke(app, ["infra", "openmetadata", "list-domains", "--json"])

    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 150
    assert len(stage.requests) == 2
    assert {method for method, _path in stage.requests} == {"GET"}


def test_never_prints_the_token(stage: FakeOpenMetadata) -> None:
    stage.fail_status = 403

    result = runner.invoke(app, ["infra", "openmetadata", "list-domains"])

    assert stage.token not in result.output


def test_disabled_governance_layer_says_how_to_enable_it(
    stage: FakeOpenMetadata, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_GOVERNANCE_ENABLED", "false")

    result = runner.invoke(app, ["infra", "openmetadata", "list-domains"])

    assert result.exit_code == 1
    assert "governance layer is disabled" in result.output
    assert "preset: full" in result.output
    assert stage.requests == []


def test_unreachable_service_is_reported(stage: FakeOpenMetadata, monkeypatch: pytest.MonkeyPatch) -> None:
    @contextlib.contextmanager
    def unreachable(_ref: str, **_kwargs: object) -> Iterator[str]:
        raise k8s.KubectlError("kubectl port-forward exited before localhost:1 became ready.")
        yield ""

    monkeypatch.setattr(access, "service_url", unreachable)

    result = runner.invoke(app, ["infra", "openmetadata", "list-domains"])

    assert result.exit_code == 1
    assert "OpenMetadata is not reachable in namespace olf-shared" in result.output


def test_missing_automation_token_fails_without_falling_back_to_a_password(
    stage: FakeOpenMetadata, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_auth, "automation_token", lambda namespace, **_kwargs: None)

    result = runner.invoke(app, ["infra", "openmetadata", "list-domains"])

    assert result.exit_code == 1
    assert "openmetadata-automation-bot is missing from namespace olf-shared" in result.output
    assert stage.requests == []


def test_failed_response_is_reported(stage: FakeOpenMetadata) -> None:
    stage.fail_status = 500

    result = runner.invoke(app, ["infra", "openmetadata", "list-domains"])

    assert result.exit_code == 1
    assert "OpenMetadata did not list its domains" in result.output
