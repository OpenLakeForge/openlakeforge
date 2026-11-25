from __future__ import annotations

import json
from pathlib import Path

from olf.common.deployment.context import (
    DeploymentContext,
    alert_receiver_secrets_variables,
    observability_variables,
)
from olf.common.profile import (
    AlertReceiver,
    DeploymentProfile,
    ObservabilitySpec,
    Preset,
    Provider,
    ProviderSpec,
    SecretRef,
    StageName,
    StageSpec,
    resolve_topology,
)


def _variables(tmp_path: Path, preset: Preset, observability: ObservabilitySpec | None = None) -> dict[str, object]:
    profile = DeploymentProfile(
        name="obs",
        provider=ProviderSpec(type=Provider.LOCAL),
        preset=preset,
        stages=(StageSpec(name=StageName.DEV, enabled=True),),
        observability=observability,
    )
    context = DeploymentContext.local(repo_root=tmp_path, topology=resolve_topology(profile))
    return json.loads(observability_variables(context)["observability"])


def test_slim_reaches_the_roots_as_an_archive_only_input(tmp_path: Path) -> None:
    assert _variables(tmp_path, Preset.SLIM) == {
        "archive_retention_days": 30,
        "search_enabled": False,
        "metrics_enabled": False,
        "metrics_retention_days": 7,
        "grafana_enabled": False,
        "alert_receivers": [],
    }


def test_full_differs_from_slim_only_by_metrics(tmp_path: Path) -> None:
    slim = _variables(tmp_path, Preset.SLIM)
    full = _variables(tmp_path, Preset.FULL)

    assert {key for key in slim if slim[key] != full[key]} == {"metrics_enabled"}
    assert full["metrics_enabled"] is True


def test_no_secret_reference_reaches_the_input(tmp_path: Path) -> None:
    receiver = AlertReceiver(name="ops", type="webhook", secret_ref=SecretRef(name="ops-webhook", key="webhook-url"))
    variables = _variables(tmp_path, Preset.FULL, ObservabilitySpec(alert_receivers=(receiver,)))

    assert variables["alert_receivers"] == [{"name": "ops", "type": "webhook", "stages": ["dev"]}]
    serialized = json.dumps(variables)
    assert "ops-webhook" not in serialized
    assert "webhook-url" not in serialized


def _receiver_context(tmp_path: Path, receivers: tuple[AlertReceiver, ...]) -> DeploymentContext:
    profile = DeploymentProfile(
        name="obs",
        provider=ProviderSpec(type=Provider.LOCAL),
        preset=Preset.FULL,
        stages=(StageSpec(name=StageName.DEV, enabled=True),),
        observability=ObservabilitySpec(alert_receivers=receivers),
    )
    return DeploymentContext.local(repo_root=tmp_path, topology=resolve_topology(profile))


def test_secret_references_reach_the_local_root_by_name_and_key_only(tmp_path: Path) -> None:
    receivers = (
        AlertReceiver(name="ops", type="webhook", secret_ref=SecretRef(name="ops-webhook", key="webhook-url")),
        AlertReceiver(name="sec", type="webhook", secret_ref=SecretRef(name="sec-webhook", key="url")),
    )

    variables = alert_receiver_secrets_variables(_receiver_context(tmp_path, receivers))

    assert json.loads(variables["alert_receiver_secrets"]) == {
        "ops": {"secret_name": "ops-webhook", "key": "webhook-url", "platform": True},
        "sec": {"secret_name": "sec-webhook", "key": "url", "platform": True},
    }


def test_only_the_named_receivers_are_marked_for_platform_conditions(tmp_path: Path) -> None:
    receivers = (
        AlertReceiver(name="ops", type="webhook", secret_ref=SecretRef(name="ops-webhook", key="webhook-url")),
        AlertReceiver(name="chat", type="webhook", secret_ref=SecretRef(name="chat-webhook", key="url")),
    )
    profile = DeploymentProfile(
        name="p",
        provider=ProviderSpec(type="local"),
        preset=Preset.FULL,
        stages=(StageSpec(name=StageName.DEV, enabled=True),),
        observability=ObservabilitySpec(alert_receivers=receivers, alert_platform_receivers=("ops",)),
    )
    context = DeploymentContext.local(repo_root=tmp_path, topology=resolve_topology(profile))

    secrets = json.loads(alert_receiver_secrets_variables(context)["alert_receiver_secrets"])

    assert {name: entry["platform"] for name, entry in secrets.items()} == {"ops": True, "chat": False}


def test_no_receivers_emit_no_secret_variable(tmp_path: Path) -> None:
    assert alert_receiver_secrets_variables(_receiver_context(tmp_path, ())) == {}


def test_a_github_discussions_receiver_has_no_secret_reference_to_pass(tmp_path: Path) -> None:
    receivers = (
        AlertReceiver(name="ops", type="webhook", secret_ref=SecretRef(name="ops-webhook", key="webhook-url")),
        AlertReceiver(name="team", type="github-discussions", repository="my-org/olf-alerts", category="Alerts"),
    )

    variables = alert_receiver_secrets_variables(_receiver_context(tmp_path, receivers))

    assert json.loads(variables["alert_receiver_secrets"]) == {
        "ops": {"secret_name": "ops-webhook", "key": "webhook-url", "platform": True},
    }


def test_only_github_discussions_receivers_emit_no_secret_variable(tmp_path: Path) -> None:
    receivers = (AlertReceiver(name="team", type="github-discussions", repository="o/r", category="Alerts"),)

    assert alert_receiver_secrets_variables(_receiver_context(tmp_path, receivers)) == {}
