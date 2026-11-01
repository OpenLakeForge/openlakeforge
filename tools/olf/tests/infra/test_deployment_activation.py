from __future__ import annotations

import base64
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from openlakeforge_domain import load_lakehouse_inventory
from openlakeforge_services.dagster import DagsterClient

from olf.common.deployment.context import DeploymentContext
from olf.common.distribution import distribution_version_at
from olf.common.profile import Provider, StageName, resolve_topology, validate_deployment_profile
from olf.common.project import ProjectSpec
from olf.common.provider_contracts import CodeLocation
from olf.infra import project_activation, project_revision
from olf.infra.artifact_store import FilesystemRevisionStore
from olf.infra.deployment import activation as activation_module

FIXTURES = Path(__file__).parents[1] / "fixtures"
ROOT = Path(__file__).resolve().parents[4]
_IMAGE = "ghcr.io/openlakeforge/project-code@sha256:" + "a" * 64
_DEFAULT_LOCATIONS = (CodeLocation(name="openlakeforge-dagster", definitions_module="lakehouse_code.definitions"),)


def _contract() -> dict:
    return json.loads((FIXTURES / "local-provider-contracts-v3.json").read_text())


def _topology(contract: dict):
    """Both stages enabled, with each stage's capabilities read from the contract itself."""
    stages = {
        name: {
            "enabled": True,
            "capabilities": {"analytics": "reporting" in stage, "governance": "governance" in stage},
        }
        for name, stage in contract["stages"].items()
    }
    return resolve_topology(
        validate_deployment_profile(
            {
                "apiVersion": "openlakeforge.io/v1alpha1",
                "kind": "DeploymentProfile",
                "metadata": {"name": contract["deployment"]["profile_name"]},
                "spec": {"provider": {"type": "local"}, "preset": "full", "stages": stages, "access": {}},
            }
        )
    )


class _Helm:
    """Records rollouts and reports readiness per namespace, as Helm does per release."""

    def __init__(self) -> None:
        self.rollouts: list[tuple[str, dict]] = []
        self.uninstalled: list[str] = []
        self.ready: set[str] = set()
        self.installed: dict[str, dict] = {}
        self.secret_suffix = "creds"
        self.fails = False

    def status(self, release: str, *, namespace: str, kube_context=None, env=None):  # noqa: ANN001, ANN202, ARG002
        return SimpleNamespace(ok=namespace in self.ready)

    def platform_globals(self, namespace: str) -> dict[str, str]:
        return {"postgresqlSecretName": f"postgresql-dagster-{namespace}-{self.secret_suffix}"}

    def get_values(self, release, *, namespace, kube_context=None, env=None):  # noqa: ANN001, ANN202, ARG002
        if release == "dagster":
            # The platform release the activation inherits its globals from.
            return SimpleNamespace(
                ok=True,
                stdout=json.dumps({"global": self.platform_globals(namespace)}),
            )
        installed = self.installed.get(namespace)
        return SimpleNamespace(ok=installed is not None, stdout=json.dumps(installed or {}))

    def upgrade_install(self, release, chart, *, namespace, values, kube_context=None, env=None):  # noqa: ANN001, ANN202, ARG002
        if self.fails:
            raise RuntimeError("helm upgrade --atomic rolled back")
        rendered = yaml.safe_load(values.read_text())
        self.rollouts.append((namespace, rendered))
        self.installed[namespace] = rendered
        self.ready.add(namespace)

    def uninstall(self, release, *, namespace, kube_context=None, env=None) -> None:  # noqa: ANN001, ARG002
        self.uninstalled.append(namespace)
        self.installed.pop(namespace, None)
        self.ready.discard(namespace)


