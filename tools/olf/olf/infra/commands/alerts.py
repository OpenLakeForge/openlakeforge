"""`olf infra alerts bind|bindings|test`: bind receiver webhook URLs (#1425) and prove a receiver works (#1431)."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import typer

from olf.common.log import fail
from olf.infra.commands.source import _stdin_is_tty
from olf.infra.deployment.source_bind import SourceBindingError, StageCluster, apply_manifest

app = typer.Typer(help="Bind alert receivers' webhook URLs in the shared namespace, and test a receiver.")

_PROFILE = typer.Option(..., "--file", "-f", help="Deployment Profile v1 path.")


def _context(profile_file: str):  # noqa: ANN202
    from olf.infra.commands._shared import deployment_context_for_profile

    return deployment_context_for_profile(profile_file)


def _cluster(context) -> StageCluster:  # noqa: ANN001
    from olf.infra.commands.project import _profile_provider

    provider = _profile_provider(context)
    return StageCluster(
        kubectl=provider.tools.kubectl,
        namespace=context.shared_namespace,
        context=provider.env.get("KUBE_CONTEXT") or context.kube_context,
        kubeconfig=context.paths.kubeconfig_path,
        env=provider.env,
    )


def _value(from_env: str | None, from_file: str | None, name: str) -> str:
    if from_env and from_file:
        raise SourceBindingError("give the URL with --from-env or --from-file, not both")
    if from_env:
        if from_env not in os.environ:
            raise SourceBindingError(f"--from-env: environment variable {from_env} is not set")
        return os.environ[from_env]
    if from_file:
        try:
            # A trailing newline is an editor's, not part of a URL.
            return Path(from_file).read_text().removesuffix("\n")
        except OSError as exc:
            raise SourceBindingError(f"--from-file: cannot read {from_file}: {exc.strerror}") from exc
    if _stdin_is_tty():
        return typer.prompt(f"receiver {name} webhook URL", hide_input=True)
    raise SourceBindingError(
        "no webhook URL: pass --from-env VAR or --from-file PATH, or run in a terminal to be asked"
    )


@app.command("bindings")
def bindings(profile_file: str = _PROFILE) -> None:
    """Report, per receiver, whether its Secret holds its key. Reads key names only."""
    from olf.infra.deployment.alert_bind import receiver_statuses

    context = _context(profile_file)
    receivers = context.topology.observability.alert_receivers
    if not receivers:
        typer.echo("No alert receivers are declared.")
        return
    cluster = _cluster(context)
    statuses = receiver_statuses(
        receivers,
        cluster.kubectl,
        namespace=cluster.namespace,
        context=cluster.context,
        kubeconfig=cluster.kubeconfig,
        env=cluster.env,
    )
    for receiver in receivers:
        if receiver.secret_ref is None:
            typer.echo(f"receiver {receiver.name}: uses the GitHub App; needs no binding")
    for status in statuses:
        where = f"secret {status.secret} key {status.key} in {cluster.namespace}"
        if status.present is None:
            typer.echo(f"receiver {status.receiver}: {where}: Secret missing")
        elif not status.bound:
            have = ", ".join(sorted(status.present)) or "(none)"
            typer.echo(f"receiver {status.receiver}: {where}: key missing (the Secret has: {have})")
        else:
            typer.echo(f"receiver {status.receiver}: {where}: bound")
    if not all(status.bound for status in statuses):
        raise typer.Exit(code=1)


@app.command("bind")
def bind(
    receiver_name: str = typer.Argument(..., metavar="RECEIVER", help="Receiver name, as in the profile."),
    profile_file: str = _PROFILE,
    from_env: str | None = typer.Option(
        None, "--from-env", help="Read the webhook URL from this environment variable."
    ),
    from_file: str | None = typer.Option(
        None, "--from-file", help="Read the webhook URL from a file, minus one trailing newline."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would change and write nothing."),
) -> None:
    """Write a receiver's webhook URL into the Secret its `secret_ref` names, in the shared namespace.

    The URL never comes from the command line; with neither option, a terminal
    prompts for it without echo. Other keys of a shared Secret are kept. It must
    use https. Output names the receiver, Secret and key, never the URL.
    """
    from olf.infra.deployment.alert_bind import bind_receiver, no_binding_message, validate_url

    try:
        context = _context(profile_file)
        receivers = context.topology.observability.alert_receivers
        receiver = next((candidate for candidate in receivers if candidate.name == receiver_name), None)
        if receiver is None:
            known = ", ".join(sorted(candidate.name for candidate in receivers)) or "(none)"
            raise SourceBindingError(f"unknown alert receiver {receiver_name!r}; the profile declares: {known}.")
        if receiver.secret_ref is None:
            raise SourceBindingError(no_binding_message(receiver))
        value = _value(from_env, from_file, receiver.name)
        validate_url(value)
        cluster = _cluster(context)
        line = bind_receiver(receiver, value, cluster, dry_run=dry_run)
    except SourceBindingError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(line)


_TEST_TIMEOUT_SECONDS = 180
_POLL_SECONDS = 3


def _heard_stages(receiver, context) -> list[str]:  # noqa: ANN001
    """The stages a receiver hears about: its `stages`, or every enabled stage when it names none."""
    return [str(stage) for stage in (receiver.stages or context.enabled_stages)]


def _outcome(logs: str) -> dict[str, object] | None:
    """The evaluator's one JSON result line, if it got as far as printing it."""
    for line in reversed(logs.splitlines()):
        if line.startswith("{"):
            try:
                found = json.loads(line)
            except ValueError:
                continue
            if isinstance(found, dict) and "status" in found:
                return found
    return None


