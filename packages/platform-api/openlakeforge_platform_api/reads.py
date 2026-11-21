"""Read endpoints: Dagster jobs and runs and archived logs per stage, OpenMetadata domains and data products.

The services take different identities (ADR 0021, decision 2). Dagster checks
nothing, so the API reaches it with no credential and the permission matrix is
the only check. The log archive is read with the API's own read-only key for
the log prefix, so there too the matrix decides. OpenMetadata authorizes by the
person's own roles, so the API forwards the caller's token unchanged and holds
no OpenMetadata credential.
"""

from __future__ import annotations

import logging
import unicodedata
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Query, Request
from openlakeforge_services.base import ServiceClientError, TransientServiceError
from openlakeforge_services.dagster import DagsterClient, DagsterJob
from openlakeforge_services.logs import ArchiveDeadline, ArchivedLogLine, LogArchive
from openlakeforge_services.openmetadata import OpenMetadataClient

from openlakeforge_platform_api import log_reads
from openlakeforge_platform_api.log_reads import ReadSlot, bounded_read
from openlakeforge_platform_api.models import (
    DataProduct,
    DataProducts,
    Domain,
    Domains,
    Job,
    Jobs,
    LogLine,
    Logs,
    Run,
    Runs,
)
from openlakeforge_platform_api.pipeline_health import job_scope
from openlakeforge_platform_api.tokens import Principal

log = logging.getLogger(__name__)

MAX_RUNS = 100
MAX_LOG_LINES = 5000
MAX_LOG_SEARCH = 200
# The search text of both log routes. It may hold whatever a person pasted from a line, so it is never logged.
LOG_SEARCH = Query(
    default=None,
    min_length=1,
    max_length=MAX_LOG_SEARCH,
    description=(
        "Only lines whose message contains this text, ignoring case: a plain substring, not a pattern. "
        f"1 to {MAX_LOG_SEARCH} characters, no control characters."
    ),
)


def search_text(q: str | None) -> str:
    """`q` as the archive reader takes it; 422 for a control character, which no single log line holds."""
    if q is None:
        return ""
    if any(unicodedata.category(char) == "Cc" for char in q):
        raise HTTPException(status_code=422, detail="`q` must not contain control characters")
    return q


def _dagster(request: Request, stage: str) -> DagsterClient:
    url = request.app.state.settings.dagster_graphql_urls.get(stage)
    if not url:
        raise HTTPException(status_code=404, detail=f"no Dagster is configured for stage {stage!r}")
    return DagsterClient(url)


@contextmanager
def _dagster_errors(stage: str) -> Iterator[None]:
    # The service's own message goes to the log, not the caller: a Dagster
    # PythonError can carry a stack trace.
    try:
        yield
    except TransientServiceError as exc:
        log.warning("Dagster for stage %s is unreachable: %s", stage, exc)
        raise HTTPException(status_code=502, detail=f"Dagster for stage {stage!r} is unreachable") from exc
    except ServiceClientError as exc:
        log.warning("Dagster for stage %s failed: %s", stage, exc)
        raise HTTPException(status_code=502, detail=f"Dagster for stage {stage!r} answered with an error") from exc


def list_jobs(request: Request, stage: str) -> Jobs:
    with _dagster_errors(stage):
        jobs = _dagster(request, stage).list_jobs()
    return Jobs(jobs=[_job(job) for job in jobs])


def _job(job: DagsterJob) -> Job:
    # The tags themselves are not answered: a job's tags include its sources' stage bindings.
    scope = job_scope(job.tags)
    return Job(
        location=job.location,
        repository=job.repository,
        name=job.name,
        description=job.description,
        domain=scope.domain,
        product=scope.product,
    )


def list_runs(
    request: Request,
    stage: str,
    job: str = "",
    limit: int = Query(default=20, ge=1, le=MAX_RUNS),  # noqa: B008 - FastAPI's parameter API
) -> Runs:
    with _dagster_errors(stage):
        runs = _dagster(request, stage).list_runs(job_name=job, limit=limit)
    return Runs(runs=[Run(**asdict(run)) for run in runs])


def get_run(request: Request, stage: str, run_id: str) -> Run:
    with _dagster_errors(stage):
        run = _dagster(request, stage).get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} in stage {stage!r}")
    return Run(**asdict(run))


async def list_logs(
    request: Request,
    stage: str,
    day: date = Query(alias="date"),  # noqa: B008 - FastAPI's parameter API
    hour: int | None = Query(default=None, ge=0, le=23),  # noqa: B008
    pod: str = "",
    container: str = "",
    component: str = "",
    product: str = "",
    run_id: str = "",
    severity: Literal["debug", "info", "warning", "error", "critical"] | None = None,
    q: str | None = LOG_SEARCH,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=500, ge=1, le=MAX_LOG_LINES),  # noqa: B008
) -> Logs:
    """The stage's archived pod log lines for a UTC day or one hour of it, oldest first.

    The stage picks the archive, so no filter can reach another stage's. `severity` is a floor on each line's
    `level`; `q` keeps the lines whose message contains it, ignoring case; `component`, `product` and `run_id` match
    v2 records only. Every filter applies before `limit`, so `truncated` means more matching lines exist.
    """
    text = search_text(q)
    reader, namespace = _log_archive(request, stage)

    def read(slot: ReadSlot) -> list[ArchivedLogLine]:
        with _archive_errors(stage):
            try:
                return reader.read(
                    namespace,
                    day,
                    hour=hour,
                    pod=pod,
                    container=container,
                    component=component,
                    product=product,
                    run_id=run_id,
                    severity=severity or "",
                    text=text,
                    since=since,
                    until=until,
                    limit=limit,
                    deadline=slot.expires,
                    cancelled=slot.cancelled.is_set,
                )
            except ArchiveDeadline as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"stage {stage!r}'s log archive did not answer in {log_reads.READ_DEADLINE_SECONDS:g} s",
                ) from exc

    lines = await bounded_read(request, read, waiting_on=f"stage {stage!r}'s log archive")
    # `truncated` tells a client that a narrower window or a higher limit would show more.
    return Logs(lines=[log_line(line) for line in lines], truncated=len(lines) >= limit)


