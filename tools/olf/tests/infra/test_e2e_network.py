import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from olf.infra.e2e import _network
from olf.infra.e2e._network import PROTECTED, positive_probes, rbac_denials, stage_wide, workload_ports
from olf.infra.e2e._shell import E2EError

FLOWS = yaml.safe_load((Path(__file__).parents[4] / "release/network-flows.yaml").read_text())


def test_positive_probes_cover_pod_to_pod_t_rows() -> None:
    probes = positive_probes(FLOWS)
    assert probes
    assert all(destination["scope"] in {"shared", "apiserver"} for _, _, destination, _ in probes)
    # F9: any stage pod to Polaris is a T row and needs no source labels.
    assert any(scope == "stage" and not labels and port == 8181 for scope, labels, _, port in probes)


def test_every_protected_backend_has_a_port() -> None:
    assert {workload: workload_ports(FLOWS, workload) for workload in PROTECTED} == {
        "postgresql": [5432],
        "polaris": [8181],
        "trino": [8080, 8443],
        "openmetadata": [8585],
        "dex": [5556],
    }


def test_rbac_denials_exempt_only_documented_cluster_secret_readers() -> None:
    assert ("list", "secrets", "--all-namespaces") in rbac_denials("olf-system", "polaris-bootstrap")
    assert ("list", "secrets", "--all-namespaces") not in rbac_denials("olf-system", "traefik")
    assert ("create", "pods", "-n", "olf-dev") in rbac_denials("olf-dev", "default")
    assert ("escalate", "clusterroles") in rbac_denials("olf-system", "traefik")


def test_stage_wide_backends_are_the_f6_f9_f10_f12_destinations() -> None:
    assert stage_wide(FLOWS) == {"postgresql", "polaris", "trino", "openmetadata"}


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("yes\n", True), ("no\n", False), ("no - RBAC: denied\n", False), ("", None), ("Error", None)],
)
def test_can_i_fails_instead_of_reading_an_error_as_no(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: bool | None
) -> None:
    monkeypatch.setattr(_network.k8s, "_kubectl", lambda *_a, **_k: stdout)
    cfg = SimpleNamespace(kube_context="kind-x")
    if expected is None:
        with pytest.raises(E2EError, match="no answer"):
            _network._can_i(cfg, "system:serviceaccount:olf-dev:x", "list", "secrets")  # type: ignore[arg-type]
    else:
        assert _network._can_i(cfg, "system:serviceaccount:olf-dev:x", "list", "secrets") is expected  # type: ignore[arg-type]


def _grants(rules: list[dict], subject: tuple[str, str], kind: str = "Role", role: str = "r") -> list[str]:
    binding = {
        "metadata": {"namespace": "olf-system"},
        "roleRef": {"kind": kind, "name": role},
        "subjects": [{"kind": "ServiceAccount", "namespace": subject[0], "name": subject[1]}],
    }
    return _network.grant_problems([binding], lambda *_: {"rules": rules}, [subject, ("olf-dev", "default")])


def _rule(resources: list[str], verbs: list[str], names: list[str] | None = None) -> dict:
    return {"resources": resources, "verbs": verbs, **({"resourceNames": names} if names else {})}


def test_create_secrets_and_replicasets_count() -> None:
    sa = ("olf-system", "worker")
    assert _grants([_rule(["secrets"], ["create"])], sa)
    assert _grants([_rule(["replicasets"], ["create"])], sa)
    assert _grants([_rule(["replicationcontrollers"], ["create"])], sa)


def test_exception_is_compared_by_tuple_not_name_prefix() -> None:
    risky = [_rule(["secrets"], ["get"])]
    assert not _grants(risky, ("olf-system", "traefik"), "ClusterRole", "traefik")
    assert _grants(risky, ("olf-system", "worker"), "ClusterRole", "traefik-evil")  # wrong subject
    assert _grants(risky, ("olf-system", "traefik"), "Role", "traefik")  # wrong kind
    assert _grants([_rule(["secrets", "deployments"], ["create"])], ("olf-system", "traefik"), "ClusterRole", "traefik")


