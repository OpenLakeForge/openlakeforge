from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from openlakeforge_domain import load_lakehouse_inventory

from olf import project_activation, project_revision
from olf.artifact_store import FilesystemRevisionStore
from olf.deployment import activation as activation_module
from olf.deployment.context import DeploymentContext, Provider
from olf.distribution import distribution_version_at
from olf.profile import StageName, resolve_topology, validate_deployment_profile
from olf.project import ProjectSpec
from olf.provider_contracts import CodeLocation

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[3]
_IMAGE = "ghcr.io/openlakeforge/project-code@sha256:" + "a" * 64
_FLOE = "sha256:" + "c" * 64
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
                "spec": {"provider": {"type": "local"}, "preset": "full", "stages": stages},
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


class _Provider:
    def __init__(self, context: DeploymentContext, helm: _Helm) -> None:
        self.context = context
        self.env: dict[str, str] = {}
        self.tools = SimpleNamespace(helm=helm)
        self.config = SimpleNamespace(
            charts={"dagster": SimpleNamespace(package_path=Path("unused.tgz"))},
            paths=context.paths,
            terraform=SimpleNamespace(apply_retry=None),
            floe=SimpleNamespace(image="ghcr.io/malon64/floe:0.6.11", version="0.6.11", runtime="image"),
        )


@pytest.fixture
def harness(external_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """A published revision plus an activation path with its I/O collaborators stubbed.

    Floe rendering, image pulls, chart preparation and the contract environment
    all reach a live cluster; the decisions under test -- what gets rolled out,
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
    floe_calls: list[str] = []
    catalog_calls: list[str] = []

    monkeypatch.setattr(activation_module.contracts, "load_provider_contracts", lambda *a, **k: contract)
    image_calls: list[str] = []
    monkeypatch.setattr(
        activation_module, "_ensure_image", lambda provider, image, **k: image_calls.append(image)
    )
    monkeypatch.setattr(activation_module, "prepare_chart", lambda *a, **k: None)
    monkeypatch.setattr(activation_module, "deploy_optional_layer_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(activation_module, "_user_chart", lambda chart, work_root: work_root)
    monkeypatch.setattr(activation_module, "sync_catalog_namespaces", lambda: catalog_calls.append("sync"))

    def _floe(provider, *, repo_root, contract_environ, env):  # noqa: ANN001, ANN202, ARG001
        floe_calls.append(str(repo_root))
        return _FLOE

    monkeypatch.setattr(activation_module, "_generate_floe", _floe)

    @contextmanager
    def _contract_environment(**kwargs):  # noqa: ANN003, ANN202
        yield {"OPENLAKEFORGE_KUBE_NAMESPACE": kwargs["namespace"]}

    monkeypatch.setattr(activation_module.contract_env, "applied_contract_environment", _contract_environment)

    providers: list[_Provider] = []

    def deploy(stage: str):  # noqa: ANN202
        context = DeploymentContext.local(
            repo_root=external_project, topology=topology, stage=stage, work_root=tmp_path / "work"
        )
        context.paths.work_root.mkdir(parents=True, exist_ok=True)
        provider = _Provider(context, helm)
        if providers:
            provider.config.floe = providers[-1].config.floe
        providers.append(provider)
        return activation_module.deploy_revision(
            provider, revision=manifest.revision, store=store, profile_name="acceptance"
        )

    return SimpleNamespace(
        deploy=deploy,
        contract=contract,
        store=store,
        helm=helm,
        manifest=manifest,
        floe_calls=floe_calls,
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

    monkeypatch.setattr(activation_module.contracts, "load_provider_contracts", lambda *a, **k: contract)
    monkeypatch.setattr(activation_module, "_ensure_image", lambda provider, image, **k: None)
    monkeypatch.setattr(activation_module, "prepare_chart", lambda *a, **k: None)
    monkeypatch.setattr(activation_module, "_user_chart", lambda chart, work_root: work_root)
    monkeypatch.setattr(activation_module, "sync_catalog_namespaces", lambda: None)
    monkeypatch.setattr(activation_module, "_generate_floe", lambda *a, **k: _FLOE)

    import olf.commands.openmetadata as openmetadata_commands
    import olf.superset as superset_module

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
    monkeypatch.setattr(openmetadata_commands, "deploy_openmetadata_metadata", lambda: metadata_calls.append(None))

    def deploy(stage: str):  # noqa: ANN202
        context = DeploymentContext.local(
            repo_root=external_project, topology=topology, stage=stage, work_root=tmp_path / "work"
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


def test_reapplying_the_active_revision_changes_nothing(harness) -> None:  # noqa: ANN001
    first = harness.deploy("dev")
    second = harness.deploy("dev")

    assert second == first
    assert len(harness.helm.rollouts) == 1
    # The point of the early gate: a no-op redeploy must not re-render Floe or
    # reconcile catalog namespaces just to discover it had nothing to do.
    assert len(harness.floe_calls) == 1
    assert len(harness.catalog_calls) == 1
    # Pulling the image (and on local, reloading it into kind) is a cluster
    # mutation, so it belongs after the gate, not before it.
    assert len(harness.image_calls) == 1


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
    uninstalling the stage -- and marks the renderer unreconciled so the next
    deploy regenerates Floe instead of trusting the rolled-back annotation."""
    active = harness.deploy("dev")
    monkeypatch.setattr(
        activation_module,
        "commit_active",
        lambda *a, **k: (_ for _ in ()).throw(project_activation.ProjectActivationError("pointer write failed")),
    )
    harness.providers[-1].config.floe = SimpleNamespace(
        image="ghcr.io/malon64/floe:0.7.0", version="0.7.0", runtime="image"
    )

    with pytest.raises(project_activation.ProjectActivationError):
        harness.deploy("dev")

    assert harness.helm.uninstalled == []
    assert project_activation.active(harness.store, stage=StageName.DEV) == active
    annotations = harness.helm.installed["olf-dev"]["deployments"][0]["deploymentAnnotations"]
    assert annotations["openlakeforge.io/floe-renderer"] == activation_module._RENDERER_UNRECONCILED


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
    from olf.deployment.activation import _user_values
    from olf.project_activation import ProjectActivation

    activation = ProjectActivation(
        deployment_profile="acceptance",
        provider="local",
        stage=StageName.DEV,
        project_name="demo",
        project_revision="sha256:" + "b" * 64,
        distribution_version="0.0.0",
        project_code_image=_IMAGE,
        floe_manifest_revision=_FLOE,
        provider_binding_digest="sha256:" + "d" * 64,
        capabilities={"analytics": False, "governance": False},
    ).resolved()

    values = _user_values(
        activation,
        contract_environ={
            "AWS_ENDPOINT_URL_S3": "http://seaweedfs-s3.olf-system:8333",
            "AWS_REGION": "us-east-1",
            "AWS_SECRET_ACCESS_KEY": "must-not-appear",
            "OPENLAKEFORGE_STORAGE_BUCKET": "lakehouse-bronze",
        },
        namespace="olf-dev",
        platform_globals={},
        floe_renderer="ghcr.io/malon64/floe:0.6.11|0.6.11|image",
        code_locations=_DEFAULT_LOCATIONS,
    )

    env = {entry["name"]: entry["value"] for entry in values["deployments"][0]["env"]}
    assert env["AWS_ENDPOINT_URL_S3"] == "http://seaweedfs-s3.olf-system:8333"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["OPENLAKEFORGE_STORAGE_BUCKET"] == "lakehouse-bronze"
    # Credential values travel by Secret reference, never inline.
    assert "AWS_SECRET_ACCESS_KEY" not in env


def _values(**capabilities: bool) -> dict:
    from olf.deployment.activation import _user_values
    from olf.project_activation import ProjectActivation

    activation = ProjectActivation(
        deployment_profile="acceptance",
        provider="local",
        stage=StageName.DEV,
        project_name="demo",
        project_revision="sha256:" + "b" * 64,
        distribution_version="0.0.0",
        project_code_image=_IMAGE,
        floe_manifest_revision=_FLOE,
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
        },
        namespace="olf-dev",
        platform_globals={},
        floe_renderer="ghcr.io/malon64/floe:0.6.11|0.6.11|image",
        code_locations=_DEFAULT_LOCATIONS,
    )


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


