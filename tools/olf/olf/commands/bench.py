"""Benchmarks of a local deployment: install footprint and end-to-end ETL."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import typer

from olf import log
from olf.commands._shared import applied_runtime_environment, deployment_context, fail

app = typer.Typer(help="Reproducible benchmarks of a local deployment; each run writes a JSON result.")

_OUTPUT_HELP = "Result directory; defaults to <project-root>/.tmp/bench/<scenario>-<UTC time>."


@app.command("footprint")
def footprint_command(
    clean: bool = typer.Option(False, "--clean", help="Destroy any existing local deployment first."),
    settle_seconds: int = typer.Option(120, "--settle-seconds", help="Wait after the first pipeline before idling."),
    idle_seconds: int = typer.Option(300, "--idle-seconds", help="Length of the steady-state sample window."),
    sample_interval: float = typer.Option(5.0, "--sample-interval", help="Seconds between node usage samples."),
    output: str = typer.Option("", "--output", help=_OUTPUT_HELP),
    project_root: str = typer.Option("", "--project-root", help="Project root; defaults to the current directory."),
    cluster_name: str = typer.Option("openlakeforge-local", "--cluster-name", help="Local kind cluster name."),
) -> None:
    """Scenario A: phase-by-phase install, first query, first pipeline, installed and idle footprint."""
    from olf import bench

    _run(
        "footprint",
        project_root,
        cluster_name,
        output,
        lambda record, target, session: bench.footprint(
            record,
            target,
            session=session,
            clean=clean,
            settle_seconds=settle_seconds,
            idle_seconds=idle_seconds,
            sample_interval=sample_interval,
        ),
        clean=clean,
    )


@app.command("etl")
def etl_command(
    scale: int = typer.Option(100, "--scale", help="Replicas of the CSV fixtures to load."),
    delta: int = typer.Option(10, "--delta", help="Replicas added for the delta reload; 0 skips it."),
    sample_interval: float = typer.Option(2.0, "--sample-interval", help="Seconds between node usage samples."),
    job_timeout_seconds: int = typer.Option(3600, "--job-timeout-seconds", help="Limit for one product job."),
    output: str = typer.Option("", "--output", help=_OUTPUT_HELP),
    project_root: str = typer.Option("", "--project-root", help="Project root; defaults to the current directory."),
    cluster_name: str = typer.Option("openlakeforge-local", "--cluster-name", help="Local kind cluster name."),
) -> None:
    """Scenario B: a scaled dataset through every product job, a no-change rerun and a delta reload."""
    from olf import bench

    _run(
        f"etl-x{scale}",
        project_root,
        cluster_name,
        output,
        lambda record, target, session: bench.etl(
            record, target, session=session, scale=scale, delta=delta, sample_interval=sample_interval
        ),
        job_timeout_seconds=job_timeout_seconds,
        scale=scale,
        delta=delta,
    )


def _run(
    scenario: str,
    project_root: str,
    cluster_name: str,
    output: str,
    body: Callable[..., None],
    *,
    job_timeout_seconds: int = 3600,
    **parameters: object,
) -> None:
    from olf import bench

    context = deployment_context(
        "local", profile="", namespace="", cluster_name=cluster_name, project_root=project_root
    )
    root = context.paths.repo_root
    output_dir = (
        Path(output)
        if output
        else root / ".tmp" / "bench" / f"{scenario}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    )
    target = bench.Target(
        project_root=root,
        cluster_name=cluster_name,
        kubeconfig_path=context.paths.kubeconfig_path,
        output_dir=output_dir.resolve(),
        job_timeout_seconds=job_timeout_seconds,
    )
    environment = bench.environment(context.paths.distribution_root)
    environment["parameters"] = {"cluster_name": cluster_name, "preset": context.topology.preset.value, **parameters}
    record = bench.BenchRecord(scenario, environment=environment)
    try:
        body(record, target, _session(cluster_name, context.paths.kubeconfig_path))
    except Exception as exc:  # noqa: BLE001 - every failure is recorded before the run exits non-zero
        log.error(f"{scenario}: {exc}")
    finally:
        path = record.write(target.output_dir / "result.json")
        for item in record.measurements:
            seconds = "" if item.seconds is None else f"{item.seconds:>9.1f}s"
            log.info(f"  {item.status:<7} {seconds:>10}  {item.name}")
        log.info(f"Result ({record.status}): {path}")
    if record.status != "ok":
        raise typer.Exit(code=fail(f"benchmark {scenario} did not complete; the result records where it stopped."))


def _session(cluster_name: str, kubeconfig_path: Path):  # noqa: ANN202
    from olf.e2e._runner import prepare_config
    from olf.e2e._shell import E2EConfig

    @contextmanager
    def session(project_root: Path) -> Iterator[E2EConfig]:
        context = deployment_context(
            "local",
            profile="",
            namespace="",
            cluster_name=cluster_name,
            kubeconfig_path=str(kubeconfig_path),
            project_root=str(project_root),
        )
        with applied_runtime_environment(context, env="local") as kube_context:
            yield prepare_config(
                "local",
                suite="smoke",
                namespace=context.namespace,
                shared_namespace=context.shared_namespace,
                kube_context=kube_context,
                repo_root=context.paths.repo_root,
                distribution_root=context.paths.distribution_root,
            )

    return session