def test_escalate_and_bind_probed_on_roles_and_clusterroles() -> None:
    denied = rbac_denials("olf-dev", "worker")
    for verb in ("escalate", "bind"):
        assert (verb, "roles", "-n", "olf-dev") in denied
        assert (verb, "clusterroles") in denied


def test_serviceaccount_groups_reach_every_olf_account() -> None:
    accounts = [("olf-system", "a"), ("olf-dev", "default"), ("other", "x")]
    binding = {"subjects": [{"kind": "Group", "name": "system:serviceaccounts"}]}
    assert _network._subjects(binding, accounts) == sorted(accounts)
    binding = {"subjects": [{"kind": "Group", "name": "system:serviceaccounts:olf-dev"}]}
    assert _network._subjects(binding, accounts) == [("olf-dev", "default")]


def test_secret_pins_must_match_the_documented_names() -> None:
    sa = ("olf-system", "polaris-bootstrap")
    assert not _grants([_rule(["secrets"], ["create"]), _rule(["secrets"], ["get"], ["polaris-dev-trino-creds"])], sa)
    assert _grants([_rule(["secrets"], ["get"], ["postgresql"])], sa)
    assert _grants([_rule(["secrets"], ["get"], ["anything"])], ("olf-system", "worker"))


def test_service_accounts_cover_every_deployed_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(_cfg: object, argv: list[str], **_k: object) -> str:
        if argv[:2] == ["get", "namespace"]:
            return "" if argv[2] == "olf-prod" else f"namespace/{argv[2]}"
        return json.dumps({"items": [{"metadata": {"name": f"sa-{argv[argv.index('-n') + 1]}"}}]})

    monkeypatch.setattr(_network, "kubectl", fake)
    cfg = SimpleNamespace(namespace="olf-dev", platform_namespace="olf-system")
    found = {ns for ns, _ in _network._service_accounts(cfg)}  # type: ignore[arg-type]
    assert "olf-uat" in found
    assert "olf-prod" not in found


def test_untrusted_namespace_is_deleted_and_awaited_before_create(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake(_cfg: object, argv: list[str], **_k: object) -> str:
        calls.append(argv)
        return ""

    def stop(*_a: object) -> None:
        raise RuntimeError("stop")

    monkeypatch.setattr(_network, "kubectl", fake)
    monkeypatch.setattr(_network, "_pod_ip", lambda *_a: "10.0.0.1")
    monkeypatch.setattr(_network, "workload_ports", lambda *_a: [80])
    monkeypatch.setattr(_network, "_probe_pod", stop)
    cfg = SimpleNamespace(namespace="olf-dev", platform_namespace="olf-system")
    with pytest.raises(RuntimeError):
        _network.check_negative_flows(cfg, FLOWS)  # type: ignore[arg-type]
    delete = next(i for i, c in enumerate(calls) if c[:3] == ["delete", "namespace", _network.UNTRUSTED_NAMESPACE])
    assert "--wait=false" not in calls[delete]
    assert calls[delete + 1][:2] == ["create", "namespace"]


def test_label_drift_flags_pods_missing_flow_labels() -> None:
    workloads = {"trino": {"app": "trino", "component": "coordinator"}}

    def pod(labels: dict[str, str]) -> dict:
        return {"metadata": {"labels": labels}}

    assert _network.label_drift(workloads, [pod({"app": "trino", "component": "worker"})])
    assert not _network.label_drift(workloads, [pod({"app": "trino", "component": "coordinator", "x": "y"})])
    assert not _network.label_drift(workloads, [])


def test_cert_manager_documented_grants_pass() -> None:
    cm = ("olf-system", "cert-manager")
    wh = ("olf-system", "cert-manager-webhook")
    pods = [_rule(["pods", "services"], ["create"])]
    assert not _grants(pods, cm, "ClusterRole", "cert-manager-controller-challenges")
    assert not _grants([_rule(["secrets"], ["create"])], wh, "Role", "cert-manager-webhook:dynamic-serving")
