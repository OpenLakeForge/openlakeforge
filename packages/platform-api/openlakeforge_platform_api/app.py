"""The HTTP surface. Every route answers under /api, which the perimeter routes on the portal's host."""

from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute

from openlakeforge_platform_api import reads
from openlakeforge_platform_api.permissions import ANONYMOUS, PermissionMatrix
from openlakeforge_platform_api.settings import Settings
from openlakeforge_platform_api.tokens import InvalidToken, IssuerUnavailable, Principal, TokenValidator

SETTINGS_ENV = "OPENLAKEFORGE_PLATFORM_API_SETTINGS"
DEFAULT_SETTINGS_PATH = "/config/settings.json"

_UNAUTHORIZED = {"WWW-Authenticate": 'Bearer error="invalid_token"'}


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _enforce(request: Request) -> None:
    """Applied to every route: validate the token, then check the route's row in the matrix.

    Identity comes from the bearer token alone. The perimeter's
    X-Auth-Request-* headers are never read: a header is not a signature.
    """
    route = request.scope.get("route")
    operation = route.name if isinstance(route, APIRoute) else ""
    matrix: PermissionMatrix = request.app.state.permissions
    permission = matrix.operations.get(operation)
    if permission is None:
        raise HTTPException(status_code=403, detail=f"operation {operation!r} has no permission row")
    stage = request.path_params.get("stage")
    if stage is not None and stage not in request.app.state.settings.stages:
        raise HTTPException(status_code=404, detail=f"stage {stage!r} is not enabled")
    if permission.role == ANONYMOUS:
        if not matrix.allows(operation, None, stage):
            raise HTTPException(status_code=403, detail="operation not permitted")
        return
    token = _bearer_token(request)
    if token is None:
        raise HTTPException(status_code=401, detail="a bearer token is required", headers=_UNAUTHORIZED)
    validator: TokenValidator = request.app.state.validator
    try:
        principal = validator.validate(token)
    except InvalidToken as exc:
        raise HTTPException(status_code=401, detail=f"token refused: {exc}", headers=_UNAUTHORIZED) from exc
    except IssuerUnavailable as exc:
        raise HTTPException(status_code=503, detail="the identity issuer is unavailable") from exc
    if principal.role is None:
        # ADR 0014: authenticated with no recognised role is denied everywhere.
        raise HTTPException(status_code=403, detail="no OpenLakeForge role assigned; ask a platform-admin")
    if not matrix.allows(operation, principal.role, stage):
        raise HTTPException(status_code=403, detail=f"role {principal.role!r} may not perform {operation!r}")
    request.state.principal = principal
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


def create_app(
    settings: Settings,
    *,
    permissions: PermissionMatrix | None = None,
    validator: TokenValidator | None = None,
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
    app.add_api_route("/api/healthz", healthz, methods=["GET"], name="healthz")
    app.add_api_route("/api/v1/whoami", whoami, methods=["GET"], name="whoami")
    app.add_api_route("/api/v1/stages/{stage}/jobs", reads.list_jobs, methods=["GET"], name="list_jobs")
    app.add_api_route("/api/v1/stages/{stage}/runs", reads.list_runs, methods=["GET"], name="list_runs")
    app.add_api_route("/api/v1/stages/{stage}/runs/{run_id}", reads.get_run, methods=["GET"], name="get_run")
    app.add_api_route("/api/v1/domains", reads.list_domains, methods=["GET"], name="list_domains")
    app.add_api_route("/api/v1/data-products", reads.list_data_products, methods=["GET"], name="list_data_products")
    return app


def main() -> FastAPI:
    """uvicorn's factory: `uvicorn --factory openlakeforge_platform_api.app:main`."""
    return create_app(Settings.from_file(os.environ.get(SETTINGS_ENV, DEFAULT_SETTINGS_PATH)))
