from __future__ import annotations

import pytest

from olf.deployment.cloud.aws import AwsBackend
from olf.deployment.cloud.azure import AzureBackend
from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.deployment.cloud.forward import cloud_forward_spec
from olf.deployment.context import DeploymentContext, Profile


@pytest.mark.parametrize(
    ("context_factory", "backend", "shared_targets"),
    [
        (DeploymentContext.aws, AwsBackend(), {"trino", "openmetadata"}),
        (DeploymentContext.azure, AzureBackend(), {"seaweedfs-s3", "polaris", "trino", "openmetadata"}),
    ],
)
def test_cloud_forward_targets_use_their_service_namespace(
    tmp_path, context_factory, backend, shared_targets  # noqa: ANN001
) -> None:
    context = context_factory(repo_root=tmp_path, profile=Profile.FULL)
    config = CloudDeploymentConfig.from_environment({}, context=context)

    spec = cloud_forward_spec(config, backend, kube_context="cloud-context")

    namespaces = {target.label: target.namespace or spec.namespace for target in spec.targets}
    assert {label: namespaces[label] for label in shared_targets} == {
        label: "olf-system" for label in shared_targets
    }
    assert namespaces["dagster"] == "olf-dev"
    assert namespaces["superset"] == "olf-dev"
