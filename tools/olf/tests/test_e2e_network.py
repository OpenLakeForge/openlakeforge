from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from olf.e2e import _network
from olf.e2e._network import PROTECTED, positive_probes, rbac_denials, stage_wide, workload_port
from olf.e2e._shell import E2EError

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


@pytest.mark.parametrize(("stdout", "expected"), [("yes\n", True), ("no\n", False), ("no - RBAC: denied\n", False), ("", None), ("Error", None)])
def test_can_i_fails_instead_of_reading_an_error_as_no(monkeypatch: pytest.MonkeyPatch, stdout: str, expected) -> None:  # noqa: ANN001
    monkeypatch.setattr(_network.k8s, "_kubectl", lambda *_a, **_k: stdout)
    cfg = SimpleNamespace(kube_context="kind-x")
    if expected is None:
        with pytest.raises(E2EError, match="no answer"):
            _network._can_i(cfg, "system:serviceaccount:olf-dev:x", "list", "secrets")  # type: ignore[arg-type]
    else:
        assert _network._can_i(cfg, "system:serviceaccount:olf-dev:x", "list", "secrets") is expected  # type: ignore[arg-type]
