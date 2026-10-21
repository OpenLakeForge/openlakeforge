from pathlib import Path

import yaml

from olf.e2e._network import PROTECTED, positive_probes, rbac_denials, stage_wide, workload_port

FLOWS = yaml.safe_load((Path(__file__).parents[3] / "release/network-flows.yaml").read_text())


def test_positive_probes_cover_pod_to_pod_t_rows() -> None:
    probes = positive_probes(FLOWS)
    assert probes
    assert all(destination["scope"] in {"shared", "apiserver"} for _, _, destination, _ in probes)
    # F9: any stage pod to Polaris is a T row and needs no source labels.
    assert any(scope == "stage" and not labels and port == 8181 for scope, labels, _, port in probes)


def test_every_protected_backend_has_a_port() -> None:
    assert {workload: workload_port(FLOWS, workload) for workload in PROTECTED} == {
        "postgresql": 5432,
        "polaris": 8181,
        "trino": 8080,
        "openmetadata": 8585,
        "dex": 5556,
    }


def test_rbac_denials_exempt_only_documented_cluster_secret_readers() -> None:
    assert ("list", "secrets", "--all-namespaces") in rbac_denials("olf-system", "polaris-bootstrap")
    assert ("list", "secrets", "--all-namespaces") not in rbac_denials("olf-system", "traefik")
    assert ("create", "pods", "-n", "olf-dev") in rbac_denials("olf-dev", "default")
    assert ("escalate", "clusterroles") in rbac_denials("olf-system", "traefik")


def test_stage_wide_backends_are_the_f6_f9_f10_f12_destinations() -> None:
    assert stage_wide(FLOWS) == {"postgresql", "polaris", "trino", "openmetadata"}
