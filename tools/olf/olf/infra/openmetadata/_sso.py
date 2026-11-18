"""Switch OpenMetadata to the identity.oidc issuer, once the issuer resolves.

OpenMetadata builds its SSO handler when it applies the security configuration
(and at every start) and never retries a discovery that failed. The issuer's
host resolves in the cluster only once Traefik exists, which is after the
platform apply, so the governance Terraform module cannot do this (#432). It
only publishes the configuration (no secret) in a ConfigMap; this runs in the
dynamic phase of `olf infra deploy`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from contextlib import AbstractContextManager

from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.infra import access, k8s
from olf.infra.access import Route
from olf.infra.openmetadata._auth import automation_token
from olf.infra.openmetadata._bootstrap import ensure_admin_password, reconcile_admin_password, wait_healthy

DEFAULT_ADMIN_EMAIL = "admin@open-metadata.org"

# Written by modules/governance/openmetadata: config.json, secret_name, secret_key.
CONFIGMAP = "openmetadata-sso-config"
PROBE_ATTEMPTS = 60
PROBE_DELAY = 5.0
# The server mounts the local CA here; a public issuer certificate verifies without it.
_PROBE = 'wget -q -T 5 -O /dev/null "$0" || SSL_CERT_FILE=/etc/olf/oidc-ca/ca.crt wget -q -T 5 -O /dev/null "$0"'


def _wait_for_issuer(service: str, namespace: str, url: str, environ: Mapping[str, str] | None = None) -> None:
    """Probe from the OpenMetadata pod: the operator's machine may not resolve the issuer."""
    deadline = time.monotonic() + PROBE_ATTEMPTS * PROBE_DELAY
    while True:
        if k8s.exec_succeeds(service, namespace, ["sh", "-c", _PROBE, url], environ=environ):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(PROBE_DELAY)
    raise OpenMetadataError(
        f"The identity issuer's discovery URL {url} did not answer from inside the cluster within "
        f"{int(PROBE_ATTEMPTS * PROBE_DELAY)}s; OpenMetadata would fail to initialise SSO. "
        "Check that Traefik and the issuer are running, then rerun 'olf openmetadata deploy-metadata'."
    )


def _demote_local_admin(client: OpenMetadataClient) -> None:
    """The local admin has no login under SSO, but an issuer user named `admin` would resolve to it (OM-11)."""
    user = client.request("GET", "/api/v1/users/name/admin")
    if user.get("isAdmin"):
        client.request(
            "PATCH",
            f"/api/v1/users/{user['id']}",
            json_body=[{"op": "add", "path": "/isAdmin", "value": False}],
            content_type="application/json-patch+json",
        )


# The release's own basic-login settings (the chart renders them into these Secrets), by security-config field.
_BASIC_AUTHENTICATION = {
    "provider": "AUTHENTICATION_PROVIDER",
    "providerName": "AUTHENTICATION_PROVIDER_NAME",
    "publicKeyUrls": "AUTHENTICATION_PUBLIC_KEYS",
    "authority": "AUTHENTICATION_AUTHORITY",
    "clientId": "AUTHENTICATION_CLIENT_ID",
    "callbackUrl": "AUTHENTICATION_CALLBACK_URL",
    "jwtPrincipalClaims": "AUTHENTICATION_JWT_PRINCIPAL_CLAIMS",
    "enableSelfSignup": "AUTHENTICATION_ENABLE_SELF_SIGNUP",
    "clientType": "AUTHENTICATION_CLIENT_TYPE",
    "responseType": "AUTHENTICATION_RESPONSE_TYPE",
}


def _release_value(secret: str, key: str, namespace: str, environ: Mapping[str, str] | None) -> object:
    """A chart setting, which the chart quotes as JSON; a key the chart did not render is empty."""
    try:
        raw = k8s.secret_value(secret, key, namespace, environ=environ)
    except k8s.KubectlError:
        return ""
    return json.loads(raw) if raw else ""


