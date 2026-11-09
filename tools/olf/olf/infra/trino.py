"""Running the Trino CLI inside the coordinator pod, shared by `olf infra query` and `olf e2e`.

Both callers go through `kubectl exec` on the coordinator because that is the
only path to Trino that needs nothing on the workstation: no port-forward, no
trust store, no client. Everything that decides *how* the CLI is invoked -
which catalog, which user, which listener - lives here once, so a fix to one
caller cannot leave the other behind.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from olf.common.deployment.errors import ExecutableNotFoundError
from olf.common.tooling.process import CommandResult, ProcessRunner, unsets_of

# The coordinator mounts its serving certificate's CA only when workload
# authentication is on (ADR 0016), so the pod itself says which listener to use
# - no contract field has to be threaded to the workstation.
TRINO_CA = "/etc/trino/tls/ca.crt"

LEGACY_CATALOG = "iceberg"
STAGE_CATALOG_PREFIX = "lakehouse_"

# The coordinator mounts its password file here when password authentication is
# on (the chart's auth.passwordAuthSecret), which is how the pod says so.
TRINO_PASSWORD_FILE = "/etc/trino/auth/password/password.db"

# Where the chart writes the coordinator's catalog properties; the Polaris token endpoint and scope are read from them.
TRINO_CATALOG_DIR = "/etc/trino/catalog"

# Mints the stage processing principal's Polaris token inside the coordinator, with the curl and sed its image has
# (no python, jq or wget), from the credential its pod already holds. The credential goes to curl as a config on stdin
# and the token lands only in the pod's shell and the CLI's argv there, never in a `kubectl` argument, a stdin of
# ours or a log. A catalog with no token endpoint has no token to send; a mint that fails fails the query.
_MINT_TOKEN = (
    r"""U=$(sed -n 's/^iceberg.rest-catalog.oauth2.server-uri=//p' {props}); if [ -n "$U" ]; then """
    r"""S=$(sed -n 's/^iceberg.rest-catalog.oauth2.scope=//p' {props}); """
    r"""T=$(printf 'url = "%s"\ndata-urlencode = "grant_type=client_credentials"\ndata-urlencode = "client_id=%s"\n"""
    r"""data-urlencode = "client_secret=%s"\ndata-urlencode = "scope=%s"\n' "$U" """
    r""""$OPENLAKEFORGE_CATALOG_{stage}_CLIENT_ID" "$OPENLAKEFORGE_CATALOG_{stage}_CLIENT_SECRET" "$S" """
    r"""| curl -sf -m 30 -K - 2>/dev/null """
    r"""| sed -n 's/.*"access_token" *: *"\([^"]*\)".*/\1/p'); """
    r"""if [ -z "$T" ]; then echo "olf: cannot mint the Polaris token for catalog {catalog} from $U: """
    r"""the endpoint is unreachable or refused the stage's credential" >&2; exit 1; fi; """
    r"""set -- --extra-credential "token=$T" "$@"; unset T; fi; """
)

# Called with the argv and the text for the process's stdin.
Runner = Callable[[Sequence[str], str], CommandResult]


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
    service: str = "trino"  # the coordinator Deployment is `<service>-coordinator`

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


def service_namespace(environ: Mapping[str, str] | None, fallback: str) -> str:
    """Namespace of the stage's Trino Service, read from its contract endpoint host.

    The host is `<service>.<namespace>[.svc...]`. `fallback` covers a contract
    that was never applied (no `OPENLAKEFORGE_QUERY_TRINO_HOST`) or a bare
    in-namespace name; callers pass the namespace the old shared topology used.
    """
    host = (os.environ if environ is None else environ).get("OPENLAKEFORGE_QUERY_TRINO_HOST", "")
    parts = host.split(".")
    return parts[1] if len(parts) > 1 and parts[1] else fallback


def service_name(environ: Mapping[str, str] | None, fallback: str) -> str:
    """Name of the stage's Trino Service, the `<service>` part of the same endpoint host."""
    host = (os.environ if environ is None else environ).get("OPENLAKEFORGE_QUERY_TRINO_HOST", "")
    return host.split(".")[0] or fallback


def contract_service(
    contracts: Mapping[str, Any] | None, stage: str, *, service: str = "trino", namespace: str
) -> tuple[str, str]:
    """(Service, namespace) of `stage`'s Trino, from the raw contracts."""
    endpoint = ((((contracts or {}).get("stages") or {}).get(stage) or {}).get("query") or {}).get("endpoint", "")
    host = {"OPENLAKEFORGE_QUERY_TRINO_HOST": urlsplit(endpoint).hostname or ""}
    return service_name(host, service), service_namespace(host, namespace)


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