class _Kubectl:
    """A stage namespace holding no source bindings, and the pull Secrets a test adds."""

    def __init__(self) -> None:
        self.secrets: dict[str, dict] = {}
        self.account_secrets: list[str] = []

    def get(self, kind, *, name, namespace, output, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG002
        if kind == "serviceaccount":
            return SimpleNamespace(ok=True, stdout=" ".join(self.account_secrets))
        if kind == "secret" and name in self.secrets:
            return SimpleNamespace(ok=True, stdout=json.dumps(self.secrets[name]))
        return SimpleNamespace(ok=False, stdout="", stderr=f"{kind} {name!r} not found")

    def patch(self, kind, name, patch, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG002
        assert (kind, name) == ("serviceaccount", "dagster")
        self.account_secrets = [item["name"] for item in json.loads(patch)["imagePullSecrets"]]


class _Webserver:
    """A stage's Dagster webserver: a reload picks up what the code server now serves.

    Answers the GraphQL the real client sends, so the client's own parsing is
    part of what is exercised. `stale` stands in for a webserver that keeps
    reporting another image however often it is reloaded.
    """

    def __init__(self, helm: _Helm) -> None:
        self.helm = helm
        self.reloads: list[tuple[str, str]] = []
        self.stale: str | None = None
        self.namespace = ""

    def served(self, namespace: str, location: str) -> str | None:
        for deployment in (self.helm.installed.get(namespace) or {}).get("deployments") or []:
            if deployment["name"] == location:
                image = deployment["image"]
                return f"{image['repository']}@{image['digest']}"
        return None

    def client(self, namespace: str) -> DagsterClient:
        def request_json(query: str, variables=None):  # noqa: ANN001, ANN202
            assert "reloadRepositoryLocation" in query
            name = (variables or {})["repositoryLocationName"]
            self.reloads.append((namespace, name))
            image = self.stale or self.served(namespace, name)
            if image is None:
                entry = {"__typename": "RepositoryLocationNotFound", "message": f"{name} not found"}
            else:
                entry = {
                    "__typename": "WorkspaceLocationEntry",
                    "displayMetadata": [{"key": "image", "value": image}, {"key": "host", "value": name}],
                    "locationOrLoadError": {"__typename": "RepositoryLocation"},
                }
            return {"data": {"reloadRepositoryLocation": entry}}

        return DagsterClient("http://dagster/graphql", request_json=request_json)


def _install_webserver(monkeypatch: pytest.MonkeyPatch, helm: _Helm) -> _Webserver:
    webserver = _Webserver(helm)

    @contextmanager
    def _stage_webserver(namespace, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        yield webserver.client(namespace)

    monkeypatch.setattr(activation_module, "_stage_webserver", _stage_webserver)
    monkeypatch.setattr(activation_module, "_LOCATION_RELOAD_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(activation_module, "_LOCATION_RELOAD_DELAY_SECONDS", 0.0)
    return webserver


class _Provider:
    def __init__(self, context: DeploymentContext, helm: _Helm, kubectl: _Kubectl | None = None) -> None:
        self.context = context
        # As DeploymentContext.environment() does for a real provider.
        self.env: dict[str, str] = {"OLF_DISTRIBUTION_ROOT": str(context.paths.distribution_root)}
        self.tools = SimpleNamespace(helm=helm, kubectl=kubectl or _Kubectl())
        self.config = SimpleNamespace(
            charts={"dagster": SimpleNamespace(package_path=Path("unused.tgz"))},
            paths=context.paths,
            terraform=SimpleNamespace(apply_retry=None),
        )


@pytest.fixture
def harness(external_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """A published revision plus an activation path with its I/O collaborators stubbed.

    Image pulls, chart preparation and the contract environment all reach a
    live cluster; the decisions under test -- what gets rolled out,
    when a redeploy is skipped, and what the active pointer ends up at -- do not.
    """
    contract = _contract()
    topology = _topology(contract)
    store = FilesystemRevisionStore(tmp_path / "store")
    spec = ProjectSpec(root=external_project, distribution_root=ROOT)
    manifest = project_revision.build_project_revision(
        spec, image=_IMAGE, distribution_version=distribution_version_at(ROOT)
    )
    project_revision.publish(store, manifest, spec)

    helm = _Helm()
    kubectl = _Kubectl()
    webserver = _install_webserver(monkeypatch, helm)
    catalog_calls: list[str] = []

    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda *a, **k: contract)
    image_calls: list[str] = []
    monkeypatch.setattr(
        activation_module, "_ensure_image", lambda provider, image, **k: image_calls.append(image)
    )
    monkeypatch.setattr(activation_module, "prepare_chart", lambda *a, **k: None)
    monkeypatch.setattr(activation_module, "deploy_optional_layer_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(activation_module, "_user_chart", lambda chart, work_root: work_root)
    monkeypatch.setattr(activation_module, "sync_catalog_namespaces", lambda: catalog_calls.append("sync"))

    @contextmanager
    def _contract_environment(**kwargs):  # noqa: ANN003, ANN202
        yield {"OPENLAKEFORGE_KUBE_NAMESPACE": kwargs["namespace"]}

    monkeypatch.setattr(activation_module.contract_env, "applied_contract_environment", _contract_environment)

    providers: list[_Provider] = []

    def deploy(stage: str, pull_secret: str | None = None):  # noqa: ANN202
        context = DeploymentContext.local(
            repo_root=external_project,
            distribution_root=ROOT,
            topology=topology,
            stage=stage,
            work_root=tmp_path / "work",
        )
        context.paths.work_root.mkdir(parents=True, exist_ok=True)
        provider = _Provider(context, helm, kubectl)
        providers.append(provider)
        return activation_module.deploy_revision(
            provider, revision=manifest.revision, store=store, profile_name="acceptance", pull_secret=pull_secret
        )

    return SimpleNamespace(
        deploy=deploy,
        contract=contract,
        store=store,
        helm=helm,
        kubectl=kubectl,
        webserver=webserver,
        manifest=manifest,
        image_calls=image_calls,
        providers=providers,
        catalog_calls=catalog_calls,
    )


def _contract_with_reporting() -> dict:
    """The default fixture contract has no `reporting` binding on any stage
    (`test_analytics_disabled_stage_skips_reports_without_failing_activation`
    below exercises that default directly) -- report-import tests need a
    stage whose contract actually resolves a Superset/Trino target."""
    contract = _contract()
    for name in ("dev", "prod"):
        stage = contract["stages"][name]
        stage["reporting"] = {
            "service_ref": f"stage/{name}/reporting",
            "endpoint_ref": f"stage/{name}/endpoints/reporting",
        }
        stage["endpoints"]["reporting"] = f"stage/{name}/endpoints/reporting"
    return contract


_MUTATION_MARKER = "MUTATED AFTER PUBLISH -- must never reach Superset"


def _build_report_harness(
    contract: dict, *, external_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Publish before mutating the checkout so its report bundle differs from the revision."""
    monkeypatch.setenv("OPENLAKEFORGE_REPO_ROOT", str(external_project))
    monkeypatch.setenv("OPENLAKEFORGE_PROJECT_ROOT", str(external_project))

    topology = _topology(contract)
    store = FilesystemRevisionStore(tmp_path / "store")
    spec = ProjectSpec(root=external_project, distribution_root=ROOT)
    manifest = project_revision.build_project_revision(
        spec, image=_IMAGE, distribution_version=distribution_version_at(ROOT)
    )
    project_revision.publish(store, manifest, spec)

    inventory = load_lakehouse_inventory(external_project)
    expected_dashboard_dirs = sorted(Path(dashboard.report_source_dir).name for dashboard in inventory.dashboards)
    mutation_target = next(
        path.relative_to(external_project)
        for dashboard in inventory.dashboards
        for path in sorted((external_project / dashboard.report_source_dir / "dashboards").glob("*.yaml"))
    )
    target = external_project / mutation_target
    document = yaml.safe_load(target.read_text())
    original_description = document["description"]
    document["description"] = _MUTATION_MARKER
    target.write_text(yaml.safe_dump(document, sort_keys=False))

    helm = _Helm()
    _install_webserver(monkeypatch, helm)

    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda *a, **k: contract)
    monkeypatch.setattr(activation_module, "_ensure_image", lambda provider, image, **k: None)
    monkeypatch.setattr(activation_module, "prepare_chart", lambda *a, **k: None)
    monkeypatch.setattr(activation_module, "_user_chart", lambda chart, work_root: work_root)
    monkeypatch.setattr(activation_module, "sync_catalog_namespaces", lambda: None)

    import olf.infra.commands.openmetadata as openmetadata_commands
    import olf.infra.superset as superset_module

    report_calls: list[dict] = []

    def _fake_deploy_reports(repo_root, namespace, sqlalchemy_uri, **kwargs):  # noqa: ANN001, ANN202
        # `repo_root` is a scratch directory that `deploy_revision`'s own
        # `TemporaryDirectory` removes once this call returns, so what the
        # bundle actually contained has to be captured now, not after.
        bundle_dirs = sorted(
            path.name for path in Path(repo_root).glob("lakehouse_code/dashboards/superset/*") if path.is_dir()
        )
        dashboard_file = Path(repo_root) / mutation_target
        description = yaml.safe_load(dashboard_file.read_text())["description"] if dashboard_file.is_file() else None
        report_calls.append(
            {
                "repo_root": Path(repo_root),
                "namespace": namespace,
                "sqlalchemy_uri": sqlalchemy_uri,
                "bundle_dirs": bundle_dirs,
                "dashboard_description": description,
                **kwargs,
            }
        )

    monkeypatch.setattr(superset_module, "deploy_reports", _fake_deploy_reports)
    metadata_calls: list[None] = []
    metadata_allow_missing: list[str | None] = []

    def _fake_deploy_metadata() -> None:
        metadata_calls.append(None)
        metadata_allow_missing.append(os.environ.get("OPENMETADATA_ALLOW_MISSING_ASSETS"))

    monkeypatch.setattr(openmetadata_commands, "deploy_openmetadata_metadata", _fake_deploy_metadata)

    def deploy(stage: str):  # noqa: ANN202
        context = DeploymentContext.local(
            repo_root=external_project,
            distribution_root=ROOT,
            topology=topology,
            stage=stage,
            work_root=tmp_path / "work",
        )
        context.paths.work_root.mkdir(parents=True, exist_ok=True)
        provider = _Provider(context, helm)
        return activation_module.deploy_revision(
            provider, revision=manifest.revision, store=store, profile_name="acceptance"
        )

    return SimpleNamespace(
        deploy=deploy,
        store=store,
        helm=helm,
        manifest=manifest,
        report_calls=report_calls,
        metadata_calls=metadata_calls,
        metadata_allow_missing=metadata_allow_missing,
        external_project=external_project,
        expected_dashboard_dirs=expected_dashboard_dirs,
        original_dashboard_description=original_description,
    )


@pytest.fixture
def report_harness(external_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """`_build_report_harness` against a contract with `reporting` enabled for dev/prod."""
    return _build_report_harness(
        _contract_with_reporting(), external_project=external_project, tmp_path=tmp_path, monkeypatch=monkeypatch
    )


def test_activating_a_revision_imports_the_frozen_reports_not_the_checkout(report_harness) -> None:  # noqa: ANN001
    report_harness.deploy("prod")

    assert len(report_harness.report_calls) == 1
    call = report_harness.report_calls[0]
    assert call["bundle_dirs"] == report_harness.expected_dashboard_dirs
    # The frozen revision's content, not the checkout's -- which was mutated
    # to a different description *after* the revision was published.
    assert call["dashboard_description"] == report_harness.original_dashboard_description
    assert call["dashboard_description"] != _MUTATION_MARKER


def test_activating_a_revision_imports_into_the_activated_stages_superset(report_harness) -> None:  # noqa: ANN001
    report_harness.deploy("dev")
    report_harness.deploy("prod")

    assert [call["namespace"] for call in report_harness.report_calls] == ["olf-dev", "olf-prod"]
    # Each stage's own Gold connection and runtime principal -- not merely
    # "different from each other", which a DEV/PROD swap would also satisfy.
    assert [call["sqlalchemy_uri"] for call in report_harness.report_calls] == [
        "trino://olf-dev-runtime@trino:8080/lakehouse_dev",
        "trino://olf-prod-runtime@trino:8080/lakehouse_prod",
    ]


def test_reactivating_the_same_revision_does_not_reimport_reports(report_harness) -> None:  # noqa: ANN001
    """Re-promoting an already-active revision imports its reports once, not
    twice -- promotion is replayed routinely, and a second import is how
    dashboards get duplicated."""
    report_harness.deploy("prod")
    report_harness.deploy("prod")

    assert len(report_harness.report_calls) == 1


def test_governance_enabled_analytics_disabled_stage_skips_reports_without_failing_activation(
    external_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Governance still dispatches metadata while the disabled analytics layer skips reports."""
    contract = _contract()
    contract["shared"]["governance_service"] = {
        "ref": "shared/governance_service",
        "implementation": "governance.openmetadata",
    }
    stage = contract["stages"]["prod"]
    stage["governance"] = {
        "service_ref": "shared/governance_service",
        "endpoint_ref": "stage/prod/endpoints/governance",
    }
    stage["endpoints"]["governance"] = "stage/prod/endpoints/governance"
    disabled = _build_report_harness(
        contract, external_project=external_project, tmp_path=tmp_path, monkeypatch=monkeypatch
    )

    activation = disabled.deploy("prod")

    assert activation is not None
    assert disabled.report_calls == []
    assert disabled.metadata_calls == [None]


def _governed_contract() -> dict:
    contract = _contract()
    contract["shared"]["governance_service"] = {
        "ref": "shared/governance_service",
        "implementation": "governance.openmetadata",
    }
    stage = contract["stages"]["prod"]
    stage["governance"] = {
        "service_ref": "shared/governance_service",
        "endpoint_ref": "stage/prod/endpoints/governance",
    }
    stage["endpoints"]["governance"] = "stage/prod/endpoints/governance"
    return contract


def test_a_first_activation_deploys_metadata_before_the_gold_tables_exist(
    external_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activation runs before any pipeline, so empty Gold schemas are reported rather than fatal."""
    monkeypatch.delenv("OPENMETADATA_ALLOW_MISSING_ASSETS", raising=False)
    harness = _build_report_harness(
        _governed_contract(), external_project=external_project, tmp_path=tmp_path, monkeypatch=monkeypatch
    )

    harness.deploy("prod")

    assert harness.metadata_allow_missing == ["true"]


def test_an_operator_can_still_require_the_gold_tables_at_activation(
    external_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENMETADATA_ALLOW_MISSING_ASSETS", "false")
    harness = _build_report_harness(
        _governed_contract(), external_project=external_project, tmp_path=tmp_path, monkeypatch=monkeypatch
    )

    harness.deploy("prod")

    assert harness.metadata_allow_missing == ["false"]


def test_reapplying_the_active_revision_changes_nothing(harness) -> None:  # noqa: ANN001
    first = harness.deploy("dev")
    second = harness.deploy("dev")

    assert second == first
    assert len(harness.helm.rollouts) == 1
    # The point of the early gate: a no-op redeploy must not reconcile catalog
    # namespaces just to discover it had nothing to do.
    assert len(harness.catalog_calls) == 1
    # Pulling the image (and on local, reloading it into kind) is a cluster
    # mutation, so it belongs after the gate, not before it.
    assert len(harness.image_calls) == 1


def test_activation_reloads_every_contracted_location_in_the_stage_webserver(harness) -> None:  # noqa: ANN001
    """A webserver restarted before the rollout keeps the previous snapshot until reloaded (#740)."""
    harness.contract["stages"]["dev"]["orchestration"]["code_locations"] = [
        {"name": "sales-dagster", "definitions_module": "lakehouse_code.sales"},
        {"name": "food-dagster", "definitions_module": "lakehouse_code.food"},
    ]

    activation = harness.deploy("dev")

    assert sorted(harness.webserver.reloads) == [("olf-dev", "food-dagster"), ("olf-dev", "sales-dagster")]
    assert project_activation.active(harness.store, stage=StageName.DEV) == activation


def test_a_webserver_still_serving_the_previous_image_fails_the_activation(harness) -> None:  # noqa: ANN001
    stale = "ghcr.io/openlakeforge/project-code@sha256:" + "f" * 64
    harness.webserver.stale = stale

    with pytest.raises(activation_module.ActivationError, match=r"did not serve .*openlakeforge-dagster: serves image"):
        harness.deploy("dev")

    # Nothing names an activation whose runs would launch another image, and
    # the release that would have served it is not left behind.
    assert project_activation.active(harness.store, stage=StageName.DEV) is None
    assert harness.helm.uninstalled == ["olf-dev"]


def test_a_stale_webserver_restores_the_previous_activation(harness) -> None:  # noqa: ANN001
    active = harness.deploy("dev")
    drifted = harness.helm.installed["olf-dev"]
    drifted["deployments"][0]["deploymentLabels"]["openlakeforge.io/activation-revision"] = "sha256:" + "9" * 64
    harness.webserver.stale = "ghcr.io/openlakeforge/project-code@sha256:" + "f" * 64

    with pytest.raises(activation_module.ActivationError):
        harness.deploy("dev")

    assert project_activation.active(harness.store, stage=StageName.DEV) == active
    labels = harness.helm.installed["olf-dev"]["deployments"][0]["deploymentLabels"]
    assert labels["openlakeforge.io/activation-revision"] == active.activation_revision


def test_promoting_one_revision_to_another_stage_reuses_the_project_revision(harness) -> None:  # noqa: ANN001
    dev = harness.deploy("dev")
    prod = harness.deploy("prod")

    assert dev.project_revision == prod.project_revision == harness.manifest.revision
    assert dev.activation_revision != prod.activation_revision
    assert project_activation.active(harness.store, stage=StageName.DEV) == dev
    assert project_activation.active(harness.store, stage=StageName.PROD) == prod
    assert {namespace for namespace, _ in harness.helm.rollouts} == {"olf-dev", "olf-prod"}


def test_failed_rollout_leaves_the_previous_revision_active(harness) -> None:  # noqa: ANN001
    active = harness.deploy("dev")
    harness.helm.fails = True

    with pytest.raises(RuntimeError):
        harness.deploy("prod")

    assert project_activation.active(harness.store, stage=StageName.DEV) == active
    assert project_activation.active(harness.store, stage=StageName.PROD) is None


def test_a_pointer_that_will_not_commit_uninstalls_a_first_activation(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """The release is live before ACTIVE.json moves. If the pointer never lands,
    a stage with no previous activation must not be left running an unreferenced one."""
    monkeypatch.setattr(
        activation_module,
        "commit_active",
        lambda *a, **k: (_ for _ in ()).throw(project_activation.ProjectActivationError("pointer write failed")),
    )

    with pytest.raises(project_activation.ProjectActivationError):
        harness.deploy("dev")

    assert harness.helm.uninstalled == ["olf-dev"]
    assert project_activation.active(harness.store, stage=StageName.DEV) is None


def test_a_pointer_that_will_not_commit_restores_the_previous_activation(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """With an activation already live, the rollback reinstalls it rather than
    uninstalling the stage."""
    active = harness.deploy("dev")
    monkeypatch.setattr(
        activation_module,
        "commit_active",
        lambda *a, **k: (_ for _ in ()).throw(project_activation.ProjectActivationError("pointer write failed")),
    )
    # A drifted release is what makes the same activation roll out again.
    drifted = harness.helm.installed["olf-dev"]
    drifted["deployments"][0]["deploymentLabels"]["openlakeforge.io/activation-revision"] = "sha256:" + "9" * 64

    with pytest.raises(project_activation.ProjectActivationError):
        harness.deploy("dev")

    assert harness.helm.uninstalled == []
    assert project_activation.active(harness.store, stage=StageName.DEV) == active
    labels = harness.helm.installed["olf-dev"]["deployments"][0]["deploymentLabels"]
    assert labels["openlakeforge.io/activation-revision"] == active.activation_revision


def test_a_pre_floe_08_activation_is_not_restored_when_the_pointer_will_not_commit(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """A schema-1 activation ran Floe 0.6 from published manifests the platform
    no longer serves; restoring it would install a code location that cannot
    load, so the healthy new release stays and the deploy can be rerun."""
    import dataclasses

    current = harness.deploy("dev")
    schema_1 = {"floe_manifest_revision": "sha256:" + "e" * 64, "activation_revision": ""}
    legacy = dataclasses.replace(current, **schema_1).resolved()
    project_activation.publish(harness.store, legacy)
    project_activation.commit_active(harness.store, legacy)
    monkeypatch.setattr(
        activation_module,
        "commit_active",
        lambda *a, **k: (_ for _ in ()).throw(project_activation.ProjectActivationError("pointer write failed")),
    )

    with pytest.raises(project_activation.ProjectActivationError, match="predates Floe 0.8"):
        harness.deploy("dev")

    assert len(harness.helm.rollouts) == 2  # the first deploy and this one; no rollback rollout
    assert harness.helm.uninstalled == []
    labels = harness.helm.installed["olf-dev"]["deployments"][0]["deploymentLabels"]
    assert labels["openlakeforge.io/activation-revision"] == current.activation_revision
    assert project_activation.active(harness.store, stage=StageName.DEV) == legacy


def test_rollout_inherits_the_platform_release_globals(harness) -> None:  # noqa: ANN001
    """The subchart installed standalone would otherwise default to a secret name no stage uses."""
    harness.deploy("prod")

    _, values = harness.helm.rollouts[0]

    assert values["global"]["postgresqlSecretName"] == "postgresql-dagster-olf-prod-creds"


def test_rollout_carries_the_activated_image_into_the_log_archiver(harness) -> None:  # noqa: ANN001
    activation = harness.deploy("dev")

    _, values = harness.helm.rollouts[0]
    archiver = values["extraManifests"][0]
    container = archiver["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]

    assert archiver["kind"] == "CronJob"
    assert container["image"] == activation.project_code_image
    assert archiver["metadata"]["namespace"] == "olf-dev"


def test_reapplying_repairs_a_release_that_drifted_to_another_activation(harness) -> None:  # noqa: ANN001
    """A healthy-but-wrong release must not be mistaken for an idempotent no-op."""
    activation = harness.deploy("dev")
    drifted = harness.helm.installed["olf-dev"]
    drifted["deployments"][0]["deploymentLabels"]["openlakeforge.io/activation-revision"] = "sha256:" + "9" * 64

    reapplied = harness.deploy("dev")

    assert reapplied == activation
    assert len(harness.helm.rollouts) == 2


def test_rollout_keeps_contract_runtime_aliases(external_project: Path, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Non-prefixed aliases reach the pod: libs.s3_artifacts reads AWS_ENDPOINT_URL_S3 itself."""
    from olf.infra.deployment.activation import _user_values
    from olf.infra.project_activation import ProjectActivation

    activation = ProjectActivation(
        deployment_profile="acceptance",
        provider="local",
        stage=StageName.DEV,
        project_name="demo",
        project_revision="sha256:" + "b" * 64,
        distribution_version="0.0.0",
        project_code_image=_IMAGE,
        provider_binding_digest="sha256:" + "d" * 64,
        capabilities={"analytics": False, "governance": False},
    ).resolved()

    values = _user_values(
        activation,
        contract_environ={
            "AWS_ENDPOINT_URL_S3": "http://seaweedfs-s3.olf-system:8333",
            "AWS_REGION": "us-east-1",
            "AWS_SECRET_ACCESS_KEY": "must-not-appear",
            "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET": "lakehouse-bronze",
        },
        namespace="olf-dev",
        platform_globals={},
        code_locations=_DEFAULT_LOCATIONS,
    )

    env = {entry["name"]: entry["value"] for entry in values["deployments"][0]["env"]}
    assert env["AWS_ENDPOINT_URL_S3"] == "http://seaweedfs-s3.olf-system:8333"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"] == "lakehouse-bronze"
    # Credential values travel by Secret reference, never inline.
    assert "AWS_SECRET_ACCESS_KEY" not in env


def _values(contract_extra: dict[str, str] | None = None, **capabilities: bool) -> dict:
    from olf.infra.deployment.activation import _user_values
    from olf.infra.project_activation import ProjectActivation

    activation = ProjectActivation(
        deployment_profile="acceptance",
        provider="local",
        stage=StageName.DEV,
        project_name="demo",
        project_revision="sha256:" + "b" * 64,
        distribution_version="0.0.0",
        project_code_image=_IMAGE,
        provider_binding_digest="sha256:" + "d" * 64,
        capabilities={"analytics": False, "governance": False, **capabilities},
    ).resolved()
    return _user_values(
        activation,
        contract_environ={
            "OPENLAKEFORGE_LOG_BASE_URI": "s3://ops/activations/dev/logs",
            "AWS_ENDPOINT_URL_S3": "http://seaweedfs-s3.olf-system:8333",
            "OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME": "seaweedfs-s3-creds",
            "OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME": "polaris-floe-creds",
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME": "openmetadata-ingestion-bot",
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY": "OPENMETADATA_INGESTION_BOT_JWT",
            **(contract_extra or {}),
        },
        namespace="olf-dev",
        platform_globals={},
        code_locations=_DEFAULT_LOCATIONS,
    )


def test_only_runtime_settings_reach_the_code_server() -> None:
    """The operator's shell and deployer-only settings stay on the workstation (#546)."""
    env = {
        entry["name"]: entry.get("value")
        for entry in _values(
            {
                "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev",
                "OPENLAKEFORGE_REPO_ROOT": "/home/operator/project",
                "OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX": "/tmp/olf-port-forward",
                "OPENLAKEFORGE_SHARED_NAMESPACE": "olf-system",
                "OPENLAKEFORGE_QUERY_TRINO_CATALOG": "iceberg",
                "OPENLAKEFORGE_STAGE": "prod",
            }
        )["deployments"][0]["env"]
    }

    assert env["OPENLAKEFORGE_CATALOG_NAME"] == "lakehouse_dev"
    assert env["OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME"] == "seaweedfs-s3-creds"
    assert not {
        "OPENLAKEFORGE_REPO_ROOT",
        "OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX",
        "OPENLAKEFORGE_SHARED_NAMESPACE",
        "OPENLAKEFORGE_QUERY_TRINO_CATALOG",
        "OPENLAKEFORGE_LOG_BASE_URI",
    } & set(env)
    # The activation's own stage, not one left in the operator's shell: it
    # decides whether the PROD-only schedules exist.
    assert env["OPENLAKEFORGE_STAGE"] == "dev"


# Runtime reads that the stage contract does not supply, and so are not in
# `_RUNTIME_ENV`, with where each comes from instead.
_RUNTIME_NAMES_SET_ELSEWHERE = {
    # Set by activation itself (`_user_values`).
    "OPENLAKEFORGE_KUBE_NAMESPACE": "activation",
    "OPENLAKEFORGE_PROJECT_CODE_IMAGE": "activation",
    "OPENLAKEFORGE_STAGE": "activation",
    "OPENLAKEFORGE_QUERY_TRINO_HTTP_SCHEME": "activation, only with the Trino CA",
    "OPENLAKEFORGE_QUERY_TRINO_CA": "activation, only with the Trino CA",
    "OPENLAKEFORGE_DBT_TRINO_METHOD": "activation, only with a Trino credential",
    "OPENLAKEFORGE_DBT_TRINO_PASSWORD": "activation, by secretKeyRef",
    # libs.k8s_log_archive runs in its own CronJob (`_archive_env`).
    "OPENLAKEFORGE_LOG_BASE_URI": "log archiver",
    "OPENLAKEFORGE_LOG_ARCHIVE_SINCE_SECONDS": "log archiver",
    "OPENLAKEFORGE_DBT_EXECUTABLE": "Terraform runtime env",
    # libs/floe_profile.py falls back to a default; no contract emits these yet.
    "OPENLAKEFORGE_FLOE_LINEAGE_DATASET_NAMESPACE": "unset, default applies",
    "OPENLAKEFORGE_FLOE_SERVICE_ACCOUNT": "unset, default applies",
}


def _names_runtime_code_reads() -> set[str]:
    import re

    pattern = re.compile(r"OPENLAKEFORGE_[A-Z0-9_]+")
    roots = [ROOT / "libs", ROOT / "lakehouse_code", ROOT / "packages/domain-model/openlakeforge_domain"]
    return {
        name
        for root in roots
        for path in root.rglob("*")
        if path.suffix in {".py", ".yml", ".yaml", ".sql"} and "dbt_packages" not in path.parts
        for name in pattern.findall(path.read_text(encoding="utf-8"))
    }


def test_every_name_the_runtime_reads_has_a_source() -> None:
    """A new env read in runtime code must be forwarded or say why not."""
    from olf.infra.deployment.activation import _RUNTIME_ENV

    unaccounted = _names_runtime_code_reads() - set(_RUNTIME_ENV) - set(_RUNTIME_NAMES_SET_ELSEWHERE)

    assert not unaccounted, f"add these to _RUNTIME_ENV or _RUNTIME_NAMES_SET_ELSEWHERE: {sorted(unaccounted)}"


def test_the_runtime_allowlist_names_only_what_runtime_code_reads() -> None:
    from olf.infra.deployment.activation import _RUNTIME_ENV

    assert not set(_RUNTIME_ENV) - _names_runtime_code_reads()


def test_trino_https_reaches_the_code_server_with_the_ca_volume() -> None:
    values = _values(
        {"OPENLAKEFORGE_QUERY_TRINO_TLS_PORT": "8443", "OPENLAKEFORGE_QUERY_TRINO_CA_CONFIGMAP": "olf-local-ca"}
    )
    deployment = values["deployments"][0]
    env = {e["name"]: e.get("value") for e in deployment["env"]}

    assert env["OPENLAKEFORGE_QUERY_TRINO_HTTP_SCHEME"] == "https"
    assert env["OPENLAKEFORGE_QUERY_TRINO_TLS_PORT"] == "8443"
    ca = env["OPENLAKEFORGE_QUERY_TRINO_CA"]
    assert deployment["volumes"] == [{"name": "query-ca", "configMap": {"name": "olf-local-ca"}}]
    (mount,) = deployment["volumeMounts"]
    assert ca == f"{mount['mountPath']}/ca.crt"


def test_without_trino_https_the_code_server_gets_no_ca_volume_or_https_env() -> None:
    deployment = _values()["deployments"][0]

    assert "volumes" not in deployment
    assert "volumeMounts" not in deployment
    names = {e["name"] for e in deployment["env"]}
    assert not names & {"OPENLAKEFORGE_QUERY_TRINO_HTTP_SCHEME", "OPENLAKEFORGE_QUERY_TRINO_CA"}


def test_governed_stage_maps_the_bot_jwt_to_the_name_openlineage_reads() -> None:
    """Mounting the Secret alone would expose its own key name, not OPENLINEAGE_API_KEY."""
    entry = next(e for e in _values(governance=True)["deployments"][0]["env"] if e["name"] == "OPENLINEAGE_API_KEY")

    assert entry["valueFrom"]["secretKeyRef"] == {
        "name": "openmetadata-ingestion-bot",
        "key": "OPENMETADATA_INGESTION_BOT_JWT",
    }


def test_ungoverned_stage_maps_no_lineage_key() -> None:
    assert not [e for e in _values()["deployments"][0]["env"] if e["name"] == "OPENLINEAGE_API_KEY"]


def test_log_archiver_only_receives_storage_credentials() -> None:
    """It reads the object store and nothing else, so it should reach nothing else."""
    values = _values(governance=True)
    archiver = values["extraManifests"][0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]

    assert [ref["secretRef"]["name"] for ref in archiver["envFrom"]] == ["seaweedfs-s3-creds"]
    # The governance JWT reaches the code server, never the log container.
    archive_env = {entry["name"] for entry in archiver["env"]}
    assert "OPENLINEAGE_API_KEY" not in archive_env
    assert archive_env == {
        "OPENLAKEFORGE_LOG_BASE_URI",
        "AWS_ENDPOINT_URL_S3",
        "OPENLAKEFORGE_KUBE_NAMESPACE",
        "OPENLAKEFORGE_LOG_ARCHIVE_SINCE_SECONDS",
    }
    # The code server still gets the full set it needs.
    assert {s["name"] for s in values["deployments"][0]["envSecrets"]} == {
        "seaweedfs-s3-creds",
        "polaris-floe-creds",
        "openmetadata-ingestion-bot",
    }


def test_reapplying_reconciles_globals_the_platform_rebound(harness) -> None:  # noqa: ANN001
    """A platform apply can rename the credentials Secret without moving any activation input."""
    activation = harness.deploy("dev")
    installed = harness.helm.installed["olf-dev"]
    assert installed["global"] == {"postgresqlSecretName": "postgresql-dagster-olf-dev-creds"}

    harness.helm.secret_suffix = "rotated"
    reapplied = harness.deploy("dev")

    assert reapplied == activation
    assert len(harness.helm.rollouts) == 2
    assert harness.helm.installed["olf-dev"]["global"] == {
        "postgresqlSecretName": "postgresql-dagster-olf-dev-rotated"
    }


@pytest.mark.parametrize(
    ("repository", "expected"),
    [
        ("ghcr.io/openlakeforge/project-code", "ghcr.io"),
        ("123456789012.dkr.ecr.eu-west-1.amazonaws.com/project-code", "123456789012.dkr.ecr.eu-west-1.amazonaws.com"),
        ("localhost:5001/project-code", "localhost:5001"),
        ("openlakeforge/project-code", ""),
        ("project-code", ""),
    ],
)
def test_registry_host_names_only_a_real_registry(repository: str, expected: str) -> None:
    """Docker Hub short forms carry no host, which is what makes them unauthenticatable here."""
    assert activation_module._registry_host(repository) == expected


class _Docker:
    """Records pulls the way the real client would perform them."""

    def __init__(self) -> None:
        self.pulls: list[tuple[str, str | None, dict]] = []

    def pull(self, image: str, *, platform=None, env=None) -> None:  # noqa: ANN001
        self.pulls.append((image, platform, dict(env or {})))


class _Backend:
    def __init__(self) -> None:
        self.logins: list[str] = []

    def registry_login(self, tools, facts, *, repository: str, env) -> None:  # noqa: ANN001, ARG002
        self.logins.append(repository)


def _image_provider(provider_kind, *, project_code_repository: str = ""):  # noqa: ANN001, ANN202
    docker = _Docker()
    backend = _Backend()
    return SimpleNamespace(
        context=SimpleNamespace(provider=provider_kind),
        tools=SimpleNamespace(docker=docker),
        backend=backend,
        config=SimpleNamespace(images=SimpleNamespace(image_platform="linux/amd64")),
        foundation_facts=SimpleNamespace(project_code_repository=project_code_repository),
    )


def test_local_image_pull_loads_the_image_into_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """A kind node cannot reach the host daemon's images, so the pull alone is not enough."""
    from olf.infra.deployment.local import images as local_images

    loaded: list[str] = []
    monkeypatch.setattr(local_images, "load_digest_image_into_kind", lambda image, *a, **k: loaded.append(image))
    provider = _image_provider(Provider.LOCAL)

    activation_module._ensure_image(provider, _IMAGE, env={})

    assert [pull[0] for pull in provider.tools.docker.pulls] == [_IMAGE]
    assert loaded == [_IMAGE]
    # Only a cloud provider pins a platform; kind runs the host's architecture.
    assert provider.tools.docker.pulls[0][1] is None
    assert provider.backend.logins == []


def test_local_pull_of_a_private_registry_uses_the_callers_docker_logins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scoped config holds no registry logins; a private revision is reachable only with the user's (#280)."""
    from olf.infra.deployment.local import images as local_images

    user_config = tmp_path / "user-docker"
    user_config.mkdir()
    (user_config / "config.json").write_text('{"auths": {"ghcr.io": {"auth": "fixture"}}}')
    monkeypatch.setenv("DOCKER_CONFIG", str(user_config))
    loads: list[dict] = []
    monkeypatch.setattr(
        local_images, "load_digest_image_into_kind", lambda image, *a, env, **k: loads.append(dict(env))
    )
    provider = _image_provider(Provider.LOCAL)

    activation_module._ensure_image(provider, _IMAGE, env={"DOCKER_CONFIG": "/scoped"})

    assert provider.tools.docker.pulls[0][2]["DOCKER_CONFIG"] == str(user_config)
    # Loading into kind talks only to the local daemon, so it keeps the scoped config.
    assert loads == [{"DOCKER_CONFIG": "/scoped"}]


def test_cloud_pull_authenticates_only_its_own_registry() -> None:
    """The scoped Docker config holds a provider login, so a foreign host must not use it."""
    registry = "123456789012.dkr.ecr.eu-west-1.amazonaws.com"
    provider = _image_provider(Provider.AWS, project_code_repository=f"{registry}/project-code")

    image = f"{registry}/project-code@sha256:{'a' * 64}"

    activation_module._ensure_image(provider, image, env={"DOCKER_CONFIG": "/scoped"})

    assert provider.backend.logins == [f"{registry}/project-code"]
    assert provider.tools.docker.pulls[0][1] == "linux/amd64"
    # Authenticated: the pull keeps the provider's own scoped config.
    assert provider.tools.docker.pulls[0][2]["DOCKER_CONFIG"] == "/scoped"


def test_cloud_pull_of_a_foreign_registry_falls_back_to_ambient_credentials() -> None:
    """A revision built elsewhere -- GHCR, or another provider -- has no login here."""
    provider = _image_provider(
        Provider.AWS, project_code_repository="123456789012.dkr.ecr.eu-west-1.amazonaws.com/project-code"
    )

    activation_module._ensure_image(provider, _IMAGE, env={"DOCKER_CONFIG": "/scoped"})

    assert provider.backend.logins == []
    assert "DOCKER_CONFIG" not in provider.tools.docker.pulls[0][2]


def _deployed_locations(values: dict) -> list[tuple[str, list[str], int]]:
    return [(entry["name"], entry["dagsterApiGrpcArgs"], entry["port"]) for entry in values["deployments"]]


def test_rollout_serves_the_default_merged_code_location(harness) -> None:  # noqa: ANN001
    """ADR 0006's shipped default: one location aggregating every product."""
    harness.deploy("dev")

    _, values = harness.helm.rollouts[0]

    assert _deployed_locations(values) == [
        ("openlakeforge-dagster", ["--module-name", "lakehouse_code.definitions"], 3030)
    ]


def test_rollout_follows_a_renamed_code_location(harness) -> None:  # noqa: ANN001
    """The platform's workspace names the location's host; a Service under the
    old name would leave the webserver pointing at nothing."""
    harness.contract["stages"]["dev"]["orchestration"]["code_locations"] = [
        {"name": "acme-dagster", "definitions_module": "acme_code.definitions"}
    ]

    harness.deploy("dev")

    _, values = harness.helm.rollouts[0]

    assert _deployed_locations(values) == [("acme-dagster", ["--module-name", "acme_code.definitions"], 3030)]


def test_rollout_creates_one_deployment_per_split_code_location(harness) -> None:  # noqa: ANN001
    """Split locations render N workspace servers on the platform side, so N
    Services have to exist -- rendering only the first leaves the rest unreachable."""
    harness.contract["stages"]["dev"]["orchestration"]["code_locations"] = [
        {"name": "sales", "definitions_module": "lakehouse_code.sales_definitions"},
        {"name": "finance", "definitions_module": "lakehouse_code.finance_definitions"},
    ]

    harness.deploy("dev")

    _, values = harness.helm.rollouts[0]

    assert _deployed_locations(values) == [
        ("sales", ["--module-name", "lakehouse_code.sales_definitions"], 3030),
        ("finance", ["--module-name", "lakehouse_code.finance_definitions"], 3030),
    ]
    # Every location runs the activated revision, not just the first.
    assert {entry["image"]["digest"] for entry in values["deployments"]} == {_IMAGE.split("@", 1)[1]}


def test_each_stage_gets_its_own_contracted_code_locations(harness) -> None:  # noqa: ANN001
    harness.contract["stages"]["prod"]["orchestration"]["code_locations"] = [
        {"name": "sales", "definitions_module": "lakehouse_code.sales_definitions"}
    ]

    harness.deploy("dev")
    harness.deploy("prod")

    rendered = {
        namespace: [entry["name"] for entry in values["deployments"]]
        for namespace, values in harness.helm.rollouts
    }

    assert rendered == {"olf-dev": ["openlakeforge-dagster"], "olf-prod": ["sales"]}


def test_changing_the_code_locations_rolls_the_stage_out_again(harness) -> None:  # noqa: ANN001
    """Renaming a location moves nothing else about the revision, so without the
    contract binding covering it a redeploy would skip as an idempotent no-op."""
    first = harness.deploy("dev")
    harness.contract["stages"]["dev"]["orchestration"]["code_locations"] = [
        {"name": "acme-dagster", "definitions_module": "lakehouse_code.definitions"}
    ]

    second = harness.deploy("dev")

    assert second.activation_revision != first.activation_revision
    assert [entry["name"] for entry in harness.helm.rollouts[1][1]["deployments"]] == ["acme-dagster"]


def test_a_rollback_restores_the_pairs_the_previous_release_ran(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """A deploy that changes `definitions_module` and then fails to commit must
    put back the pair that was actually running.

    Re-rendering the previous activation against the current contract would
    give the old image a module that exists only in the new one. The rollback
    then fails readiness, Helm restores the uncommitted new release, and
    ACTIVE.json names an activation the cluster is not running."""
    active = harness.deploy("dev")
    harness.contract["stages"]["dev"]["orchestration"]["code_locations"] = [
        {"name": "acme-dagster", "definitions_module": "lakehouse_code.next_definitions"}
    ]
    monkeypatch.setattr(
        activation_module,
        "commit_active",
        lambda *a, **k: (_ for _ in ()).throw(project_activation.ProjectActivationError("pointer write failed")),
    )

    with pytest.raises(project_activation.ProjectActivationError):
        harness.deploy("dev")

    assert project_activation.active(harness.store, stage=StageName.DEV) == active
    restored = harness.helm.installed["olf-dev"]["deployments"]
    # Both halves come from the release that was running. Keeping the
    # contract's name here would pair the previous image with a module it need
    # not contain, and an unready rollback lets Helm restore the uncommitted
    # new release while ACTIVE.json still names the old one.
    assert [entry["dagsterApiGrpcArgs"] for entry in restored] == [["--module-name", "lakehouse_code.definitions"]]
    assert [entry["name"] for entry in restored] == ["openlakeforge-dagster"]


def test_a_release_missing_one_contracted_location_is_not_skipped(harness) -> None:  # noqa: ANN001
    """A redeploy must repair a release that lost one of several code locations.

    The label check accepted a release where any one deployment matched, which
    was equivalent to all of them only while the contract named exactly one.
    With several, a release carrying one correct deployment and one missing
    would be read as up to date, and the reapply that would have restored it
    skipped -- leaving Terraform's workspace pointing at a Service nobody
    creates."""
    harness.contract["stages"]["dev"]["orchestration"]["code_locations"] = [
        {"name": "openlakeforge-dagster", "definitions_module": "lakehouse_code.definitions"},
        {"name": "acme-dagster", "definitions_module": "lakehouse_code.acme"},
    ]
    harness.deploy("dev")
    assert len(harness.helm.rollouts) == 1
    harness.helm.installed["olf-dev"]["deployments"] = harness.helm.installed["olf-dev"]["deployments"][:1]

    harness.deploy("dev")

    assert len(harness.helm.rollouts) == 2
    assert [entry["name"] for entry in harness.helm.rollouts[1][1]["deployments"]] == [
        "openlakeforge-dagster",
        "acme-dagster",
    ]


def test_a_rollback_matches_modules_by_name_not_position(harness, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """Reordering a multi-location contract must not shuffle modules between hosts.

    Pairing the contracted list with the release's by position hands each
    Service another location's definitions when only the order changed: the
    rollback stays healthy and serves the wrong code under each workspace
    host, which is worse than failing."""
    harness.contract["stages"]["dev"]["orchestration"]["code_locations"] = [
        {"name": "alpha-dagster", "definitions_module": "lakehouse_code.alpha"},
        {"name": "beta-dagster", "definitions_module": "lakehouse_code.beta"},
    ]
    harness.deploy("dev")
    harness.contract["stages"]["dev"]["orchestration"]["code_locations"] = [
        {"name": "beta-dagster", "definitions_module": "lakehouse_code.beta"},
        {"name": "alpha-dagster", "definitions_module": "lakehouse_code.alpha"},
    ]
    monkeypatch.setattr(
        activation_module,
        "commit_active",
        lambda *a, **k: (_ for _ in ()).throw(project_activation.ProjectActivationError("pointer write failed")),
    )

    with pytest.raises(project_activation.ProjectActivationError):
        harness.deploy("dev")

    restored = {
        entry["name"]: entry["dagsterApiGrpcArgs"][1] for entry in harness.helm.installed["olf-dev"]["deployments"]
    }
    assert restored == {"alpha-dagster": "lakehouse_code.alpha", "beta-dagster": "lakehouse_code.beta"}


def test_an_unreadable_release_refuses_the_upgrade_before_it_starts(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """With no way to learn what the release runs, there is no safe rollback.

    Failing here costs nothing -- the upgrade has not begun. Proceeding would
    leave the previous image paired with the current contract's modules if the
    pointer write then failed, which is the mismatch this path exists to
    prevent."""
    harness.deploy("dev")
    rollouts = len(harness.helm.rollouts)
    original = harness.helm.get_values
    # Only the activation's own release: the platform release is a different
    # read on the same method, and failing that one is a different scenario.
    monkeypatch.setattr(
        harness.helm,
        "get_values",
        lambda release, **k: original(release, **k)
        if release == "dagster"
        else SimpleNamespace(ok=False, stdout=""),
    )

    with pytest.raises(activation_module.ActivationError, match="Refusing to upgrade"):
        harness.deploy("dev")

    assert len(harness.helm.rollouts) == rollouts


def test_a_rollback_does_not_give_the_previous_image_a_new_module(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """Growing a stage from one code location to two must not break recovery.

    The added name has no module in the running release, so pairing it with
    the contract's would hand `previous`'s image a module introduced in the
    new one. The rollback could not become ready, and Helm would restore the
    uncommitted new release while ACTIVE.json still named the old activation."""
    harness.deploy("dev")
    harness.contract["stages"]["dev"]["orchestration"]["code_locations"] = [
        {"name": "openlakeforge-dagster", "definitions_module": "lakehouse_code.definitions"},
        {"name": "acme-dagster", "definitions_module": "lakehouse_code.brand_new"},
    ]
    monkeypatch.setattr(
        activation_module,
        "commit_active",
        lambda *a, **k: (_ for _ in ()).throw(project_activation.ProjectActivationError("pointer write failed")),
    )

    with pytest.raises(project_activation.ProjectActivationError):
        harness.deploy("dev")

    restored = harness.helm.installed["olf-dev"]["deployments"]
    assert [entry["name"] for entry in restored] == ["openlakeforge-dagster"]
    assert all("lakehouse_code.brand_new" not in entry["dagsterApiGrpcArgs"] for entry in restored)


def test_a_rollback_restores_the_image_that_ran_the_modules(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """The rollback target is one observation, not two sources spliced.

    A release manually rolled back to another activation runs modules the
    pointer's image need not contain. Taking modules from the release and the
    image from `ACTIVE.json` can therefore name a module that digest never
    shipped, and the rollback would not become ready."""
    harness.deploy("dev")
    live = harness.helm.installed["olf-dev"]["deployments"]
    other = "ghcr.io/openlakeforge/project-code@sha256:" + "d" * 64
    repository, digest = other.split("@", 1)
    for entry in live:
        entry["image"] = {"repository": repository, "digest": digest, "pullPolicy": "IfNotPresent"}
        entry["dagsterApiGrpcArgs"] = ["--module-name", "lakehouse_code.other_definitions"]
        # The label a manual rollback to another activation leaves behind;
        # without it the redeploy reads the release as current and skips.
        entry["deploymentLabels"]["openlakeforge.io/activation-revision"] = "sha256:" + "e" * 64
    monkeypatch.setattr(
        activation_module,
        "commit_active",
        lambda *a, **k: (_ for _ in ()).throw(project_activation.ProjectActivationError("pointer write failed")),
    )

    with pytest.raises(project_activation.ProjectActivationError):
        harness.deploy("dev")

    restored = harness.helm.installed["olf-dev"]["deployments"]
    assert [entry["dagsterApiGrpcArgs"] for entry in restored] == [
        ["--module-name", "lakehouse_code.other_definitions"]
    ]
    assert {entry["image"]["digest"] for entry in restored} == {digest}


def test_a_deleted_release_is_uninstalled_rather_than_restored(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """A pointer is not a running deployment.

    With `ACTIVE.json` naming an activation whose release was deleted, there is
    no state to put back, and reconstructing one from the current contract
    would pair its modules with the previous image. Undoing means removing what
    was just installed."""
    harness.deploy("dev")
    harness.helm.installed.pop("olf-dev")
    harness.helm.ready.discard("olf-dev")
    monkeypatch.setattr(
        activation_module,
        "commit_active",
        lambda *a, **k: (_ for _ in ()).throw(project_activation.ProjectActivationError("pointer write failed")),
    )

    with pytest.raises(project_activation.ProjectActivationError):
        harness.deploy("dev")

    assert harness.helm.uninstalled == ["olf-dev"]


def test_stage_query_tls_fields_of_a_real_contract_reach_the_code_server() -> None:
    """The fields sit only under stages.<stage>.query, as `terraform output` emits them."""
    import json

    from test_provider_conformance import REPO_ROOT, _topology_of

    from olf.infra.contracts import build_contract_env

    contract = json.loads((Path(__file__).parents[1] / "fixtures/local-provider-contracts-v3.json").read_text())
    contract["stages"]["dev"]["query"].update(tls_port="8443", ca_configmap_name="olf-local-ca")

    exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT, topology=_topology_of(contract), stage="dev")
    deployment = _values({k: v for k, v in exports.items() if "QUERY_TRINO" in k})["deployments"][0]

    env = {e["name"]: e.get("value") for e in deployment["env"]}
    assert env["OPENLAKEFORGE_QUERY_TRINO_HTTP_SCHEME"] == "https"
    assert deployment["volumes"][0]["configMap"]["name"] == "olf-local-ca"


def test_a_password_contract_gives_dbt_the_stage_secret_by_reference() -> None:
    values = _values(
        {
            "OPENLAKEFORGE_QUERY_TRINO_TLS_PORT": "8443",
            "OPENLAKEFORGE_QUERY_TRINO_CA_CONFIGMAP": "olf-local-ca",
            "OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET": "trino-credential",
        }
    )
    env = {e["name"]: e for e in values["deployments"][0]["env"]}

    assert env["OPENLAKEFORGE_DBT_TRINO_METHOD"]["value"] == "ldap"
    assert env["OPENLAKEFORGE_DBT_TRINO_PASSWORD"] == {
        "name": "OPENLAKEFORGE_DBT_TRINO_PASSWORD",
        "valueFrom": {"secretKeyRef": {"name": "trino-credential", "key": "password"}},
    }


def test_without_a_password_contract_dbt_gets_no_trino_password() -> None:
    names = {e["name"] for e in _values()["deployments"][0]["env"]}

    assert not names & {"OPENLAKEFORGE_DBT_TRINO_METHOD", "OPENLAKEFORGE_DBT_TRINO_PASSWORD"}


def test_run_pods_can_bind_floe_domain_jobs_to_their_own_image_namespace_and_secrets() -> None:
    """Floe's Job runs on the release's own image in its stage, and references credentials by Secret name."""
    values = _values({"OPENLAKEFORGE_KUBE_NAMESPACE": "olf-dev-default-from-contract"})
    env = [(e["name"], e.get("value")) for e in values["deployments"][0]["env"]]
    by_name = dict(env)

    assert by_name["OPENLAKEFORGE_PROJECT_CODE_IMAGE"] == _IMAGE
    assert [value for name, value in env if name == "OPENLAKEFORGE_KUBE_NAMESPACE"] == ["olf-dev"]
    assert by_name["OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME"] == "seaweedfs-s3-creds"
    assert by_name["OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME"] == "polaris-floe-creds"


def test_a_revision_whose_source_is_not_bound_in_the_stage_is_not_rolled_out(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """A missing stage binding would surface only as a run pod that cannot start."""
    from openlakeforge_domain.inventory import Source

    real = activation_module.load_lakehouse_inventory

    def with_a_bound_source(path):  # noqa: ANN001, ANN202
        inventory = real(path)
        bound = Source(
            descriptor_path=Path("source.yaml"),
            name="crm_api",
            display_name="CRM API",
            resources=(),
            runtime_config=("base_url",),
            runtime_secrets=("api_token",),
        )
        return SimpleNamespace(sources=(*inventory.sources, bound))

    monkeypatch.setattr(activation_module, "load_lakehouse_inventory", with_a_bound_source)

    with pytest.raises(activation_module.ActivationError, match="olf-source-crm-api-credentials"):
        harness.deploy("dev")

    assert harness.helm.rollouts == []
    assert project_activation.active(harness.store, stage=StageName.DEV) is None


def _pull_secret(*hosts: str, secret_type: str = "kubernetes.io/dockerconfigjson") -> dict:
    config = {"auths": {host: {"auth": "dTpw"} for host in hosts}}
    return {"type": secret_type, "data": {".dockerconfigjson": base64.b64encode(json.dumps(config).encode()).decode()}}


def test_without_a_pull_secret_no_pod_references_one(harness) -> None:  # noqa: ANN001
    harness.deploy("dev")

    _, values = harness.helm.rollouts[0]
    archiver = values["extraManifests"][0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]

    assert "imagePullSecrets" not in values
    assert "imagePullSecrets" not in archiver
    assert harness.kubectl.account_secrets == []


def test_every_project_code_pod_pulls_with_the_secret(harness) -> None:  # noqa: ANN001
    """Code servers and (through them) run pods, the log archiver, and the account Floe Jobs run as."""
    harness.kubectl.secrets["ghcr-pull"] = _pull_secret("ghcr.io")
    harness.kubectl.account_secrets = ["registry-credentials"]

    harness.deploy("dev", pull_secret="ghcr-pull")

    _, values = harness.helm.rollouts[0]
    archiver = values["extraManifests"][0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert values["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    assert archiver["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    # Every pod type runs as `dagster`, so the account link reaches Floe Jobs too and keeps what was listed.
    assert values["serviceAccount"] == {"create": False, "name": "dagster"}
    assert archiver["serviceAccountName"] == "dagster"
    assert harness.kubectl.account_secrets == ["registry-credentials", "ghcr-pull"]


def test_docker_hub_credentials_match_a_docker_io_image() -> None:
    kubectl = _Kubectl()
    kubectl.secrets["hub-pull"] = _pull_secret("https://index.docker.io/v1/")
    context = SimpleNamespace(paths=SimpleNamespace(kubeconfig_path=None), kube_context="kind-test")
    provider = SimpleNamespace(tools=SimpleNamespace(kubectl=kubectl), context=context, env={})

    activation_module._require_pull_secret(
        provider, "hub-pull", image="docker.io/acme/code@sha256:" + "a" * 64, namespace="olf-dev", env={}
    )


def test_an_unusable_pull_secret_stops_the_activation_before_any_rollout(harness) -> None:  # noqa: ANN001
    previous = harness.deploy("dev")
    harness.kubectl.secrets["wrong-type"] = _pull_secret("ghcr.io", secret_type="Opaque")
    harness.kubectl.secrets["wrong-registry"] = _pull_secret("quay.io")
    harness.kubectl.secrets["not-docker-config"] = {"type": "kubernetes.io/dockerconfigjson", "data": {}}

    for name, problem in (
        ("absent", "missing or unreadable"),
        ("wrong-type", "must have type kubernetes.io/dockerconfigjson"),
        ("wrong-registry", "no credentials for ghcr.io"),
        ("not-docker-config", "has no readable .dockerconfigjson"),
    ):
        with pytest.raises(activation_module.ActivationError, match=problem):
            harness.deploy("dev", pull_secret=name)

    assert len(harness.helm.rollouts) == 1
    assert project_activation.active(harness.store, stage=StageName.DEV) == previous
    assert harness.kubectl.account_secrets == []


def test_adding_a_pull_secret_to_the_active_revision_rolls_it_out_again(harness) -> None:  # noqa: ANN001
    harness.kubectl.secrets["ghcr-pull"] = _pull_secret("ghcr.io")
    harness.deploy("dev")

    harness.deploy("dev", pull_secret="ghcr-pull")
    harness.deploy("dev", pull_secret="ghcr-pull")

    assert len(harness.helm.rollouts) == 2


def test_rollback_keeps_the_pull_secret_the_live_release_used(harness, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    harness.kubectl.secrets["ghcr-pull"] = _pull_secret("ghcr.io")
    harness.deploy("dev", pull_secret="ghcr-pull")
    monkeypatch.setattr(
        activation_module,
        "commit_active",
        lambda *a, **k: (_ for _ in ()).throw(project_activation.ProjectActivationError("pointer write failed")),
    )
    harness.helm.installed["olf-dev"]["deployments"][0]["deploymentLabels"][activation_module._ACTIVATION_LABEL] = "x"

    with pytest.raises(project_activation.ProjectActivationError):
        harness.deploy("dev", pull_secret="ghcr-pull")

    assert harness.helm.installed["olf-dev"]["imagePullSecrets"] == [{"name": "ghcr-pull"}]


def _reloading_client(replies: list[object]) -> tuple[DagsterClient, list[str]]:
    """Each reload answers with the next reply: an image, or an exception to raise."""
    reloaded: list[str] = []

    def request_json(_query: str, variables=None):  # noqa: ANN001, ANN202
        reloaded.append((variables or {})["repositoryLocationName"])
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {
            "data": {
                "reloadRepositoryLocation": {
                    "__typename": "WorkspaceLocationEntry",
                    "displayMetadata": [{"key": "image", "value": reply}],
                    "locationOrLoadError": {"__typename": "RepositoryLocation"},
                }
            }
        }

    return DagsterClient("http://dagster/graphql", request_json=request_json), reloaded


def test_the_wait_retries_until_the_webserver_reports_the_activated_digest() -> None:
    from openlakeforge_services.dagster import DagsterTransientError

    previous = "ghcr.io/openlakeforge/project-code@sha256:" + "f" * 64
    client, reloaded = _reloading_client([DagsterTransientError("connection refused"), previous, _IMAGE])
    now = [0.0]

    activation_module.await_locations_serving(
        client,
        _DEFAULT_LOCATIONS,
        _IMAGE,
        timeout_seconds=60,
        delay_seconds=5,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    assert reloaded == ["openlakeforge-dagster"] * 3


def test_the_wait_is_bounded_and_names_what_the_location_served() -> None:
    previous = "ghcr.io/openlakeforge/project-code@sha256:" + "f" * 64
    client, reloaded = _reloading_client([previous] * 10)
    now = [0.0]

    with pytest.raises(activation_module.ActivationError, match=f"openlakeforge-dagster: serves image {previous}"):
        activation_module.await_locations_serving(
            client,
            _DEFAULT_LOCATIONS,
            _IMAGE,
            timeout_seconds=10,
            delay_seconds=5,
            clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )

    assert len(reloaded) == 3  # at 0s, 5s and 10s