def _restore_basic(
    namespace: str,
    *,
    service: str,
    remote_port: int,
    log_path: str,
    admin_email: str,
    environ: Mapping[str, str] | None,
) -> None:
    """Put back the release's basic login after spec.identity was removed, with the Secret's admin password.

    The stored copy wins over the environment (see the module docstring), so a server that once had SSO keeps
    redirecting to an issuer that no longer exists until the configuration is written back.
    """
    token = automation_token(namespace, environ=environ)
    if token is None:  # no bot yet: a fresh install, which reads basic login from the environment
        return

    def route() -> AbstractContextManager[Route]:
        return access.service_route(
            "shared/governance_service",
            service=service,
            remote_port=remote_port,
            namespace=namespace,
            log_path=log_path,
            shared_namespace=namespace,
            environ=environ,
        )

    # A stack first deployed with identity still has the default password (#1281): the platform
    # bootstrap reconciles it only while basic login is on, and this deploy's ran under SSO.
    password = ensure_admin_password(namespace, environ)
    with route() as current:
        client = current.client(OpenMetadataClient, token=token)
        if client.request("GET", "/api/v1/system/config/auth").get("provider") == "basic":
            reconcile_admin_password(client, current, admin_email, password)
            return
        config = dict(client.request("GET", "/api/v1/system/security/config"))
        secret = f"{service}-authentication-secret"
        config["authenticationConfiguration"] = {
            field: _release_value(secret, key, namespace, environ) for field, key in _BASIC_AUTHENTICATION.items()
        }
        # The SSO path demoted the local admin; startup re-promotes the release's admin principals.
        config["authorizerConfiguration"]["adminPrincipals"] = _release_value(
            f"{service}-authorizer-secret", "AUTHORIZER_ADMIN_PRINCIPALS", namespace, environ
        )
        client.request("PUT", "/api/v1/system/security/config", json_body=config, ok_statuses=(200, 201))
    # The handler is built from the stored configuration at startup.
    k8s.restart_deployment(service, namespace, environ=environ)
    with route() as restarted:
        wait_healthy(restarted)
        reconcile_admin_password(restarted.client(OpenMetadataClient, token=token), restarted, admin_email, password)


def apply_sso(
    namespace: str,
    *,
    service: str,
    remote_port: int,
    log_path: str,
    admin_email: str = DEFAULT_ADMIN_EMAIL,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Apply the published security configuration, restarting the server on its first application."""
    data = k8s.configmap_data(CONFIGMAP, namespace, environ=environ)
    if data is None:  # no spec.identity: basic login, which an earlier deploy with identity may have replaced
        _restore_basic(
            namespace,
            service=service,
            remote_port=remote_port,
            log_path=log_path,
            admin_email=admin_email,
            environ=environ,
        )
        return
    config = json.loads(data["config.json"])
    token = automation_token(namespace, environ=environ)
    if token is None:
        raise OpenMetadataError(f"The OpenMetadata automation bot token Secret is missing in {namespace}.")
    oidc = config["authenticationConfiguration"]
    # What GET /system/config/auth reports back; any difference re-applies.
    desired = {key: oidc[key] for key in ("provider", "authority", "clientId", "callbackUrl")}
    with access.service_route(
        "shared/governance_service",
        service=service,
        remote_port=remote_port,
        namespace=namespace,
        log_path=log_path,
        shared_namespace=namespace,
        environ=environ,
    ) as route:
        client = route.client(OpenMetadataClient, token=token)
        current: Mapping[str, object] = client.request("GET", "/api/v1/system/config/auth")
        # Always PUT: the client secret is not readable back, and a rotation
        # changes nothing else. Only a first application needs the restart.
        first = not all(current.get(key) == value for key, value in desired.items())
        _wait_for_issuer(service, namespace, oidc["oidcConfiguration"]["discoveryUri"], environ)
        oidc["oidcConfiguration"]["secret"] = k8s.secret_value(
            data["secret_name"], data["secret_key"], namespace, environ=environ
        )
        client.request("PUT", "/api/v1/system/security/config", json_body=config, ok_statuses=(200, 201))
    # The handler is built from the stored configuration at startup, and the
    # one the PUT built may predate the issuer answering.
    if first:
        k8s.restart_deployment(service, namespace, environ=environ)
    # After the restart: startup re-promotes anything in the configured admin list.
    with access.service_route(
        "shared/governance_service",
        service=service,
        remote_port=remote_port,
        namespace=namespace,
        log_path=log_path,
        shared_namespace=namespace,
        environ=environ,
    ) as route:
        _demote_local_admin(route.client(OpenMetadataClient, token=token))
