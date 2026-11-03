"""Dagster's pods name their stage on every deploy path, not only after `olf project` activation.

libs/bronze_dlt.py refuses to load without OPENLAKEFORGE_STAGE, which names each dlt pipeline and its
state. Project activation set it, but the Dagster module's own environment did not, so a platform deploy
(`olf infra deploy --phase artifacts`) failed every dlt Bronze load with "OPENLAKEFORGE_STAGE is not set".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TERRAFORM = Path(__file__).resolve().parents[4] / "infra/terraform"
ROOTS = ("local", "aws-poc", "azure-poc")


def test_the_runtime_environment_carries_the_stage() -> None:
    main = (TERRAFORM / "modules/orchestration/dagster/main.tf").read_text()
    namespace_env = main[main.index("namespace_env = [") : main.index("]", main.index("namespace_env = ["))]

    assert re.search(r'name\s*=\s*"OPENLAKEFORGE_STAGE"\s*\n\s*value\s*=\s*var\.stage', namespace_env)
    assert "local.namespace_env" in main[main.index("runtime_env = concat(") :].split("\n", 1)[0]


@pytest.mark.parametrize("root", ROOTS)
def test_every_root_passes_each_stage_its_own_name(root: str) -> None:
    main = (TERRAFORM / f"environments/{root}/main.tf").read_text()
    block = main[main.index('source   = "../../modules/orchestration/dagster"') :]
    block = block[: block.index("\n}")]

    assert "for_each = local.enabled_stages" in block
    assert re.search(r"^\s*stage\s*=\s*each\.key\s*$", block, re.MULTILINE)


def test_run_pods_carry_the_default_resources() -> None:
    module = TERRAFORM / "modules/orchestration/dagster"
    release = (module / "release.tf").read_text()
    variables = (module / "variables.tf").read_text()
    default = variables[variables.index('variable "run_pod_resources"') :].split("\n}\n", 1)[0]

    assert re.search(r"containerConfig = \{\s*resources\s*=\s*var\.run_pod_resources", release)
    assert 'memory = "512Mi"' in default and 'memory = "2Gi"' in default
