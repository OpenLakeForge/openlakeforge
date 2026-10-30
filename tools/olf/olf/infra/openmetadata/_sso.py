"""Switch OpenMetadata to the identity.oidc issuer, once the issuer resolves.

OpenMetadata builds its SSO handler when it applies the security configuration
(and at every start) and never retries a discovery that failed. The issuer's
host resolves in the cluster only once Traefik exists, which is after the
platform apply, so the Terraform-run bootstrap Job cannot do this (#432). It
only publishes the configuration (no secret) in a ConfigMap; this runs in the
dynamic phase of `olf deploy`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping

from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.infra import access, k8s
from olf.infra.openmetadata._auth import automation_token

# Written by modules/governance/openmetadata: config.json, secret_name, secret_key.
CONFIGMAP = "openmetadata-sso-config"
PROBE_ATTEMPTS = 60
PROBE_DELAY = 5.0
# The server mounts the local CA here; a public issuer certificate verifies without it.
_PROBE = 'wget -q -T 5 -O /dev/null "$0" || SSL_CERT_FILE=/etc/olf/oidc-ca/ca.crt wget -q -T 5 -O /dev/null "$0"'


def _wait_for_issuer(service: str, namespace: str, url: str) -> None:
    """Probe from the OpenMetadata pod: the operator's machine may not resolve the issuer."""
    deadline = time.monotonic() + PROBE_ATTEMPTS * PROBE_DELAY
    while True:
        if k8s.exec_succeeds(service, namespace, ["sh", "-c", _PROBE, url]):
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


def apply_sso(namespace: str, *, service: str, remote_port: int, log_path: str) -> None:
    """Apply the published security configuration, restarting the server on its first application."""
    data = k8s.configmap_data(CONFIGMAP, namespace)
    if data is None:  # no spec.identity: basic login stays
        return
    config = json.loads(data["config.json"])
    token = automation_token(namespace)
    if token is None:
        raise OpenMetadataError(f"The OpenMetadata automation bot token Secret is missing in {namespace}.")
    oidc = config["authenticationConfiguration"]
    # What GET /system/config/auth reports back; any difference re-applies.
    desired = {key: oidc[key] for key in ("provider", "authority", "clientId", "callbackUrl")}
    with access.service_url(
        "shared/governance_service",
        service=service,
        remote_port=remote_port,
        namespace=namespace,
        log_path=log_path,
        shared_namespace=namespace,
    ) as base_url:
        client = OpenMetadataClient(base_url, token=token)
        current: Mapping[str, object] = client.request("GET", "/api/v1/system/config/auth")
        # Always PUT: the client secret is not readable back, and a rotation
        # changes nothing else. Only a first application needs the restart.
        first = not all(current.get(key) == value for key, value in desired.items())
        _wait_for_issuer(service, namespace, oidc["oidcConfiguration"]["discoveryUri"])
        oidc["oidcConfiguration"]["secret"] = k8s.secret_value(data["secret_name"], data["secret_key"], namespace)
        client.request("PUT", "/api/v1/system/security/config", json_body=config, ok_statuses=(200, 201))
    # The handler is built from the stored configuration at startup, and the
    # one the PUT built may predate the issuer answering.
    if first:
        k8s.restart_deployment(service, namespace)
    # After the restart: startup re-promotes anything in the configured admin list.
    with access.service_url(
        "shared/governance_service",
        service=service,
        remote_port=remote_port,
        namespace=namespace,
        log_path=log_path,
        shared_namespace=namespace,
    ) as base_url:
        _demote_local_admin(OpenMetadataClient(base_url, token=token))
