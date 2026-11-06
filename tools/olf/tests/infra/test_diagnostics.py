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


def test_collect_probes_the_trino_coordinator_for_its_authentication_mode(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
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
    assert "Authorization: Basic" in script and "/etc/trino/config.properties" in script
    assert (tmp_path / "trino-probe.txt").is_file()


def test_the_trino_probe_script_sends_a_wrong_password_and_then_none() -> None:
    import socketserver
    import subprocess
    import threading

    seen: list[bytes] = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            seen.append(self.request.recv(4096))
            self.request.sendall(b"HTTP/1.1 401 Unauthorized\r\n\r\nreply")

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        script = diagnostics._TRINO_PROBE.split("echo '--- config")[0]
        result = subprocess.run(
            ["bash", "-c", script],
            env={"PROBE_PORT": str(server.server_address[1])},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        server.shutdown()

    assert result.stdout.count("reply") == 2
    assert b"Authorization: Basic cHJvYmU6d3Jvbmc=\r\n" in seen[0]
    assert b"Authorization" not in seen[1]
    assert b"\r\n\r\nSELECT 1" in seen[0]