def test_namespace_alias_follows_the_activated_stage() -> None:
    """floe_dagster.kubernetes_runner submits Jobs into NAMESPACE, defaulting to "lakehouse"."""
    env = {entry["name"]: entry.get("value") for entry in _values()["deployments"][0]["env"]}

    assert env["NAMESPACE"] == "olf-dev"


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


def test_reapplying_regenerates_when_the_floe_renderer_changes(harness) -> None:  # noqa: ANN001
    """FLOE_IMAGE/VERSION/RUNTIME change the manifests without moving any activation input."""
    harness.deploy("dev")

    harness.providers[-1].config.floe = SimpleNamespace(
        image="ghcr.io/malon64/floe:0.7.0", version="0.7.0", runtime="image"
    )
    harness.deploy("dev")

    assert len(harness.helm.rollouts) == 2
    annotations = harness.helm.installed["olf-dev"]["deployments"][0]["deploymentAnnotations"]
    assert annotations["openlakeforge.io/floe-renderer"] == "ghcr.io/malon64/floe:0.7.0|0.7.0|image"


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
    from olf.deployment.local import images as local_images

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
    from olf.deployment.local import images as local_images

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


@pytest.mark.parametrize(
    ("provider_kind", "expected_transport", "expected_renderer"),
    [(Provider.AWS, "direct", "aws"), (Provider.LOCAL, "port-forward", "local")],
)
def test_floe_generation_picks_the_transport_its_provider_can_reach(
    provider_kind, expected_transport: str, expected_renderer: str, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """AWS writes to object storage directly; local has to go through a forwarded port."""
    rendered: list[str] = []
    monkeypatch.setattr(
        activation_module, "generate_aws_manifests", lambda *a, **k: rendered.append("aws")
    )
    monkeypatch.setattr(
        activation_module, "generate_local_manifests", lambda *a, **k: rendered.append("local")
    )
    monkeypatch.setattr(activation_module, "activate_runtime_revision", lambda _dir, *, via: via)
    provider = SimpleNamespace(
        context=SimpleNamespace(
            provider=provider_kind,
            paths=SimpleNamespace(distribution_root=Path("/dist")),
            namespace="olf-dev",
            features=SimpleNamespace(governance_enabled=True),
        ),
        tools=SimpleNamespace(),
        config=SimpleNamespace(floe=SimpleNamespace(runtime_artifact_dir=Path("/artifacts"))),
    )

    transport = activation_module._generate_floe(
        provider, repo_root=Path("/repo"), contract_environ={}, env={}
    )

    assert rendered == [expected_renderer]
    assert transport == expected_transport


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
    annotations = restored[0]["deploymentAnnotations"]
    assert annotations["openlakeforge.io/floe-renderer"] == activation_module._RENDERER_UNRECONCILED


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
