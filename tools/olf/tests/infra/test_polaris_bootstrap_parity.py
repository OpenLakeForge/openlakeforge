"""olf's Polaris bootstrap and the Terraform Job it replaces leave the same Polaris and Kubernetes state (#562).

Until the Job is deleted both run on every deploy: the Job during `terraform apply`, olf right after. Each case
runs the Job's real script, rendered from `bootstrap.sh.tftpl` and `person-access.sh.tftpl` as `templatefile`
does, against the same fake Polaris and namespaces as olf's port, and compares what each leaves behind.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from _polaris_fake import ASSIGNED, ROOT, STAGES, STS, Cluster, FakePolaris, conformance_plan, kubectl_shim, serve

from olf.common.tooling.kubectl import Kubectl
from olf.infra import k8s
from olf.infra.polaris import PolarisClient, PolarisConfig
from olf.infra.polaris_bootstrap import PERSON_CATALOG_ROLES, run_bootstrap

TEMPLATES = Path(__file__).resolve().parents[4] / "infra/terraform/modules/catalog/polaris/templates"
_DIRECTIVE = re.compile(r"^%\{ (.*?) ~\}\n", re.M)
_INTERPOLATION = re.compile(r"(?<!\$)\$\{([\w.]+)\}")


def _parse(parts: Iterator[str], end: tuple[str, ...] = ()) -> tuple[list[Any], str]:
    """`re.split` text and directive pieces, as a tree; returns it and the directive that closed it."""
    nodes: list[Any] = []
    for text in parts:
        nodes.append(text)
        directive = next(parts, None)
        if directive is None or directive in end:
            return nodes, directive or ""
        if directive.startswith("for "):
            body, _ = _parse(parts, ("endfor",))
            nodes.append(("for", directive, body, []))
        elif directive.startswith("if "):
            body, closed = _parse(parts, ("else", "endif"))
            otherwise = _parse(parts, ("endif",))[0] if closed == "else" else []
            nodes.append(("if", directive, body, otherwise))
    return nodes, ""


def _render_nodes(nodes: list[Any], scope: dict[str, Any]) -> str:
    def lookup(match: re.Match[str]) -> str:
        value: Any = scope
        for name in match.group(1).split("."):
            value = value[name]
        return str(value)

    out = []
    for node in nodes:
        if isinstance(node, str):
            out.append(_INTERPOLATION.sub(lookup, node))
        elif node[0] == "for":
            key, value, collection = re.fullmatch(r"for (\w+), (\w+) in (\w+)", node[1]).groups()  # type: ignore[union-attr]  # the templates' only loop shape
            for k, v in sorted(scope[collection].items()):
                out.append(_render_nodes(node[2], {**scope, key: k, value: v}))
        else:
            condition = node[1].removeprefix("if ")
            held = scope["sts"] is not None if condition == "sts != null" else bool(scope[condition])
            out.append(_render_nodes(node[2] if held else node[3], scope))
    return "".join(out)


def render(template: str, scope: dict[str, Any]) -> str:
    """Terraform's `templatefile` for the directives these templates use."""
    nodes, _ = _parse(iter(_DIRECTIVE.split(template)))
    return _render_nodes(nodes, scope).replace("$${", "${").replace("%%{", "%{")


def _job_script(*, person_access: bool, sts: bool) -> str:
    """`local.bootstrap_script` for the local root's DEV and PROD stages, as main.tf renders it."""
    stage_catalogs = {
        stage: {
            "namespace": f"olf-{stage}",
            "catalog_name": f"lakehouse_{stage}",
            **{f"{layer}_bucket_name": f"lakehouse-{stage}-{layer}" for layer in ("bronze", "silver", "gold")},
            "trino_principal_name": f"trino-{stage}",
            "trino_credentials_secret_name": f"polaris-{stage}-trino-creds",
            "floe_principal_name": f"floe-{stage}",
            "floe_credentials_secret_name": f"polaris-{stage}-floe-creds",
            "deployer_principal_name": f"deployer-{stage}",
            "deployer_credentials_secret_name": f"polaris-{stage}-deployer-creds",
        }
        for stage in STAGES
    }
    script = render(
        (TEMPLATES / "bootstrap.sh.tftpl").read_text(),
        {
            "release_name": "polaris",
            "oauth_scope": "PRINCIPAL_ROLE:ALL",
            "stage_catalogs": stage_catalogs,
            "sts": STS if sts else None,
            # The environment roots' and the module's own values.
            "principal_role": "data-engineer",
            "catalog_role": "catalog-admin",
            "floe_principal_role": "data-writer",
            "floe_catalog_role": "catalog-writer",
            "om_principal_name": "openmetadata",
            "om_credentials_secret_name": "polaris-om-creds",
            "om_principal_role": "data-reader",
            "om_catalog_role": "catalog-reader",
            "deployer_principal_role": "namespace-admin",
            "deployer_catalog_role": "catalog-namespace-admin",
            "person_access_enabled": person_access,
        },
    )
    if person_access:
        rendered = {
            "principal": "github",
            "catalog_roles": {role: list(privileges) for role, privileges in PERSON_CATALOG_ROLES.items()},
            "catalogs": [f"lakehouse_{stage}" for stage in STAGES],
            "people": [
                {"principal": f"trino-people-{stage}", "secret": f"polaris-{stage}-trino-people-creds"}
                for stage in STAGES
            ],
            "assigned": {"olf-signed-in": {}, **ASSIGNED},
        }
        script += render((TEMPLATES / "person-access.sh.tftpl").read_text(), {"person_access": json.dumps(rendered)})
    return script


