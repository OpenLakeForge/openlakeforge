from __future__ import annotations

import json
from pathlib import Path

from olf.infra.deployment.cloud.cluster_name import DEFAULT_AWS_CLUSTER_NAME, aws_cluster_name


def _state(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "terraform.tfstate"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_the_environment_wins_over_a_recorded_name(tmp_path: Path) -> None:
    state = _state(tmp_path, {"outputs": {"cluster_name": {"value": "recorded"}}})

    assert aws_cluster_name({"AWS_CLUSTER_NAME": "chosen"}, state) == "chosen"


def test_a_recorded_output_is_kept(tmp_path: Path) -> None:
    state = _state(tmp_path, {"outputs": {"cluster_name": {"value": "limited-eks-openlakeforge-poc"}}})

    assert aws_cluster_name({}, state) == "limited-eks-openlakeforge-poc"


def test_a_cluster_resource_names_it_when_outputs_were_never_written(tmp_path: Path) -> None:
    state = _state(
        tmp_path,
        {
            "outputs": {},
            "resources": [
                {"type": "aws_vpc", "instances": [{"attributes": {"id": "vpc-1"}}]},
                {"type": "aws_eks_cluster", "instances": [{"attributes": {"name": "half-created"}}]},
            ],
        },
    )

    assert aws_cluster_name({}, state) == "half-created"


def test_no_state_unreadable_state_and_empty_values_fall_back_to_the_default(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.tfstate"
    corrupt.write_text("{not json", encoding="utf-8")

    assert aws_cluster_name({}) == DEFAULT_AWS_CLUSTER_NAME
    assert aws_cluster_name({}, tmp_path / "missing.tfstate") == DEFAULT_AWS_CLUSTER_NAME
    assert aws_cluster_name({}, corrupt) == DEFAULT_AWS_CLUSTER_NAME
    assert aws_cluster_name({"AWS_CLUSTER_NAME": ""}, _state(tmp_path, {"outputs": {}})) == DEFAULT_AWS_CLUSTER_NAME
