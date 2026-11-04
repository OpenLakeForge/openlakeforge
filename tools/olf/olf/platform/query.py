"""`olf query`: run SQL as yourself against a stage's data (ADR 0019, ADR 0020).

The query goes to Trino's `query_api` route with the signed-in person's Dex
token, twice: as the bearer Trino authenticates, and as the `token` extra
credential its stage's people catalog hands to Polaris. Polaris's grants decide
what the person reads and writes, so every refusal is explained from the error
Trino passes on, whose own message names neither the role nor the reason.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import typer
from openlakeforge_services.trino import (
    TrinoAuthError,
    TrinoError,
    TrinoForbiddenError,
    TrinoNotFoundError,
    TrinoQueryError,
    TrinoResult,
    TrinoStatementClient,
    TrinoTlsError,
    failure_messages,
)

from olf.common.log import fail
from olf.platform import session as sessions
from olf.platform.output import table

FORMATS = ("table", "csv", "json")
STAGES = ("dev", "uat", "prod")
SOURCE = "olf-query"


def statement_url(instance: sessions.Instance) -> str:
    """Trino's /v1 on the query host, with the issuer's scheme (plain HTTP when the routes are)."""
    scheme = urlsplit(instance.issuer).scheme or "https"
    return f"{scheme}://trino.{instance.base_domain}/v1/statement"


def person_token(instance: sessions.Instance) -> tuple[str, dict[str, Any]]:
    """The signed-in person's token and its claims, refreshed when due."""
    token = sessions.active_session(instance).id_token
    claims = sessions.claims(token)
    if not claims.get("email"):
        raise sessions.PlatformError("the stored session carries no e-mail, which Trino names you by; run `olf login`")
    # Polaris names the person's principal from the connector id (ADR 0019, section 4, step 11).
    if not isinstance(claims.get("federated_claims"), dict):
        raise sessions.PlatformError(
            "the stored session predates `olf query` and lacks the claims Polaris reads; run `olf login` again"
        )
    return token, claims


def run_statement(url: str, sql: str, *, bearer: Callable[[], str], user: str, catalog: str) -> TrinoResult:
    """The person's token is also the `token` extra credential their stage's people catalog hands to Polaris."""
    client = TrinoStatementClient(url, catalog=catalog, user=user, source=SOURCE, send=sessions.send)
    try:
        return client.run(sql, bearer=bearer, extra_credentials=lambda token: {"token": token})
    except TrinoQueryError as exc:
        raise sessions.PlatformError(explain(exc.error, catalog)) from exc
    except TrinoTlsError as exc:
        raise sessions.PlatformError(
            f"cannot verify the TLS certificate of {urlsplit(url).hostname}. A local deployment uses its own CA: "
            "run `olf infra access trust`, then set REQUESTS_CA_BUNDLE to the certificate file it writes"
        ) from exc
    except TrinoAuthError as exc:
        raise sessions.PlatformError("Trino or the perimeter refused your session; run `olf login`") from exc
    except TrinoForbiddenError as exc:
        raise sessions.PlatformError(
            "the perimeter refused you: your GitHub teams map to no OpenLakeForge role. A platform-admin assigns roles"
        ) from exc
    except TrinoNotFoundError as exc:
        raise sessions.PlatformError(
            "this deployment has no query route for people; it needs spec.identity.issuer github and "
            "Trino workload authentication at password. Operators can use `olf infra query`"
        ) from exc
    except TrinoError as exc:
        raise sessions.PlatformError(str(exc)) from exc


def explain(error: dict[str, Any], stage: str) -> str:
    """Name what refused the query and why, from the error Trino passes on (ADR 0019, section 6)."""
    chain = list(dict.fromkeys(failure_messages(error)))
    text = "\n".join(chain)
    reason = next(
        (line for line in chain if "Forbidden:" in line or "Access Denied" in line), chain[-1] if chain else ""
    )
    if "Forbidden:" in text and "not authorized" in text:
        return (
            f"Polaris refused this on stage {stage}: your role does not reach it. Which role reads or writes each "
            f"stage and layer is `data_access` in release/identity-roles.yaml.\n{reason}"
        )
    if "Access Denied" in text and ("Put failed" in text or "Error committing write" in text):
        return f"your role may only read on stage {stage}; writing needs a role with write access there.\n{reason}"
    if "Cannot access catalog" in text:
        return (
            f"only the people catalogs {', '.join(STAGES)} are open to you; name tables as <schema>.<table>.\n{reason}"
        )
    if "cannot become user" in text:
        return f"Trino refused the user name: a person may act only as their own e-mail.\n{reason}"
    if "invalid_request" in text or "Malformed request" in text:
        return (
            "the query reached Polaris without your token, which should not happen through `olf query`; "
            f"report it with this message.\n{text}"
        )
    return text or "the query failed without an error message."


def json_keys(columns: Sequence[str]) -> list[str]:
    """Column names as object keys: a repeated name (`SELECT 1 AS id, 2 AS id`, a join) gets `_2`, `_3`, so no
    value is dropped."""
    keys: list[str] = []
    for name in columns:
        key, n = name, 1
        while key in keys:
            n += 1
            key = f"{name}_{n}"
        keys.append(key)
    return keys


def render(result: TrinoResult, output_format: str) -> str:
    if output_format == "json":
        keys = json_keys(result.columns)
        return "".join(json.dumps(dict(zip(keys, row, strict=True)), default=str) + "\n" for row in result.rows)
    if output_format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(result.columns)
        writer.writerows(result.rows)
        return buffer.getvalue()
    return table(result.columns, [["NULL" if value is None else str(value) for value in row] for row in result.rows])


def query(
    sql: str = typer.Argument("", help="SQL to run. Use --file to read it from a file instead."),
    file: str = typer.Option("", "--file", help="Read the SQL from this file."),
    stage: str = typer.Option("dev", "--stage", help="Stage to query: dev, uat, or prod."),
    output_format: str = typer.Option("table", "--format", help="Output format: table, csv, or json."),
    profile_file: str = typer.Option(
        "openlakeforge.yaml", "--profile-file", "-f", help="Deployment Profile of the deployment to query."
    ),
) -> None:
    """Run SQL as yourself against a stage's data.

    You need `olf login` first. The stage's catalog is the session default, so
    SQL names `<schema>.<table>`. What you may read or write on each stage and
    layer is your role's `data_access` row, enforced by Polaris. Operators who
    need the stage's workload user use `olf infra query`.
    """
    if output_format not in FORMATS:
        raise typer.Exit(code=fail(f"unknown --format {output_format!r}; expected table, csv, or json."))
    if stage not in STAGES:
        raise typer.Exit(code=fail(f"unknown --stage {stage!r}; expected dev, uat, or prod."))
    if bool(sql) == bool(file):
        raise typer.Exit(code=fail("give the SQL either as an argument or with --file."))
    if file:
        try:
            sql = Path(file).read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.Exit(code=fail(f"cannot read {file}: {exc}")) from exc
    if not sql.strip():
        raise typer.Exit(code=fail("the SQL is empty."))
    try:
        instance = sessions.resolve_instance(profile_file)
        if not instance.dex:
            raise sessions.PlatformError(
                "per-person queries need sign-in with GitHub (spec.identity.issuer: github); "
                "operators can use `olf infra query`, which runs as the stage's workload user"
            )
        _, claims = person_token(instance)
        result = run_statement(
            statement_url(instance),
            sql,
            bearer=lambda: sessions.active_session(instance).id_token,
            user=str(claims["email"]),
            catalog=stage,
        )
    except (sessions.PlatformError, OSError, ValueError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(render(result, output_format), nl=False)
