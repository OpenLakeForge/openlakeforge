from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import E2E_REPO_ROOT

from olf.common.deployment.context import DeploymentContext, query_variables
from olf.common.profile import Size, resolve_project, resolve_topology, validate_deployment_profile
from olf.common.query_sizing import QUERY_SIZES_PATH, QuerySizingError, load_query_sizing

_TRINO_VALUES = yaml.safe_load((E2E_REPO_ROOT / "infra/helm/values/local/trino.yaml").read_text())
# Trino reserves 30% of the heap as headroom, and refuses to start when max-memory-per-node does not fit in the rest.
_USABLE_HEAP = 0.7


def _gigabytes(quantity: str) -> float:
    return float(quantity.rstrip("GBi"))


def test_small_is_the_single_node_trino_the_local_values_file_already_sets() -> None:
    small = load_query_sizing(E2E_REPO_ROOT, Size.SMALL)
    coordinator = _TRINO_VALUES["coordinator"]

    assert small.workers == _TRINO_VALUES["server"]["workers"] == 0
    assert small.worker is None
    assert small.schedule_on_coordinator is coordinator["config"]["nodeScheduler"]["includeCoordinator"]
    assert small.max_memory_per_node == coordinator["config"]["query"]["maxMemoryPerNode"]
    assert small.coordinator.heap == coordinator["jvm"]["maxHeapSize"]
    assert small.coordinator.cpu_request == coordinator["resources"]["requests"]["cpu"]
    assert small.coordinator.memory_request == coordinator["resources"]["requests"]["memory"]
    assert small.coordinator.memory_limit == coordinator["resources"]["limits"]["memory"]
    # The chart's own default, which the values file leaves unset.
    assert small.max_memory == "4GB"


def test_each_size_adds_workers_the_coordinator_stops_scheduling_on() -> None:
    sizing = {size: load_query_sizing(E2E_REPO_ROOT, size) for size in Size}

    assert [sizing[size].workers for size in (Size.SMALL, Size.MEDIUM, Size.LARGE)] == [0, 2, 4]
    assert [sizing[size].schedule_on_coordinator for size in (Size.SMALL, Size.MEDIUM, Size.LARGE)] == [
        True,
        False,
        False,
    ]


@pytest.mark.parametrize("size", list(Size))
def test_every_node_can_hold_the_per_node_query_limit(size: Size) -> None:
    sizing = load_query_sizing(E2E_REPO_ROOT, size)

    for node in (sizing.coordinator, sizing.worker):
        if node is None:
            continue
        assert _gigabytes(sizing.max_memory_per_node) <= _gigabytes(node.heap) * _USABLE_HEAP
        assert _gigabytes(node.memory_request) <= _gigabytes(node.memory_limit)
        assert _gigabytes(node.heap) < _gigabytes(node.memory_limit)


def _write_sizes(root: Path, mutate) -> Path:  # noqa: ANN001
    release = root / "release"
    release.mkdir(parents=True)
    document = yaml.safe_load((E2E_REPO_ROOT / QUERY_SIZES_PATH).read_text())
    mutate(document["sizes"])
    (release / "query-sizes.yaml").write_text(yaml.safe_dump(document))
    return root


@pytest.mark.parametrize(
    ("size", "mutate", "message"),
    [
        (Size.MEDIUM, lambda sizes: sizes.pop("medium"), "defines no size 'medium'"),
        (Size.MEDIUM, lambda sizes: sizes["medium"].pop("worker"), "worker resources are required"),
        (Size.SMALL, lambda sizes: sizes["small"].update(worker=sizes["medium"]["worker"]), "worker resources"),
        (Size.SMALL, lambda sizes: sizes["small"].update(schedule_on_coordinator=False), "must schedule queries"),
        (Size.MEDIUM, lambda sizes: sizes["medium"].update(workers=-1), "non-negative integer"),
        (Size.MEDIUM, lambda sizes: sizes["medium"].update(replicas=3), "unknown field"),
        (Size.MEDIUM, lambda sizes: sizes["medium"]["worker"].pop("heap"), "must set exactly"),
    ],
)
def test_a_malformed_mapping_is_refused_by_name(tmp_path: Path, size: Size, mutate, message: str) -> None:  # noqa: ANN001
    with pytest.raises(QuerySizingError, match=message):
        load_query_sizing(_write_sizes(tmp_path, mutate), size)


