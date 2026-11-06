"""Running SQL against a stage's Trino, shared by `olf infra query` and `olf e2e`.

Both callers reach the coordinator through a `kubectl port-forward` and speak Trino's REST protocol, because the
Trino CLI and the Python client refuse a password over plain HTTP and the coordinator serves nothing else
(ADR 0016). Everything that decides *how* a query is sent - which catalog, which principal, which claimed user -
lives here once, so a fix to one caller cannot leave the other behind.
"""

from __future__ import annotations

import base64
import contextlib
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import requests
from openlakeforge_services import trino as statements

from olf.common.deployment.errors import ExecutableNotFoundError
from olf.common.tooling.process import CommandResult, ProcessRunner, unsets_of
from olf.infra import k8s
from olf.platform.query import render as render_result

HTTP_PORT = 8080
LEGACY_CATALOG = "iceberg"
STAGE_CATALOG_PREFIX = "lakehouse_"

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


def explain_failure(target: TrinoTarget, detail: str) -> str:
    """Turn kubectl's or Trino's error text into a message that says what to do."""
    text = detail.strip()
    lowered = text.lower()
    if "access denied" in lowered:
        return (
            f"Trino denied the query for {target.user}: {text}\n"
            "olf infra query runs as the stage's runtime workload user, which can only reach its own stage's catalog."
        )
    if ("catalog" in lowered and ("does not exist" in lowered or "not found" in lowered)) or (
        "notfound" in lowered and ("namespace" in lowered or "trino" in lowered)
    ):
        return (
            f"stage {target.stage!r} does not look deployed ({target.catalog} is not served by Trino): "
            f"deploy it first with `olf infra deploy`.\n{text}"
        )
    if "unable to connect to the server" in lowered or "connection refused" in lowered:
        return f"cannot reach the cluster behind kube context {target.kube_context!r}: {text}"
    return text or "the query failed without any output."


@contextlib.contextmanager
def _port_forward(target: TrinoTarget, environ: Mapping[str, str] | None) -> Iterator[int]:
    with k8s.port_forward(
        "trino",
        HTTP_PORT,
        target.namespace,
        log_path=f"/tmp/openlakeforge-trino-{target.stage}-port-forward.log",
        kube_context=target.kube_context,
        environ=environ,
    ) as port:
        yield port


def render(result: statements.TrinoResult, output_format: str) -> str:
    """`table`, `csv` and `json` as `olf query` prints them; `bare` is the rows alone, comma-joined, for scripts."""
    if output_format != "bare":
        return render_result(result, output_format)
    return "".join(",".join(_bare(value) for value in row) + "\n" for row in result.rows)


def _bare(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def run_sql(
    target: TrinoTarget,
    sql: str,
    *,
    output_format: str,
    kubectl: str,
    runner: Runner | None = None,
    environ: Mapping[str, str] | None = None,
    session_user: str | None = None,
    password: str | None = None,
    forward: Callable[[TrinoTarget, Mapping[str, str] | None], contextlib.AbstractContextManager[int]] | None = None,
    send: Callable[..., requests.Response] | None = None,
) -> str:
    """Run each statement of `sql` and return the rendered results; raise `TrinoQueryError` with an explanation.

    Trino's REST endpoint takes one statement and rejects a trailing `;`, which the CLI's `--execute` did not, so the
    text is split here (`split_statements`) and the results of the statements that return columns follow each other.

    The runtime principal authenticates with the password in its Secret when the contract names one. The overrides
    are for the isolation probes: `password` of `""` sends none, another value replaces the Secret's, and
    `session_user` is a claimed identity sent apart from the authenticated principal.
    """
    source = os.environ if environ is None else environ
    base = [kubectl, "--context", target.kube_context]
    run = runner or (lambda command, stdin: _subprocess_runner(command, stdin, environ=environ))
    try:
        if password is None and source.get("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET"):
            lookup = run([*base, *password_args(target)], "")
            if lookup.returncode != 0:
                raise TrinoQueryError(
                    f"cannot read the password Secret {password_secret(target)} in namespace {target.namespace}: "
                    f"{(lookup.stderr or lookup.stdout or '').strip()}\n"
                    "olf infra query needs RBAC permission to get that Secret."
                )
            password = decode_password(lookup.stdout or "")
        with (forward or _port_forward)(target, environ) as port:
            client = statements.TrinoStatementClient(
                f"http://127.0.0.1:{port}/v1/statement",
                catalog=target.catalog,
                user=session_user or target.user,
                source="olf-infra-query",
                send=send or _loopback_session().request,
                basic=(target.user, password) if password else None,
            )
            results = [client.run(statement, bearer=lambda: "") for statement in split_statements(sql)]
    except TrinoQueryError:
        raise
    except statements.TrinoQueryError as exc:
        raise TrinoQueryError(explain_failure(target, str(exc))) from exc
    except statements.TrinoError as exc:
        raise TrinoQueryError(explain_failure(target, str(exc))) from exc
    except (OSError, ExecutableNotFoundError, k8s.KubectlError) as exc:
        raise TrinoQueryError(explain_failure(target, f"could not reach Trino through kubectl: {exc}")) from exc
    return "".join(render(result, output_format) for result in results if result.columns)


def split_statements(sql: str) -> list[str]:
    """The statements of `sql`, split on `;` outside quotes and comments, without empty ones.

    A semicolon inside a string, a quoted identifier, a `--` comment or a `/* */` comment belongs to it. The quote
    handling needs no escape case: `''` and `""` close and reopen the quote in one step.
    """
    statements_: list[str] = []
    current: list[str] = []
    quote = ""
    i = 0
    while i < len(sql):
        char = sql[i]
        pair = sql[i : i + 2]
        if quote:
            end = {"'": "'", '"': '"', "--": "\n", "/*": "*/"}[quote]
            if sql.startswith(end, i):
                current.append(end)
                i += len(end)
                quote = ""
                continue
        elif char in "'\"":
            quote = char
        elif pair in ("--", "/*"):
            quote = pair
            current.append(pair)
            i += 2
            continue
        elif char == ";":
            statements_.append("".join(current))
            current = []
            i += 1
            continue
        current.append(char)
        i += 1
    statements_.append("".join(current))
    return [statement.strip() for statement in statements_ if _has_code(statement)]


def _has_code(statement: str) -> bool:
    """False for text that is only whitespace and comments: Trino rejects an empty statement."""
    rest = statement
    while True:
        rest = rest.lstrip()
        if rest.startswith("--"):
            rest = rest.partition("\n")[2]
        elif rest.startswith("/*"):
            rest = rest.partition("*/")[2]
        else:
            return bool(rest)


def _loopback_session() -> requests.Session:
    """A session that ignores `HTTP_PROXY` and friends: the request goes to the port-forward on loopback, and the
    stage's password must not be handed to a workstation proxy on the way."""
    session = requests.Session()
    session.trust_env = False
    return session


def _subprocess_runner(
    command: Sequence[str], stdin: str, *, environ: Mapping[str, str] | None = None
) -> CommandResult:
    return ProcessRunner().run(command, input_text=stdin, check=False, env=environ, unset=unsets_of(environ))
