from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from _tooling_support import RecordedCall, RecordingRunner

from olf.common.deployment.context import DeploymentContext, Profile
from olf.common.deployment.errors import DeploymentPreconditionError
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.common.profile import Provider, Size, resolve_topology, validate_deployment_profile
from olf.common.tooling.docker import Docker
from olf.common.tooling.helm import Helm
from olf.common.tooling.kind import Kind
from olf.common.tooling.kubectl import Kubectl
from olf.common.tooling.process import CommandResult
from olf.common.tooling.resolver import PathExecutableResolver
from olf.common.tooling.terraform import Terraform
from olf.common.tooling.toolkit import Toolkit
from olf.infra.deployment.storage_resize import plan_storage_resize, require_storage_size_applies

SIZES_FILE = Path(__file__).parents[4] / "release/storage-sizes.yaml"

# A deployment applied at `small`: the claims the chart names, at the sizes the release file gives that row.
_MASTER = "data-olf-system-seaweedfs-master-0"
_FILER = "data-filer-seaweedfs-filer-0"
_VOLUME = "data1-seaweedfs-volume-0"
_SMALL = {_MASTER: "1Gi", _FILER: "5Gi", _VOLUME: "20Gi"}
_LARGE = {_MASTER: "5Gi", _FILER: "50Gi", _VOLUME: "2Ti"}


class _Cluster(RecordingRunner):
    """The slice of a cluster a resize reads: its PVCs and StorageClasses. Everything else answers empty."""

    def __init__(
        self, claims: dict[str, str], *, classes: dict[str, bool], claim_class: str | None = "standard"
    ) -> None:
        super().__init__()
        self.claims = claims
        self.classes = classes
        self.claim_class = claim_class

    def run(self, command, **kwargs):  # type: ignore[no-untyped-def,override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(part) for part in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        verb = argv[argv.index("get") + 1] if "get" in argv else ""
        if verb == "pvc":
            items = [
                {
                    "metadata": {"name": name},
                    "spec": {
                        "resources": {"requests": {"storage": size}},
                        **({"storageClassName": self.claim_class} if self.claim_class else {}),
                    },
                }
                for name, size in self.claims.items()
            ]
            other = {"metadata": {"name": "data-postgresql-0"}, "spec": {"resources": {"requests": {"storage": "5G"}}}}
            items.append(other)
            return _ok(json.dumps({"items": items}))
        if verb == "storageclass":
            default = {"storageclass.kubernetes.io/is-default-class": "true"}
            items = [
                {
                    "metadata": {"name": name, "annotations": default if name == "standard" else {}},
                    **({"allowVolumeExpansion": True} if expandable else {}),
                }
                for name, expandable in self.classes.items()
            ]
            return _ok(json.dumps({"items": items}))
        return _ok()

    def mutations(self) -> list[list[str]]:
        return [call.argv for call in self.calls if {"patch", "delete", "apply", "replace"} & set(call.argv)]


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(argv=(), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)


def _config(tmp_path: Path, size: Size) -> LocalDeploymentConfig:
    (tmp_path / "release").mkdir(exist_ok=True)
    shutil.copy(SIZES_FILE, tmp_path / "release/storage-sizes.yaml")
    topology = resolve_topology(
        validate_deployment_profile(
            {
                "apiVersion": "openlakeforge.io/v1alpha1",
                "kind": "DeploymentProfile",
                "metadata": {"name": "acme-data"},
                "spec": {
                    "provider": {"type": "local"},
                    "preset": "slim",
                    "stages": {"dev": {"enabled": True}},
                    "storage": {"size": size.value},
                },
            }
        )
    )
    context = DeploymentContext.local(repo_root=tmp_path, profile=Profile.SLIM, topology=topology)
    return LocalDeploymentConfig.from_environment({}, context=context)


def _toolkit(runner: RecordingRunner) -> Toolkit:
    tools = ("terraform", "docker", "kind", "kubectl", "helm")
    resolver = PathExecutableResolver(overrides={tool: Path(tool) for tool in tools})
    return Toolkit(
        runner=runner,
        resolver=resolver,
        terraform=Terraform(runner, resolver),
        helm=Helm(runner, resolver),
        kubectl=Kubectl(runner, resolver),
        docker=Docker(runner, resolver),
        kind=Kind(runner, resolver),
    )


def _plan(config: LocalDeploymentConfig, cluster: _Cluster):  # type: ignore[no-untyped-def]
    return plan_storage_resize(config, _toolkit(cluster), kube_context="kind-test", env={})


def test_a_first_install_has_nothing_to_resize(tmp_path: Path) -> None:
    config = _config(tmp_path, Size.LARGE)

    assert _plan(config, _Cluster({}, classes={"standard": False})) == ()


def test_the_size_already_applied_changes_nothing(tmp_path: Path) -> None:
    cluster = _Cluster(_SMALL, classes={"standard": False})

    assert _plan(_config(tmp_path, Size.SMALL), cluster) == ()
    assert cluster.mutations() == []


