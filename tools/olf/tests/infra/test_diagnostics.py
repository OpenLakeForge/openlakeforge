from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from olf.common.deployment.errors import ExecutableNotFoundError
from olf.infra.commands import diagnostics


def test_collect_keeps_kubernetes_diagnostics_when_docker_is_missing(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    calls: list[list[str]] = []

    class Resolver:
        def resolve(self, name: str) -> Path:
            if name == "docker":
                raise ExecutableNotFoundError(name)
            assert name == "kubectl"
            return Path("kubectl")

    class Runner:
        def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            calls.append(argv)
            return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(diagnostics.Toolkit, "default", lambda: SimpleNamespace(resolver=Resolver(), runner=Runner()))
    monkeypatch.setattr(diagnostics, "_host_memory_snapshot", lambda: "total=123\navailable=45\n")

    diagnostics.collect(tmp_path, namespace="lakehouse")

    assert "ExecutableNotFoundError" in (tmp_path / "docker-disk.txt").read_text()
    assert (tmp_path / "host-memory.txt").read_text() == "total=123\navailable=45\n"
    assert (tmp_path / "all-events.txt").is_file()
    assert any(call[0] == "kubectl" for call in calls)


def test_collect_dumps_the_trino_coordinator_configuration(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    calls: list[list[str]] = []

    class Resolver:
        def resolve(self, name: str) -> Path:
            return Path(name)

    class Runner:
        def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            calls.append(argv)
            names = "pod/trino-coordinator-abc\npod/other-1\n" if "name" in argv else ""
            return SimpleNamespace(stdout=names, stderr="")

    monkeypatch.setattr(diagnostics.Toolkit, "default", lambda: SimpleNamespace(resolver=Resolver(), runner=Runner()))

    diagnostics.collect(tmp_path, namespace="olf-system")

    probes = [call for call in calls if "exec" in call]
    assert len(probes) == 1
    assert probes[0][probes[0].index("exec") + 1 : -3] == ["-n", "olf-system", "trino-coordinator-abc", "--"]
    script = probes[0][-1]
    assert "config.properties" in script and "password-authenticator.properties" in script
    assert (tmp_path / "trino-config.txt").is_file()


def test_collect_dumps_the_configuration_of_each_stage_coordinator(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    calls: list[list[str]] = []

    class Resolver:
        def resolve(self, name: str) -> Path:
            return Path(name)

    class Runner:
        def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            calls.append(argv)
            return SimpleNamespace(stdout="pod/trino-coordinator-x\n" if "name" in argv else "", stderr="")

    contracts = {
        "stages": {
            "dev": {"query": {"endpoint": "http://trino-dev.olf-dev:8080"}},
            "prod": {"query": {"endpoint": "http://trino-prod.olf-prod:8080"}},
        }
    }
    monkeypatch.setenv("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostics, "load_provider_contracts", lambda _dir: contracts)
    monkeypatch.setattr(diagnostics.Toolkit, "default", lambda: SimpleNamespace(resolver=Resolver(), runner=Runner()))

    diagnostics.collect(tmp_path, namespace="olf-system")

    probed = [call[call.index("exec") + 2] for call in calls if "exec" in call]
    assert probed == ["olf-dev", "olf-prod"]
    assert (tmp_path / "trino-config-olf-prod.txt").is_file()