def _stuck_pods(cluster: StageCluster, job: str, scope: dict[str, Any]) -> list[str]:
    """What the Job's pods are doing and what Kubernetes said about them, read before the Job is deleted."""
    pods = cluster.kubectl.get(
        "pods", namespace=cluster.namespace, output="json", extra_args=["-l", f"job-name={job}"], check=False, **scope
    )
    try:
        items = json.loads(pods.stdout).get("items", [])
    except ValueError:
        items = []
    if not items:
        return ["no pod was created for the Job"]
    lines = []
    for pod in items:
        name = pod["metadata"]["name"]
        status = pod.get("status", {})
        waiting = [
            f"{c.get('name')}: {c['state']['waiting'].get('reason')}"
            for c in status.get("containerStatuses", [])
            if c.get("state", {}).get("waiting")
        ]
        lines.append(f"pod {name}: {status.get('phase', 'Unknown')}" + (f" ({'; '.join(waiting)})" if waiting else ""))
        events = cluster.kubectl.get(
            "events",
            namespace=cluster.namespace,
            extra_args=["--field-selector", f"involvedObject.name={name}"],
            check=False,
            **scope,
        )
        lines += [f"  {line}" for line in events.stdout.splitlines()[-6:] if line.strip()]
    return lines


@app.command("test")
def send_test(
    receiver_name: str = typer.Argument(..., metavar="RECEIVER", help="Receiver name, as in the profile."),
    profile_file: str = _PROFILE,
    stage: str | None = typer.Option(
        None, "--stage", help="Stage the notification names. Default: the first enabled stage the receiver hears."
    ),
) -> None:
    """Send one test notification through the deployed evaluator and report what the receiver answered.

    It runs the evaluator as a one-off Job made from its CronJob, so the image, credentials, network policy and CA
    bundle are the ones a real alert uses. The notification carries `event: test`; a GitHub Discussions receiver
    opens a discussion and closes it. No alert state is read or written. Exits non-zero unless it was delivered.
    """
    from olf.infra.deployment.alert_job import EVALUATOR, evaluator_job, job_outcome

    try:
        context = _context(profile_file)
        receivers = context.topology.observability.alert_receivers
        receiver = next((candidate for candidate in receivers if candidate.name == receiver_name), None)
        if receiver is None:
            known = ", ".join(sorted(candidate.name for candidate in receivers)) or "(none)"
            raise SourceBindingError(f"unknown alert receiver {receiver_name!r}; the profile declares: {known}.")
        heard = _heard_stages(receiver, context)
        if stage is None:
            if not heard:
                raise SourceBindingError(f"receiver {receiver_name!r} hears about no enabled stage")
            stage = heard[0]
        elif stage not in heard:
            raise SourceBindingError(
                f"receiver {receiver_name!r} does not hear about stage {stage!r}; it hears: {', '.join(heard)}."
            )
        cluster = _cluster(context)
        scope: dict[str, Any] = {"context": cluster.context, "kubeconfig": cluster.kubeconfig, "env": cluster.env}
        found = cluster.kubectl.get(
            "cronjob", name=EVALUATOR, namespace=cluster.namespace, output="json", check=False, **scope
        )
        if found.returncode != 0:
            if "NotFound" in found.stderr:
                raise SourceBindingError(
                    "no alert receivers are deployed; declare one and run `olf infra deploy`"
                )
            raise SourceBindingError(f"could not read the {EVALUATOR} CronJob: {found.stderr.strip()}")
        name = f"alert-test-{uuid.uuid4().hex[:8]}"
        job = evaluator_job(
            json.loads(found.stdout), name, ["--test-receiver", receiver_name, "--test-stage", stage]
        )
        # A failed send must not be retried: every retry would be another discussion.
        job["spec"]["backoffLimit"] = 0
        apply_manifest(cluster, json.dumps(job), "Job", name)
    except SourceBindingError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc

    typer.echo(f"Sending a test notification to receiver {receiver_name} (stage {stage}) ...")
    try:
        deadline = time.monotonic() + _TEST_TIMEOUT_SECONDS
        while (
            outcome := job_outcome(
                json.loads(
                    cluster.kubectl.get("job", name=name, namespace=cluster.namespace, output="json", **scope).stdout
                )
            )
        ) is None:
            if time.monotonic() >= deadline:
                for line in _stuck_pods(cluster, name, scope):
                    typer.echo(f"  {line}")
                raise typer.Exit(code=fail(f"the test Job {name} did not finish in {_TEST_TIMEOUT_SECONDS} seconds"))
            time.sleep(_POLL_SECONDS)
        logs = cluster.kubectl.logs(f"job/{name}", namespace=cluster.namespace, check=False, **scope).stdout
    finally:
        cluster.kubectl.delete("job", name, namespace=cluster.namespace, extra_args=["--wait=false"], **scope)

    result = _outcome(logs)
    lines = [line for line in logs.splitlines() if receiver_name in line and not line.startswith("{")]
    # The evaluator names the receiver on every send line; a refusal before the send (settings, unknown receiver)
    # does not, so without a result the whole log is the explanation.
    for line in lines if result else logs.splitlines()[-10:]:
        typer.echo(f"  {line}")
    if result is None:
        raise typer.Exit(code=fail(f"the test Job {outcome} without reporting a result for receiver {receiver_name}"))
    http = f"HTTP {result['http_status']}, " if result.get("http_status") else ""
    typer.echo(f"receiver {receiver_name}: {result['status']} ({http}{result.get('attempts')} attempt(s))")
    if result["status"] != "delivered":
        raise typer.Exit(code=3)
