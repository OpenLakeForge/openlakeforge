"""The HTTP surface. Every route answers under /api, which the perimeter routes on the portal's host."""

from __future__ import annotations

import os
import uuid
from typing import Any, NoReturn

import boto3
from botocore.config import Config
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from openlakeforge_services.logs import ObjectStore

from openlakeforge_platform_api import reads, report_routes, writes
from openlakeforge_platform_api.audit import AuditLog, ObjectWriter
from openlakeforge_platform_api.audit import record as audit_record
from openlakeforge_platform_api.permissions import ANONYMOUS, Permission, PermissionMatrix
from openlakeforge_platform_api.settings import Settings
from openlakeforge_platform_api.tokens import InvalidToken, IssuerUnavailable, Principal, TokenValidator

SETTINGS_ENV = "OPENLAKEFORGE_PLATFORM_API_SETTINGS"
DEFAULT_SETTINGS_PATH = "/config/settings.json"
# The write-only audit key, mounted from its own Secret under names boto3 does not read on its own.
AUDIT_ACCESS_KEY_ENV = "OPENLAKEFORGE_AUDIT_ACCESS_KEY_ID"
AUDIT_SECRET_KEY_ENV = "OPENLAKEFORGE_AUDIT_SECRET_ACCESS_KEY"

_UNAUTHORIZED = {"WWW-Authenticate": 'Bearer error="invalid_token"'}


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _refuse(
    request: Request, permission: Permission | None, status: int, detail: str, headers: dict[str, str] | None = None
) -> NoReturn:
    """Answer `status`, auditing the refusal when the operation changes state.

    A refusal of an authenticated person is stored; one of a caller with no
    valid token is written to stdout only, so an anonymous caller cannot fill
    the audit bucket. Either way the refusal stands if the record cannot be
    stored: refusing is already the safe outcome.
    """
    if permission is not None and permission.changes_state:
        stage = request.path_params.get("stage")
        entry = audit_record(request, "refused", stage=stage, target={}, status=status, detail=detail)
        audit: AuditLog = request.app.state.audit
        if getattr(request.state, "principal", None) is None:
            audit.write(entry, durable=False)
        else:
            audit.write_best_effort(entry)
    raise HTTPException(status_code=status, detail=detail, headers=headers)


def _enforce(request: Request) -> None:
    """Applied to every route: validate the token, then check the route's row in the matrix.

    Identity comes from the bearer token alone. The perimeter's
    X-Auth-Request-* headers are never read: a header is not a signature.
    """
    # The API's own id for this request, for audit records and launch keys; never taken from the client.
    request.state.request_id = uuid.uuid4().hex
    request.state.principal = None
    route = request.scope.get("route")
    operation = route.name if isinstance(route, APIRoute) else ""
    matrix: PermissionMatrix = request.app.state.permissions
    permission = matrix.operations.get(operation)
    if permission is None:
        _refuse(request, None, 403, f"operation {operation!r} has no permission row")
    stage = request.path_params.get("stage")
    if stage is not None and stage not in request.app.state.settings.stages:
        _refuse(request, permission, 404, f"stage {stage!r} is not enabled")
    if permission.role == ANONYMOUS:
        if not matrix.allows(operation, None, stage):
            _refuse(request, permission, 403, "operation not permitted")
        return
    token = _bearer_token(request)
    if token is None:
        _refuse(request, permission, 401, "a bearer token is required", _UNAUTHORIZED)
    validator: TokenValidator = request.app.state.validator
    try:
        principal = validator.validate(token)
    except InvalidToken as exc:
        _refuse(request, permission, 401, f"token refused: {exc}", _UNAUTHORIZED)
    except IssuerUnavailable:
        _refuse(request, permission, 503, "the identity issuer is unavailable")
    request.state.principal = principal
    if principal.role is None:
        # ADR 0014: authenticated with no recognised role is denied everywhere.
        _refuse(request, permission, 403, "no OpenLakeForge role assigned; ask a platform-admin")
    if not matrix.allows(operation, principal.role, stage):
        # The refusal names what would pass, so a client can tell the person which role to ask for.
        if matrix.allows(operation, matrix.precedence[0], stage):
            detail = f"role {principal.role!r} may not perform {operation!r}; it needs {permission.role!r} or above"
        else:
            detail = f"{operation!r} does not apply to stage {stage!r}" if stage else f"{operation!r} needs a stage"
        _refuse(request, permission, 403, detail)
    # Kept for the routes that forward it to a service that authorizes the person (ADR 0021, decision 2).
    request.state.token = token