def test_a_missing_mapping_is_refused(tmp_path: Path) -> None:
    with pytest.raises(QuerySizingError, match="cannot read"):
        load_query_sizing(tmp_path, Size.SMALL)


def _topology(size: Size):  # noqa: ANN202
    # The profile still accepts only `small` (#324), so the larger tiers are set on the resolved topology.
    document = yaml.safe_load((E2E_REPO_ROOT / "openlakeforge.yaml").read_text())
    return dataclasses.replace(resolve_topology(validate_deployment_profile(document)), query_size=size)


def _context(tmp_path: Path, size: Size) -> DeploymentContext:
    return DeploymentContext.local(repo_root=tmp_path, distribution_root=E2E_REPO_ROOT, topology=_topology(size))


def test_the_root_receives_the_size_as_one_typed_variable(tmp_path: Path) -> None:
    variables = query_variables(_context(tmp_path, Size.MEDIUM))

    sizing = json.loads(variables["query_sizing"])
    assert (sizing["size"], sizing["workers"]) == ("medium", 2)
    assert sizing["worker"] == {"heap": "2G", "cpu_request": "500m", "memory_request": "2Gi", "memory_limit": "3Gi"}


def test_a_size_change_moves_only_the_query_sizing_variable(tmp_path: Path) -> None:
    from olf.infra.deployment.local import platform

    small = platform.platform_apply_variables(_local_config(tmp_path, _topology(Size.SMALL)))
    medium = platform.platform_apply_variables(_local_config(tmp_path, _topology(Size.MEDIUM)))

    assert {key for key in small if small[key] != medium[key]} == {"query_sizing"}
    assert set(small) == set(medium)


def test_a_size_change_never_reaches_the_project_image_or_revision(tmp_path: Path) -> None:
    from olf.common.project import ProjectSpec
    from olf.infra import project_revision
    from olf.infra.deployment.local import platform

    image = "ghcr.io/openlakeforge/openlakeforge-project-code@sha256:" + "a" * 64
    project = tmp_path / "project"
    project.mkdir()
    shutil.copytree(E2E_REPO_ROOT / "lakehouse_code", project / "lakehouse_code")
    base = yaml.safe_load((E2E_REPO_ROOT / "openlakeforge.yaml").read_text())

    profile = validate_deployment_profile(base)
    (project / "openlakeforge.yaml").write_text(yaml.safe_dump(base))

    def resolved(size: Size) -> tuple[object, str, dict[str, str]]:
        revision = project_revision.build_project_revision(
            ProjectSpec(root=project, distribution_root=E2E_REPO_ROOT),
            image=image,
            distribution_version="0.2.0-alpha.1",
        )
        variables = platform.platform_apply_variables(_local_config(tmp_path, _topology(size)))
        image_variables = {key: value for key, value in variables.items() if key.startswith("project_code_image")}
        return resolve_project(profile), revision.revision, image_variables

    small, medium, large = (resolved(size) for size in (Size.SMALL, Size.MEDIUM, Size.LARGE))

    assert small == medium == large


def _local_config(tmp_path: Path, topology):  # noqa: ANN001, ANN202
    from olf.common.deployment.local.config import LocalDeploymentConfig

    context = DeploymentContext.local(repo_root=tmp_path, distribution_root=E2E_REPO_ROOT, topology=topology)
    return LocalDeploymentConfig.from_environment({}, context=context)
