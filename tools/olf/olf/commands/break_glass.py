"""`olf break-glass`: recorded, time-boxed service access without the issuer (#177)."""

from __future__ import annotations

import os

import typer

from olf import break_glass
from olf.commands._shared import deployment_context
from olf.log import fail


def break_glass_command(
    service: str = typer.Argument(..., help=f"One of: {', '.join(break_glass.TARGETS)}."),
    reason: str = typer.Option(..., "--reason", help="Why the issuer path is not used; recorded."),
    ttl_minutes: int = typer.Option(break_glass.DEFAULT_TTL_MINUTES, "--ttl-minutes", min=1, help="Hard stop."),
    local_port: int = typer.Option(0, "--local-port", help="Local port; defaults to the remote one."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Local kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Reach SERVICE through your kubeconfig until you exit or the TTL ends, and record the use.

    Ports bind to 127.0.0.1; Superset and OpenMetadata open a shell in their pod.
    The perimeter and every Ingress are left untouched.
    """
    from olf.tooling.toolkit import Toolkit

    if service not in break_glass.TARGETS:
        raise typer.Exit(code=fail(f"unknown service {service!r}; expected one of {', '.join(break_glass.TARGETS)}"))
    context = deployment_context(
        "local",
        profile="",
        namespace="",
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    )
    env = context.command_env(base=os.environ)
    tools = Toolkit.default(environ=env)
    cluster = break_glass.Cluster(
        tools.kubectl,
        context=context.kube_context,
        kubeconfig=context.paths.kubeconfig_path,
        shared_namespace=context.shared_namespace,
        namespace=context.namespace,
        env=env,
    )
    tls_port = None
    if service == "trino":
        from pathlib import Path

        from olf.contracts import load_provider_contracts

        contract_dir = Path(env.get("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", context.paths.platform_terraform_dir))
        contracts = load_provider_contracts(str(contract_dir.resolve()), environ=env, resolver=tools.resolver)
        tls_port = break_glass.trino_tls_port(contracts, context.stage.value)
    port = break_glass.remote_port(service, tls_port)
    where = f"127.0.0.1:{local_port or port}, {break_glass.scheme(service, tls_port)}" if port else "a shell in the pod"
    typer.echo(f"break-glass: {service} on {where}; at most {ttl_minutes} min; recorded in {cluster.shared_namespace}.")
    name = break_glass.open_session(cluster, service, reason, ttl_minutes, local_port=local_port, tls_port=tls_port)
    typer.echo(f"break-glass: ended; record {cluster.shared_namespace}/{name}")