def _principal(request: Request) -> Principal:
    principal: Principal = request.state.principal
    return principal


def healthz() -> dict[str, str]:
    # Liveness of this process only: the issuer is not called, so an issuer
    # outage does not restart the pod.
    return {"status": "ok"}


def whoami(principal: Principal = Depends(_principal)) -> dict[str, Any]:  # noqa: B008 - FastAPI's dependency API
    return {
        "subject": principal.subject,
        "role": principal.role,
        "email": principal.email,
        "name": principal.name,
        "audience": principal.audience,
    }


def _object_store(settings: Settings) -> ObjectStore | None:
    """An S3 client for the log archive, or None when there is no archive or no key to read it with.

    The key is passed explicitly from the environment (the mounted Secret):
    left to boto3's credential chain, a missing key falls through to the
    instance metadata endpoint, which NetworkPolicy drops, so every request
    would hang until that times out. Creating the client opens no connection,
    so the API starts without the store. Path-style addressing because
    SeaweedFS serves buckets under one host.
    """
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not settings.log_archives or not access_key or not secret_key:
        return None
    client: ObjectStore = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}),
    )
    return client


def _audit_store(settings: Settings) -> ObjectWriter | None:
    """An S3 client with the write-only audit key, or None when there is no audit location or no key.

    Its own client, not the log reader's: the two keys are separate identities
    with separate grants, and neither can do what the other does.
    """
    access_key = os.environ.get(AUDIT_ACCESS_KEY_ENV)
    secret_key = os.environ.get(AUDIT_SECRET_KEY_ENV)
    if not settings.audit_base_uri or not access_key or not secret_key:
        return None
    client: ObjectWriter = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}),
    )
    return client


def create_app(
    settings: Settings,
    *,
    permissions: PermissionMatrix | None = None,
    validator: TokenValidator | None = None,
    object_store: ObjectStore | None = None,
    audit: AuditLog | None = None,
) -> FastAPI:
    # No interactive docs or OpenAPI route: each would be a route outside the
    # matrix. ADR 0021 decision 5 commits the OpenAPI document under docs/schema.
    app = FastAPI(
        title="OpenLakeForge platform API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(_enforce)],
    )
    app.state.settings = settings
    app.state.permissions = permissions or PermissionMatrix.packaged(settings.precedence)
    app.state.validator = validator or TokenValidator(settings)
    app.state.object_store = object_store or _object_store(settings)
    app.state.audit = audit or AuditLog(_audit_store(settings), settings.audit_base_uri)
    app.add_api_route("/api/healthz", healthz, methods=["GET"], name="healthz")
    app.add_api_route("/api/v1/whoami", whoami, methods=["GET"], name="whoami")
    app.add_api_route("/api/v1/stages/{stage}/jobs", reads.list_jobs, methods=["GET"], name="list_jobs")
    app.add_api_route("/api/v1/stages/{stage}/runs", reads.list_runs, methods=["GET"], name="list_runs")
    app.add_api_route(
        "/api/v1/stages/{stage}/runs", writes.launch_run, methods=["POST"], status_code=202, name="launch_run"
    )
    app.add_api_route("/api/v1/stages/{stage}/runs/{run_id}", reads.get_run, methods=["GET"], name="get_run")
    app.add_api_route("/api/v1/stages/{stage}/logs", reads.list_logs, methods=["GET"], name="list_logs")
    app.add_api_route(
        "/api/v1/stages/{stage}/reports",
        report_routes.import_report,
        methods=["POST"],
        name="import_report",
    )
    app.add_api_route(
        "/api/v1/stages/{stage}/reports/{dashboard}/export",
        report_routes.export_report,
        methods=["GET"],
        name="export_report",
    )
    app.add_api_route("/api/v1/domains", reads.list_domains, methods=["GET"], name="list_domains")
    app.add_api_route("/api/v1/data-products", reads.list_data_products, methods=["GET"], name="list_data_products")
    return app


def main() -> FastAPI:
    """uvicorn's factory: `uvicorn --factory openlakeforge_platform_api.app:main`."""
    return create_app(Settings.from_file(os.environ.get(SETTINGS_ENV, DEFAULT_SETTINGS_PATH)))