def test_growing_where_the_storage_class_expands_names_every_volume_that_changes(tmp_path: Path) -> None:
    growth = _plan(_config(tmp_path, Size.MEDIUM), _Cluster(_SMALL, classes={"standard": True}))

    assert {step.pvc: (step.current, step.target) for step in growth} == {
        _MASTER: ("1Gi", "2Gi"),
        _FILER: ("5Gi", "20Gi"),
        _VOLUME: ("20Gi", "300Gi"),
    }


def _require(config: LocalDeploymentConfig, cluster: _Cluster) -> None:
    require_storage_size_applies(config, _toolkit(cluster), kube_context="kind-test", env={})


def test_growing_is_refused_for_now_even_where_the_storage_class_expands(tmp_path: Path) -> None:
    cluster = _Cluster(_SMALL, classes={"standard": True})

    with pytest.raises(DeploymentPreconditionError, match="does not grow existing volumes in place yet") as refused:
        _require(_config(tmp_path, Size.MEDIUM), cluster)

    assert _VOLUME in str(refused.value)
    assert cluster.mutations() == []


def test_the_size_already_applied_passes_the_check(tmp_path: Path) -> None:
    cluster = _Cluster(_SMALL, classes={"standard": False})

    _require(_config(tmp_path, Size.SMALL), cluster)

    assert cluster.mutations() == []


def test_a_retry_after_a_partial_run_grows_only_what_is_still_small(tmp_path: Path) -> None:
    claims = {**_SMALL, _VOLUME: "300Gi"}

    growth = _plan(_config(tmp_path, Size.MEDIUM), _Cluster(claims, classes={"standard": True}))

    assert {step.pvc for step in growth} == {_MASTER, _FILER}


def test_growing_where_the_storage_class_cannot_expand_is_refused_before_any_mutation(tmp_path: Path) -> None:
    cluster = _Cluster(_SMALL, classes={"standard": False})
    config = _config(tmp_path, Size.MEDIUM)

    with pytest.raises(DeploymentPreconditionError, match=r"StorageClass 'standard'.*allowVolumeExpansion") as refused:
        _require(config, cluster)

    assert "docs/reference/deployment-profile.md#changing-the-storage-size" in str(refused.value)
    assert cluster.mutations() == []


def test_a_claim_without_a_class_is_judged_by_the_clusters_default(tmp_path: Path) -> None:
    refusing = _Cluster(_SMALL, classes={"standard": False, "fast": True}, claim_class=None)
    allowed = _Cluster(_SMALL, classes={"standard": True}, claim_class=None)

    with pytest.raises(DeploymentPreconditionError, match="StorageClass 'standard'"):
        _plan(_config(tmp_path, Size.MEDIUM), refusing)
    assert _plan(_config(tmp_path, Size.MEDIUM), allowed)


def test_a_class_that_no_longer_exists_cannot_be_assumed_to_expand(tmp_path: Path) -> None:
    with pytest.raises(DeploymentPreconditionError, match="StorageClass 'gone'"):
        _plan(_config(tmp_path, Size.MEDIUM), _Cluster(_SMALL, classes={"standard": True}, claim_class="gone"))


@pytest.mark.parametrize("expandable", [True, False])
def test_shrinking_is_always_refused_before_any_mutation(tmp_path: Path, expandable: bool) -> None:
    cluster = _Cluster(_LARGE, classes={"standard": expandable})

    with pytest.raises(DeploymentPreconditionError, match="never shrinks"):
        _require(_config(tmp_path, Size.MEDIUM), cluster)

    assert cluster.mutations() == []


def test_one_refusal_does_not_hide_another(tmp_path: Path) -> None:
    claims = {**_SMALL, _VOLUME: "2Ti"}

    with pytest.raises(DeploymentPreconditionError) as refused:
        _plan(_config(tmp_path, Size.MEDIUM), _Cluster(claims, classes={"standard": False}))

    assert _VOLUME in str(refused.value)
    assert _FILER in str(refused.value)


def test_aws_has_no_volume_to_resize(tmp_path: Path) -> None:
    cluster = _Cluster(_LARGE, classes={})
    aws = SimpleNamespace(context=SimpleNamespace(provider=Provider.AWS))

    assert plan_storage_resize(aws, _toolkit(cluster), kube_context="eks", env={}) == ()  # type: ignore[arg-type]
    assert cluster.calls == []


def test_an_unreadable_cluster_is_not_read_as_having_no_volumes(tmp_path: Path) -> None:
    class _Down(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[no-untyped-def,override]
            return CommandResult(argv=(), returncode=1, stdout="", stderr="connection refused", duration_seconds=0.0)

    with pytest.raises(DeploymentPreconditionError, match="connection refused"):
        plan_storage_resize(_config(tmp_path, Size.SMALL), _toolkit(_Down()), kube_context="kind-test", env={})
