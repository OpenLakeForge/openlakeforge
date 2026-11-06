"""The Full DEV+PROD contract the backup tests plan from."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import CONFORMANCE_PROFILE

from olf.common.profile import StageName
from olf.common.provider_contracts import ProviderContracts, parse_provider_contracts
from olf.infra.commands._shared import deployment_context_for_profile

# Captured from the local root applied with openlakeforge.conformance.yaml: Full, DEV and PROD, GitHub sign-in.
_CONTRACT = json.loads((Path(__file__).parents[1] / "fixtures" / "conformance-provider-contracts.json").read_text())


def full_contracts() -> ProviderContracts:
    topology = deployment_context_for_profile(str(CONFORMANCE_PROFILE), stage=StageName.DEV.value).topology
    return parse_provider_contracts(_CONTRACT, topology)
