"""The Polaris bootstrap: stage catalogs, workload principals and their grants, and people's roles (#562).

Ported from the catalog module's `bootstrap.sh.tftpl` and `person-access.sh.tftpl`, which the
`polaris-bootstrap-<revision>` Job runs during `terraform apply`. Until that Job is deleted both run,
the Job first, so every step reads the current state and writes only what is missing: each converges
on the state the other leaves. The calls and their payloads are the template's own.

Credentials travel from the Polaris response straight into a Secret manifest on kubectl's stdin
(`k8s.write_secret`); none is logged, put in argv, or quoted in an error.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

from olf.common import log
from olf.infra import k8s
from olf.infra.polaris import PolarisClient, PolarisError

DROP_WITH_PURGE = "polaris.config.drop-with-purge.enabled"


@dataclass(frozen=True)
class Credential:
    """A Polaris principal and the Secret that holds its one-time client credentials."""

    principal: str
    secret: str
    client_id_key: str
    client_secret_key: str
    namespace: str
    # A stage namespace is deleted by `--allow-stage-removal` and recreated empty on re-enable, while
    # its principal survives in the metastore. A shared namespace never loses a Secret that way.
    recreatable: bool = False

    @property
    def pending_marker(self) -> str:
        return f"{self.secret}-bootstrap-pending"


@dataclass(frozen=True)
class CatalogSts:
    role_arn: str
    region: str
    endpoint: str
    sts_endpoint: str


@dataclass(frozen=True)
class StageCatalog:
    stage: str
    catalog_name: str
    bronze_bucket: str
    silver_bucket: str
    gold_bucket: str
    trino: Credential
    floe: Credential
    deployer: Credential


def _q(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def _call(
    client: PolarisClient,
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    ok: tuple[int, ...] = (200,),
    absent: tuple[int, ...] = (),
) -> dict | None:
    """The response body, or None when Polaris answered one of the `absent` statuses."""
    try:
        return client.management(method, path, payload=payload, ok_statuses=ok)
    except PolarisError as err:
        if err.status in absent:
            return None
        raise


def ensure_catalog(client: PolarisClient, stage: StageCatalog, sts: CatalogSts | None) -> None:
    """Create the stage catalog, or give an existing one the drop-with-purge property.

    Trino drops tables with purge, and Polaris drops a view only with purge, answering 403 unless the
    catalog allows it (#741). A stage catalog's `allowedLocations` are its own stage's buckets, so a
    purge cannot reach another stage's files (ADR 0004).
    """
    storage: dict[str, object] = {
        "storageType": "S3",
        "allowedLocations": [
            f"s3://{stage.bronze_bucket}/",
            f"s3://{stage.silver_bucket}/",
            f"s3://{stage.gold_bucket}/",
        ],
        "pathStyleAccess": True,
    }
    if sts is None:
        storage["stsUnavailable"] = True
    else:
        storage.update(
            roleArn=sts.role_arn,
            region=sts.region,
            endpoint=sts.endpoint,
            stsEndpoint=sts.sts_endpoint,
            stsUnavailable=False,
        )
    created = _call(
        client,
        "POST",
        "/catalogs",
        {
            "name": stage.catalog_name,
            "type": "INTERNAL",
            "properties": {"default-base-location": f"s3://{stage.silver_bucket}/", DROP_WITH_PURGE: "true"},
            "storageConfigInfo": storage,
        },
        ok=(201,),
        absent=(409,),
    )
    if created is not None:
        log.info(f"Created Polaris catalog '{stage.catalog_name}'.")
        return
    path = f"/catalogs/{_q(stage.catalog_name)}"
    current = client.management("GET", path)
    if sts is not None and (current.get("storageConfigInfo") or {}).get("stsUnavailable") is not False:
        # Polaris refuses to add a role ARN to an existing catalog (ADR 0019).
        log.warn(
            f"Catalog {stage.catalog_name} was created without STS and vends no credentials; "
            "a stack redeployed from scratch gets it."
        )
    properties = current.get("properties") or {}
    if properties.get(DROP_WITH_PURGE) != "true":
        # Polaris replaces the whole property map on update, so the current one is kept.
        client.management(
            "PUT",
            path,
            payload={
                "currentEntityVersion": current.get("entityVersion"),
                "properties": {**properties, DROP_WITH_PURGE: "true"},
            },
        )


def _rotate(client: PolarisClient, principal: str) -> dict:
    return client.management("POST", f"/principals/{_q(principal)}/rotate")


def ensure_credential(client: PolarisClient, credential: Credential, *, environ: Mapping[str, str] | None) -> None:
    """Create the principal and its Secret, or recover the Secret, without rotating a client in use.

    The pending marker is written before the first create call. It marks the otherwise unrecoverable
    window where Polaris created a principal but Kubernetes did not keep its one-time credentials, and
    it takes precedence over an older Secret, which cannot authenticate to the new principal.
    """
    namespace, principal = credential.namespace, credential.principal

    def exists(kind: str, name: str) -> bool:
        return k8s.resource_exists(kind, name, namespace, environ=environ)

    if _call(client, "GET", f"/principals/{_q(principal)}", absent=(404,)) is not None:
        if exists("configmap", credential.pending_marker):
            issued = _rotate(client, principal)
        elif exists("secret", credential.secret):
            return
        elif credential.recreatable:
            log.warn(
                f"Polaris principal '{principal}' exists but '{namespace}' has no Secret or pending marker; "
                "treating this as a removed-then-re-enabled stage and rotating its credentials."
            )
            issued = _rotate(client, principal)
        else:
            raise PolarisError(
                f"Polaris principal '{principal}' exists but credential Secret '{credential.secret}' is missing "
                f"in '{namespace}'. Restore the Secret before rerunning, to avoid rotating active clients."
            )
    else:
        if not exists("configmap", credential.pending_marker):
            k8s.apply_manifest(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": credential.pending_marker, "namespace": namespace},
                    "data": {"principal": principal},
                },
                environ=environ,
            )
        # In-memory Polaris state is not migrated: an older Secret cannot authenticate to the new principal.
        k8s.delete_secret(credential.secret, namespace, environ=environ)
        created = _call(client, "POST", "/principals", {"name": principal, "type": "SERVICE"}, ok=(201,), absent=(409,))
        issued = created if created is not None else _rotate(client, principal)

    pair = issued.get("credentials") or {}
    client_id, client_secret = pair.get("clientId"), pair.get("clientSecret")
    if not client_id or not client_secret:
        raise PolarisError(f"Polaris returned no client credentials for principal '{principal}'.")
    # Replaced rather than applied, so the Secret holds exactly these two keys.
    k8s.delete_secret(credential.secret, namespace, environ=environ)
    k8s.write_secret(
        credential.secret,
        namespace,
        {credential.client_id_key: client_id, credential.client_secret_key: client_secret},
        overwrite=False,
        environ=environ,
    )
    k8s.delete_configmap(credential.pending_marker, namespace, environ=environ)
    log.info(f"Stored the credentials of Polaris principal '{principal}' in '{namespace}'.")