def log_line(line: ArchivedLogLine) -> LogLine:
    return LogLine(**asdict(line))


def _log_archive(request: Request, stage: str) -> tuple[LogArchive, str]:
    archive = request.app.state.settings.log_archives.get(stage)
    if archive is None:
        raise HTTPException(status_code=404, detail=f"no log archive is configured for stage {stage!r}")
    store = request.app.state.object_store
    if store is None:
        raise HTTPException(status_code=503, detail="the platform API holds no object-store key for the log archive")
    return LogArchive(store, archive.base_uri), archive.namespace


@contextmanager
def _archive_errors(stage: str) -> Iterator[None]:
    # The store's own message goes to the log: it can name the bucket, the key
    # or the endpoint, none of which the caller needs.
    try:
        yield
    except ClientError as exc:
        log.warning("the object store refused the log archive read for stage %s: %s", stage, exc)
        raise HTTPException(status_code=502, detail=f"the object store refused stage {stage!r}'s log archive") from exc
    except BotoCoreError as exc:
        log.warning("the object store is unreachable for stage %s: %s", stage, exc)
        raise HTTPException(status_code=502, detail="the object store is unreachable") from exc
    # KeyError and TypeError: a record that is JSON but not the archiver's shape.
    except (OSError, EOFError, zlib.error, ValueError, KeyError, TypeError) as exc:
        log.warning("stage %s's log archive is unreadable: %s", stage, exc)
        raise HTTPException(status_code=502, detail=f"stage {stage!r}'s log archive is unreadable") from exc


def _openmetadata(request: Request) -> OpenMetadataClient:
    url = request.app.state.settings.openmetadata_url
    if not url:
        raise HTTPException(status_code=404, detail="OpenMetadata is not configured on this deployment")
    # The token the API has just validated, unchanged. OpenMetadata checks its
    # signature and expiry but not its audience, so the API's own audience
    # check in front is the only one on this path.
    return OpenMetadataClient(url.rstrip("/"), token=request.state.token)


@contextmanager
def _openmetadata_errors() -> Iterator[None]:
    try:
        yield
    except ServiceClientError as exc:
        log.warning("OpenMetadata failed: %s", exc)
        if exc.status == 401:
            raise HTTPException(
                status_code=401,
                detail="OpenMetadata refused the token",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            ) from exc
        if exc.status == 403:
            raise HTTPException(status_code=403, detail="OpenMetadata denied the request to this user") from exc
        if isinstance(exc, TransientServiceError) and exc.status is None:
            raise HTTPException(status_code=502, detail="OpenMetadata is unreachable") from exc
        raise HTTPException(status_code=502, detail="OpenMetadata answered with an error") from exc


def _read_openmetadata[T](request: Request, read: Callable[[OpenMetadataClient], T]) -> T:
    """`read` with the caller's token; a caller OpenMetadata has never seen is registered once, then read again.

    OpenMetadata answers 404 "user instance for <name> not found" to a valid
    token whose user does not exist yet, as for a person who has only used the
    CLI (ADR 0021, decision 2). The role sync assigns their roles on its next run.
    """
    client = _openmetadata(request)
    with _openmetadata_errors():
        try:
            return read(client)
        except ServiceClientError as exc:
            principal: Principal = request.state.principal
            name = openmetadata_user_name(principal.username)
            if exc.status != 404 or "user instance for" not in str(exc) or not name or not principal.email:
                raise
            log.info("Registering %s in OpenMetadata on first use", name)
            client.register_self(name, principal.email)
            return read(client)


def openmetadata_user_name(preferred_username: str | None) -> str | None:
    """The name OpenMetadata gives the token's holder, so a registration matches the principal it checks.

    OpenMetadata 1.13.6 takes the first `jwtPrincipalClaims` entry, which this
    platform sets to `preferred_username` (modules/governance/openmetadata),
    keeps what comes before an `@`, and lowercases it
    (SecurityUtil.findUserNameFromClaims). Its self-signup refuses any other name
    with 403 OTHER_USER_SIGN_UP (UserResource.createUser), so a mismatch fails
    closed rather than creating someone else.
    """
    if not preferred_username:
        return None
    return preferred_username.split("@")[0].lower()


def list_domains(request: Request) -> Domains:
    domains = _read_openmetadata(request, lambda client: client.list_domains())
    return Domains(domains=[Domain(name=name, display_name=display_name) for name, display_name in domains])


def list_data_products(request: Request) -> DataProducts:
    products = _read_openmetadata(request, lambda client: client.list_data_products())
    return DataProducts(data_products=[DataProduct(**asdict(product)) for product in products])