def cli_script(ca_path: str | None = None, *, stage: str | None = None, catalog: str | None = None) -> str:
    """The shell the coordinator pod runs, choosing the listener the pod itself offers.

    With password authentication on, the password arrives on stdin and goes to
    the CLI as TRINO_PASSWORD, so it is on no command line. An empty line sends
    no password at all, which is what the header-only isolation probe needs.

    With a `stage` and `catalog`, the CLI also carries the stage's Polaris token as the `token` extra credential
    (#967), which a keyless catalog reads and a keyed one ignores.
    """
    ca = ca_path or TRINO_CA
    props = f"{TRINO_CATALOG_DIR}/{catalog}.properties"
    mint = ""
    if stage and catalog:
        code = _MINT_TOKEN.format(props=props, stage=stage.upper(), catalog=catalog)
        mint = f"if [ -f {props} ]; then {code}fi; "
    return (
        f"{mint}"
        f"if [ -f {TRINO_PASSWORD_FILE} ]; then "
        "IFS= read -r TRINO_PASSWORD; export TRINO_PASSWORD; "
        'if [ -n "$TRINO_PASSWORD" ]; then set -- --password "$@"; fi; fi; '
        f"if [ -f {ca} ]; then "
        f'exec trino --server https://localhost:8443 --truststore-path {ca} "$@"; '
        'else exec trino "$@"; fi'
    )


def password_secret(target: TrinoTarget) -> str:
    """The Secret in the coordinator's namespace holding the stage principal's password (ADR 0016)."""
    return f"trino-credential-{target.user}"


def password_args(target: TrinoTarget) -> list[str]:
    """`kubectl` arguments that print the principal's base64 password; empty while no credential exists."""
    return [
        "get",
        "secret",
        password_secret(target),
        "-n",
        target.namespace,
        "--ignore-not-found",
        "-o",
        "jsonpath={.data.password}",
    ]


def decode_password(raw: str) -> str:
    return base64.b64decode(raw).decode("utf-8") if raw.strip() else ""


def exec_args(target: TrinoTarget, sql: str, *, output_format: str, session_user: str | None = None) -> list[str]:
    """Arguments for `kubectl` (after `--context`) that run `sql` as the stage's runtime user.

    The stage catalog is the session default, so SQL can name `<schema>.<table>`.
    `session_user` is a claimed identity sent apart from the authenticated
    principal, for the isolation probes. `-i` carries the password on stdin.
    """
    return [
        "exec",
        "-i",
        "-n",
        target.namespace,
        f"deploy/{target.service}-coordinator",
        "--",
        "sh",
        "-c",
        cli_script(stage=target.stage, catalog=target.catalog),
        "trino",
        "--catalog",
        target.catalog,
        "--output-format",
        output_format,
        "--user",
        target.user,
        *(["--session-user", session_user] if session_user else []),
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
            "olf infra query runs as the stage's runtime workload user, which can only reach its own stage's catalog."
        )
    if ("catalog" in lowered and ("does not exist" in lowered or "not found" in lowered)) or (
        "notfound" in lowered and ("namespace" in lowered or "trino-coordinator" in lowered)
    ):
        return (
            f"stage {target.stage!r} does not look deployed ({target.catalog} is not served by Trino): "
            f"deploy it first with `olf infra deploy`.\n{text}"
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
    environ: Mapping[str, str] | None = None,
) -> str:
    """Run `sql` once and return Trino's output; raise `TrinoQueryError` with an explanation on failure."""
    base = [kubectl, "--context", target.kube_context]
    run = runner or (lambda command, stdin: _subprocess_runner(command, stdin, environ=environ))
    try:
        # Only a contract that names a credential has a password to send; an unauthenticated
        # coordinator is queried with no Secret read and no stdin.
        password = ""
        stdin = ""
        if (os.environ if environ is None else environ).get("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET"):
            lookup = run([*base, *password_args(target)], "")
            if lookup.returncode != 0:
                raise TrinoQueryError(
                    f"cannot read the password Secret {password_secret(target)} in namespace {target.namespace}: "
                    f"{(lookup.stderr or lookup.stdout or '').strip()}\n"
                    "olf infra query needs RBAC permission to get that Secret."
                )
            password = decode_password(lookup.stdout or "")
            stdin = f"{password}\n"
        result = run([*base, *exec_args(target, sql, output_format=output_format)], stdin)
    except (OSError, ExecutableNotFoundError) as exc:
        raise TrinoQueryError(f"could not run kubectl: {exc}") from exc
    if result.returncode != 0:
        raise TrinoQueryError(explain_failure(target, result.stderr or result.stdout or ""))
    return result.stdout


def _subprocess_runner(
    command: Sequence[str], stdin: str, *, environ: Mapping[str, str] | None = None
) -> CommandResult:
    return ProcessRunner().run(command, input_text=stdin, check=False, env=environ, unset=unsets_of(environ))
