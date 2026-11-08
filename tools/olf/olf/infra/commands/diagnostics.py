"""Best-effort diagnostics collection using structured subprocess argv."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from functools import partial
from pathlib import Path

import typer

from olf.common import config
from olf.common.contract_state import load_provider_contracts
from olf.common.tooling.toolkit import Toolkit
from olf.infra import trino

app = typer.Typer(help="Collect bounded deployment diagnostics.")
_OUTPUT_ARGUMENT = typer.Argument(..., help="Directory to receive text diagnostics.")


def _host_memory_snapshot() -> str:
    """Return host memory evidence without requiring a platform-specific shell utility."""
    try:
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            return meminfo.read_text()
        page_size = os.sysconf("SC_PAGE_SIZE")
        physical_pages = os.sysconf("SC_PHYS_PAGES")
        return f"total={page_size * physical_pages}\n"
    except (OSError, ValueError, AttributeError) as exc:
        return f"unavailable: {type(exc).__name__}: {exc}\n"


def _capture(tools: Toolkit, output: Path, name: str, argv_factory: Callable[[], list[str]]) -> str:
    """Capture one diagnostic command, including command-resolution failures."""
    try:
        argv = argv_factory()
        result = tools.runner.run(argv, check=False)
        text = result.stdout + result.stderr
    except Exception as exc:  # Diagnostics must preserve partial evidence.
        text = f"{type(exc).__name__}: {exc}\n"
    (output / name).write_text(text)
    return text


# The coordinator's rendered configuration, for the authentication and TLS
# listeners it actually runs with. The files hold ${ENV:...} references, never
# secret values.
_TRINO_CONFIG = (
    'for f in config.properties password-authenticator.properties; do echo "--- $f ---"; cat "/etc/trino/$f" 2>&1; done'
)


def _coordinator_namespaces(selected: str) -> list[str]:
    """The namespaces holding a stage's Trino coordinator, from the contract endpoints; `selected` without one."""
    directory = config.env("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR")
    if not directory:
        return [selected]
    try:
        contracts = load_provider_contracts(directory)
    except Exception:  # Diagnostics must preserve partial evidence.
        return [selected]
    stages = (contracts or {}).get("stages") or {}
    found = [trino.contract_service(contracts, stage, namespace=selected)[1] for stage in stages]
    return list(dict.fromkeys(found)) or [selected]


@app.command("collect")
def collect(
    output_dir: Path = _OUTPUT_ARGUMENT,
    namespace: str = typer.Option("", "--namespace"),
    kube_context: str = typer.Option("", "--kube-context"),
) -> None:
    """Collect host, Docker, node, workload, event, and bounded pod-log evidence."""
    output_dir.mkdir(parents=True, exist_ok=True)
    selected_namespace = namespace or config.namespace()
    selected_context = kube_context or os.environ.get("KUBE_CONTEXT", "")
    disk = shutil.disk_usage(output_dir)
    (output_dir / "host-disk.txt").write_text(f"total={disk.total}\nused={disk.used}\nfree={disk.free}\n")
    (output_dir / "host-memory.txt").write_text(_host_memory_snapshot())
    tools = Toolkit.default()
    context = ["--context", selected_context] if selected_context else []

    def command(tool: str, *args: str) -> list[str]:
        return [str(tools.resolver.resolve(tool)), *args]

    _capture(tools, output_dir, "docker-disk.txt", lambda: command("docker", "system", "df"))
    _capture(tools, output_dir, "nodes.txt", lambda: command("kubectl", *context, "get", "nodes", "-o", "wide"))
    _capture(tools, output_dir, "all-pods.txt", lambda: command("kubectl", *context, "get", "pods", "-A", "-o", "wide"))
    _capture(
        tools,
        output_dir,
        "all-events.txt",
        lambda: command("kubectl", *context, "get", "events", "-A", "--sort-by=.lastTimestamp"),
    )
    _capture(
        tools,
        output_dir,
        "workloads.txt",
        lambda: command("kubectl", *context, "get", "all", "-n", selected_namespace, "-o", "wide"),
    )
    _capture(
        tools,
        output_dir,
        "events.txt",
        lambda: command("kubectl", *context, "get", "events", "-n", selected_namespace, "--sort-by=.lastTimestamp"),
    )
    _capture(
        tools,
        output_dir,
        "pod-descriptions.txt",
        lambda: command("kubectl", *context, "describe", "pods", "-n", selected_namespace),
    )
    pods = _capture(
        tools,
        output_dir,
        "pod-names.txt",
        lambda: command("kubectl", *context, "get", "pods", "-n", selected_namespace, "-o", "name"),
    )
    for item in pods.splitlines():
        if item.startswith("pod/"):
            pod = item.removeprefix("pod/")
            logs = ["logs", "-n", selected_namespace, pod, "--all-containers", "--tail=200"]
            _capture(tools, output_dir, f"{pod}.log", partial(command, "kubectl", *context, *logs))
    coordinator_namespaces = _coordinator_namespaces(selected_namespace)
    for coordinator_namespace in coordinator_namespaces:
        listed = (
            pods
            if coordinator_namespace == selected_namespace
            else _capture(
                tools,
                output_dir,
                f"pod-names-{coordinator_namespace}.txt",
                partial(command, "kubectl", *context, "get", "pods", "-n", coordinator_namespace, "-o", "name"),
            )
        )
        suffix = "" if len(coordinator_namespaces) == 1 else f"-{coordinator_namespace}"
        for item in listed.splitlines():
            if item.removeprefix("pod/").startswith("trino-coordinator"):
                exec_args = [
                    "exec",
                    "-n",
                    coordinator_namespace,
                    item.removeprefix("pod/"),
                    "--",
                    "sh",
                    "-c",
                    _TRINO_CONFIG,
                ]
                _capture(
                    tools, output_dir, f"trino-config{suffix}.txt", partial(command, "kubectl", *context, *exec_args)
                )
    typer.echo(f"Diagnostics written to {output_dir}")
