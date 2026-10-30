"""End-to-end environment validation."""

from __future__ import annotations

import typer

from olf.common.log import fail
from olf.infra.commands._shared import applied_runtime_environment, deployment_context, deployment_context_for_profile

app = typer.Typer(help="End-to-end environment validation.")


@app.command("run")
def e2e_run(
    env: str = typer.Option(..., "--env", help="Environment to validate: local, azure, or aws."),
    suite: str = typer.Option("", "--suite", help="Suite to run: full or smoke. Defaults to full."),
    checks: str = typer.Option(
        "all",
        "--checks",
        envvar="OLF_E2E_CHECKS",
        help="Full-suite checks: 'core' skips the drills (Polaris and Traefik restart recovery, certificate renewal, "
        "network flows and RBAC, break-glass, perimeter latency, issuer outage, secret rotation); 'all' runs them.",
    ),
    stage: str = typer.Option("", "--stage", help="Stage to validate: dev, uat, or prod. Defaults to dev."),
    profile: str = typer.Option(
        "",
        "--profile",
        help="Deprecated single-DEV preset shorthand: 'full' or 'slim'. Pass the value `olf deploy --profile` used.",
    ),
    profile_file: str = typer.Option(
        "",
        "--file",
        "-f",
        help="Deployment Profile v1 path the deployment was created from. Omit when the project-root "
        "openlakeforge.yaml is authoritative.",
    ),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Run end-to-end validation for a deployed OpenLakeForge environment.

    Self-sufficient: resolves and applies the provider-contract environment
    itself (the in-process equivalent of sourcing `scripts/contracts/
    load-runtime-env.sh` before this ran), so `olf e2e run --env aws` needs
    no shell wrapper around it.

    Resolves the same installed-vs-source runtime layout `olf deploy` does
    (`deployment_context`) - an installed distribution's Terraform roots,
    kubeconfig, and descriptors live under `OLF_HOME`/the extracted payload,
    not the caller's current directory, so validating the deployment just
    created requires the identical path resolution `olf deploy` used.

    `-f` names the Deployment Profile the deployment was applied from, the
    same way `olf platform apply -f` and `olf project deploy -f` do.
    """
    from olf.common.deployment.errors import DeploymentError
    from olf.infra import e2e

    valid_envs = {"local", "azure", "aws"}
    valid_suites = {"", "full", "smoke"}
    if env not in valid_envs:
        raise typer.Exit(code=fail(f"unknown --env {env!r}; expected one of: {', '.join(sorted(valid_envs))}."))
    if checks not in {"core", "all"}:
        raise typer.Exit(code=fail(f"unknown --checks {checks!r}; expected 'core' or 'all'."))
    if suite not in valid_suites:
        raise typer.Exit(code=fail(f"unknown --suite {suite!r}; expected 'full' or 'smoke'."))

    # A v3 contract records the profile name the deployment resolved, and
    # `_parse_v3` refuses a contract whose name disagrees with the topology
    # this command resolves. `-f` is what lets a deployment driven from a
    # profile elsewhere - `olf platform apply -f`, `olf project deploy -f` -
    # be validated against that same file instead of the project root, and
    # `--profile` does the same for `olf deploy --profile`, whose shorthand
    # topology is named "legacy" rather than after the project profile.
    if profile and profile_file:
        raise typer.Exit(code=fail("--profile and --file are mutually exclusive."))
    if profile_file:
        context = deployment_context_for_profile(profile_file, stage=stage)
        if context.provider.value != env:
            raise typer.Exit(
                code=fail(f"{profile_file} targets provider {context.provider.value!r}, not --env {env!r}.")
            )
    else:
        context = deployment_context(
            env, profile=profile, namespace="", cluster_name="", project_root=project_root, stage=stage
        )
    try:
        with applied_runtime_environment(context, env=env) as kube_context:
            e2e.run(
                env,  # type: ignore[arg-type]
                suite=suite or None,  # type: ignore[arg-type]
                checks=checks,  # type: ignore[arg-type]
                namespace=context.namespace,
                shared_namespace=context.shared_namespace,
                kube_context=kube_context,
                repo_root=context.paths.repo_root,
                distribution_root=context.paths.distribution_root,
                deployment=context,
            )
    except (e2e.E2EError, DeploymentError) as exc:
        # DeploymentError covers a managed-toolchain provisioning failure
        # (#127) raised while resolving the provider-contract environment,
        # before e2e.run() (and its own E2EError-only preflight) is ever
        # reached - it must fail the same clean way, not escape as a raw
        # traceback.
        raise typer.Exit(code=fail(str(exc))) from exc