class _Stack:
    """One Polaris and one set of namespaces, bootstrapped by the Job's script or by olf."""

    def __init__(self, url: str, fake: FakePolaris, cluster: Cluster, tmp_path: Path, monkeypatch: Any) -> None:
        self.url, self.fake, self.cluster, self.tmp_path, self.monkeypatch = url, fake, cluster, tmp_path, monkeypatch

    def job(self, *, person_access: bool = True, sts: bool = True) -> None:
        script = _job_script(person_access=person_access, sts=sts)
        script = script.replace('polaris_url="http://polaris:8181"', f'polaris_url="{self.url}"')
        script = script.replace("/tmp/polaris-body", str(self.tmp_path / "body"))
        environ = {
            "PATH": f"{kubectl_shim(self.tmp_path / 'bin', self.cluster)}:/usr/bin:/bin",
            "NAMESPACE": "olf-system",
            "ROOT_CLIENT_ID": ROOT[0],
            "ROOT_CLIENT_SECRET": ROOT[1],
        }
        result = subprocess.run(["sh", "-ec", script], capture_output=True, text=True, env=environ, check=False)
        assert result.returncode == 0, result.stderr

    def olf(self, *, person_access: bool = True, sts: bool = True) -> None:
        resolver = Mock()
        resolver.resolve.return_value = self.tmp_path / "kubectl"
        cluster = self.cluster
        self.monkeypatch.setattr(k8s, "ProcessRunner", lambda: cluster)
        self.monkeypatch.setattr(k8s, "_kubectl_executable", lambda *_a: "kubectl")
        kubectl = SimpleNamespace(kubectl=Kubectl(cluster, resolver))
        self.monkeypatch.setattr(k8s.Toolkit, "default", lambda **_k: kubectl)
        client = PolarisClient(PolarisConfig(self.url, "", *ROOT))
        client.login()
        run_bootstrap(
            client, conformance_plan(person_access=person_access, sts=sts), environ={"KUBE_CONTEXT": "kind-test"}
        )

    def state(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.fake.state(), self.cluster.state(self.fake)


@pytest.fixture
def stacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_Stack, _Stack]]:
    first, second = FakePolaris(), FakePolaris()
    with serve(first) as first_url, serve(second) as second_url:
        yield (
            _Stack(first_url, first, Cluster(tmp_path / "a.json"), tmp_path / "a", monkeypatch),
            _Stack(second_url, second, Cluster(tmp_path / "b.json"), tmp_path / "b", monkeypatch),
        )


@pytest.fixture(autouse=True)
def _directories(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()


_SHAPES = pytest.mark.parametrize(
    "shape",
    [{"person_access": True, "sts": True}, {"person_access": False, "sts": False}],
    ids=["github-sign-in-sts", "no-people-static-keys"],
)


@_SHAPES
def test_a_fresh_stack_ends_the_same_whichever_bootstraps_it(stacks: tuple[_Stack, _Stack], shape: dict) -> None:
    job, olf = stacks

    job.job(**shape)
    olf.olf(**shape)

    assert olf.state() == job.state()
    assert olf.state()[1]  # Secrets were written, and each names its principal's current credential
    assert "stale" not in json.dumps(olf.state()[1])


@_SHAPES
def test_olf_after_the_job_changes_nothing(stacks: tuple[_Stack, _Stack], shape: dict) -> None:
    stack, _ = stacks
    stack.job(**shape)
    before, issued = stack.state(), stack.fake.issued

    stack.olf(**shape)

    assert stack.state() == before
    assert stack.fake.issued == issued


@_SHAPES
def test_the_job_after_olf_changes_nothing(stacks: tuple[_Stack, _Stack], shape: dict) -> None:
    stack, _ = stacks
    stack.olf(**shape)
    before, issued = stack.state(), stack.fake.issued

    stack.job(**shape)

    assert stack.state() == before
    assert stack.fake.issued == issued


def _lost_credential(stack: _Stack) -> None:
    stack.cluster.put("configmap", "olf-system", "polaris-om-creds-bootstrap-pending", {"principal": "openmetadata"})


def _removed_stage(stack: _Stack) -> None:
    stack.cluster.save({k: v for k, v in stack.cluster.load().items() if "/olf-prod/" not in k})


def _new_catalog_property(stack: _Stack) -> None:
    catalog = stack.fake.catalogs["lakehouse_dev"]
    catalog["properties"] = {"default-base-location": "s3://lakehouse-dev-silver/", "operator-set": "kept"}


@pytest.mark.parametrize("damage", [_lost_credential, _removed_stage, _new_catalog_property])
def test_both_recover_a_damaged_stack_the_same_way(
    stacks: tuple[_Stack, _Stack], damage: Callable[[_Stack], None]
) -> None:
    job, olf = stacks
    job.job()
    olf.olf()
    damage(job)
    damage(olf)

    job.job()
    olf.olf()

    assert olf.state() == job.state()
    assert "stale" not in json.dumps(olf.state()[1])
