"""Running the Trino CLI inside the coordinator pod, shared by `olf query` and `olf e2e`.

Both callers go through `kubectl exec` on the coordinator because that is the
only path to Trino that needs nothing on the workstation: no port-forward, no
trust store, no client. Everything that decides *how* the CLI is invoked -
which catalog, which user, which listener - lives here once, so a fix to one
caller cannot leave the other behind.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

# The coordinator mounts its serving certificate's CA only when workload
# authentication is on (ADR 0016), so the pod itself says which listener to use
# - no contract field has to be threaded to the workstation.
TRINO_CA = "/etc/trino/tls/ca.crt"

LEGACY_CATALOG = "iceberg"
STAGE_CATALOG_PREFIX = "lakehouse_"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class TrinoQueryError(RuntimeError):
    """A Trino query could not be run or was rejected; the message is user-facing."""


@dataclass(frozen=True)
class TrinoTarget:
    """Where and as whom to run Trino for one stage."""

    provider: str
    kube_context: str
    namespace: str
    catalog: str
    stage: str

    @property
    def user(self) -> str:
        # Trino's file-based access control (modules/query/trino/main.tf) grants
        # catalog access to "olf-<stage>-runtime", not to the Kubernetes
        # namespace: the cloud POC roots serve every stage from one shared
        # namespace, so deriving the user from it would be denied by the
        # default-deny catalog rule there.
        return f"olf-{self.stage}-runtime"


def stage_catalog_name(environ: Mapping[str, str] | None = None) -> str:
    """The Iceberg catalog the applied contract serves the stage under.

    Reads the already-resolved `OPENLAKEFORGE_QUERY_TRINO_CATALOG`, which
    `applied_contract_environment` exports before any caller runs. The local
    root provisions one Polaris catalog per stage, named `lakehouse_<stage>`
    (#114); the AWS/Azure roots are not yet stage-aware and still resolve the
    single legacy `iceberg` catalog, which is the default here.
    """
    source = os.environ if environ is None else environ
    return source.get("OPENLAKEFORGE_QUERY_TRINO_CATALOG") or LEGACY_CATALOG


def stage_from_catalog(catalog: str) -> str:
    return catalog.removeprefix(STAGE_CATALOG_PREFIX)


def glue_namespace_prefix(provider: str, catalog: str) -> str:
    """Physical-namespace prefix for AWS's shared default Glue catalog (#114).

    This account cannot create a custom Glue catalog per stage, so every
    stage's databases live in the one default catalog and are kept
    collision-free with a `<stage catalog name>_` prefix (contracts.py,
    catalog.py). Local's Polaris deployment gives each stage its own
    catalog, so descriptor namespace names stay bare there.
    """
    return f"{catalog}_" if provider == "aws" else ""


def cli_script(ca_path: str | None = None) -> str:
    ca = ca_path or TRINO_CA
    return (
        f"if [ -f {ca} ]; then "
        f'exec trino --server https://localhost:8443 --truststore-path {ca} "$@"; '
        'else exec trino "$@"; fi'
    )


def exec_args(target: TrinoTarget, sql: str, *, output_format: str) -> list[str]:
    """Arguments for `kubectl` (after `--context`) that run `sql` as the stage's runtime user.

    The stage catalog is the session default, so SQL can name `<schema>.<table>`.
    """
    return [
        "exec",
        "-n",
        target.namespace,
        "deploy/trino-coordinator",
        "--",
        "sh",
        "-c",
        cli_script(),
        "trino",
        "--catalog",
        target.catalog,
        "--output-format",
        output_format,
        "--user",
        target.user,
        "--execute",
        sql,
    ]


def explain_failure(target: TrinoTarget, detail: str) -> str:
    """Turn kubectl's or Trino's stderr into a message that says what to do."""
    text = detail.strip()
    lowered = text.lower()
    if "access denied" in lowered:
        return (
            f"Trino denied the query for {target.user}: {text}\n"
            "olf query runs as the stage's runtime workload user, which can only reach its own stage's catalog."
        )
    if ("catalog" in lowered and ("does not exist" in lowered or "not found" in lowered)) or (
        "notfound" in lowered and ("namespace" in lowered or "trino-coordinator" in lowered)
    ):
        return (
            f"stage {target.stage!r} does not look deployed ({target.catalog} is not served by Trino): "
            f"deploy it first with `olf deploy`.\n{text}"
        )
    if "unable to connect to the server" in lowered or "connection refused" in lowered:
        return f"cannot reach the cluster behind kube context {target.kube_context!r}: {text}"
    return text or "the query failed without any output."


def run_sql(
    target: TrinoTarget,
    sql: str,
    *,
    output_format: str,
    kubectl: str,
    runner: Runner | None = None,
) -> str:
    """Run `sql` once and return Trino's output; raise `TrinoQueryError` with an explanation on failure."""
    command = [kubectl, "--context", target.kube_context, *exec_args(target, sql, output_format=output_format)]
    run = runner or _subprocess_runner
    try:
        result = run(command)
    except OSError as exc:
        raise TrinoQueryError(f"could not run kubectl: {exc}") from exc
    if result.returncode != 0:
        raise TrinoQueryError(explain_failure(target, result.stderr or result.stdout or ""))
    return result.stdout


def _subprocess_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, check=False)
