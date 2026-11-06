"""The Polaris bootstrap: stage catalogs, workload principals and their grants, and people's roles (#562).

The platform phase runs it once `terraform apply` has finished; it replaced the catalog module's
`polaris-bootstrap-<revision>` Job and its shell templates. Every step reads the current state and
writes only what is missing, so a rerun changes nothing and a step that failed is repaired by the next.

Credentials travel from the Polaris response straight into a Secret manifest on kubectl's stdin
(`k8s.write_secret`); none is logged, put in argv, or quoted in an error.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

from olf.common import log
from olf.common.provider_contracts import ProviderContracts, StageContract
from olf.infra import k8s
from olf.infra.polaris import PolarisClient, PolarisConfig, PolarisError

# The catalog module's Secret holding the root principal's credentials, beside Polaris.
ROOT_CREDENTIALS_SECRET = "polaris-bootstrap-credentials"
# The SeaweedFS module's role Polaris assumes through the gateway's STS (ADR 0019).
SEAWEEDFS_VENDING_ROLE = "arn:aws:iam::000000000000:role/polaris-vending"
# The Dex module's GitHub connector id, which names the one principal every person's token maps to.
DEX_CONNECTOR = "github"
# The label the Trino chart puts on the coordinator and worker Deployments.
TRINO_SELECTOR = "app.kubernetes.io/name=trino"
# The pod-template annotation that names the Polaris credentials Trino's pods started with.
TRINO_CREDENTIALS_ANNOTATION = "openlakeforge.io/polaris-credentials"
DROP_WITH_PURGE = "polaris.config.drop-with-purge.enabled"
MANAGE_CONTENT = "CATALOG_MANAGE_CONTENT"
MANAGE_METADATA = "CATALOG_MANAGE_METADATA"
MANAGE_ACCESS = "CATALOG_MANAGE_ACCESS"
# The catalog role each person role holds on a stage catalog, per its data_access row (ADR 0019).
PERSON_CATALOG_ROLES: Mapping[str, tuple[str, ...]] = {
    "olf-gold-read": ("NAMESPACE_LIST",),
    "olf-read": ("TABLE_READ_DATA", "NAMESPACE_READ_PROPERTIES", "VIEW_READ_PROPERTIES"),
    "olf-write": (MANAGE_CONTENT,),
}
# Removed on every run: Gold writes through Trino, and pre-migration deployments had a dbt principal.
LEGACY_DBT_PRINCIPAL = "dbt"
LEGACY_DBT_SECRET = "polaris-dbt-creds"


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
class Grant:
    """`principal` holds `principal_role`, which holds `catalog_role` on the catalog, which holds `privilege`."""

    principal: str
    principal_role: str
    catalog_role: str
    privilege: str


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


@dataclass(frozen=True)
class PersonAccess:
    principal: str
    # Principal role -> catalog -> the one catalog role it holds there; a role with no entry holds none.
    assigned: Mapping[str, Mapping[str, str]]
    people: tuple[Credential, ...]


@dataclass(frozen=True)
class BootstrapPlan:
    shared_namespace: str
    stages: tuple[StageCatalog, ...]
    openmetadata: Credential
    sts: CatalogSts | None = None
    person_access: PersonAccess | None = None


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


def _present(client: PolarisClient, path: str, field: str, value: str) -> bool:
    """Whether the listing at `path` has an entry whose `field` is `value`; a missing listing has none.

    Polaris answers a repeated role assignment or grant with HTTP 500 (a `grant_records_pkey`
    violation), not 409, so the bootstrap reads first and writes only what is missing.
    """
    body = _call(client, "GET", path, absent=(404,)) or {}
    return any(
        isinstance(entry, dict) and entry.get(field) == value
        for listing in body.values()
        if isinstance(listing, list)
        for entry in listing
    )


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


def _ensure_principal_role(client: PolarisClient, principal: str, role: str) -> None:
    _call(client, "POST", "/principal-roles", {"principalRole": {"name": role}}, ok=(201,), absent=(409,))
    if not _present(client, f"/principals/{_q(principal)}/principal-roles", "name", role):
        client.management(
            "PUT",
            f"/principals/{_q(principal)}/principal-roles",
            payload={"principalRole": {"name": role}},
            ok_statuses=(201, 409),
        )


def _ensure_catalog_role(client: PolarisClient, catalog: str, role: str, privileges: tuple[str, ...]) -> None:
    path = f"/catalogs/{_q(catalog)}/catalog-roles"
    _call(client, "POST", path, {"catalogRole": {"name": role}}, ok=(201,), absent=(409,))
    for privilege in privileges:
        if not _present(client, f"{path}/{_q(role)}/grants", "privilege", privilege):
            client.management(
                "PUT",
                f"{path}/{_q(role)}/grants",
                payload={"grant": {"type": "catalog", "privilege": privilege}},
                ok_statuses=(201, 409),
            )


def _assign_catalog_role(client: PolarisClient, principal_role: str, catalog: str, catalog_role: str) -> None:
    client.management(
        "PUT",
        f"/principal-roles/{_q(principal_role)}/catalog-roles/{_q(catalog)}",
        payload={"catalogRole": {"name": catalog_role}},
        ok_statuses=(201, 409),
    )


def grant_catalog_access(client: PolarisClient, grant: Grant, catalog: str) -> None:
    _ensure_principal_role(client, grant.principal, grant.principal_role)
    _ensure_catalog_role(client, catalog, grant.catalog_role, (grant.privilege,))
    held = f"/principal-roles/{_q(grant.principal_role)}/catalog-roles/{_q(catalog)}"
    if not _present(client, held, "name", grant.catalog_role):
        _assign_catalog_role(client, grant.principal_role, catalog, grant.catalog_role)


def reconcile_person_access(
    client: PolarisClient, access: PersonAccess, catalogs: tuple[str, ...], *, environ: Mapping[str, str] | None
) -> None:
    """People's access (ADR 0019): the one principal every person's token names, a principal role per
    canonical role, and on each catalog exactly the catalog role its data_access row needs. A narrowed
    row revokes the grant it no longer names, which an add-only bootstrap would leave in place."""
    _call(client, "POST", "/principals", {"principal": {"name": access.principal}}, ok=(201,), absent=(409,))
    roles = sorted(access.assigned)
    for role in roles:
        _ensure_principal_role(client, access.principal, role)
    for catalog in catalogs:
        for catalog_role, privileges in sorted(PERSON_CATALOG_ROLES.items()):
            _ensure_catalog_role(client, catalog, catalog_role, privileges)
        for role in roles:
            wanted = access.assigned[role].get(catalog)
            held = f"/principal-roles/{_q(role)}/catalog-roles/{_q(catalog)}"
            for catalog_role in sorted(PERSON_CATALOG_ROLES):
                if _present(client, held, "name", catalog_role):
                    if catalog_role != wanted:
                        client.management("DELETE", f"{held}/{_q(catalog_role)}", ok_statuses=(204,))
                elif catalog_role == wanted:
                    _assign_catalog_role(client, role, catalog, catalog_role)
    # Each stage's Trino people catalog credential holds no principal role and so no grant: Trino uses it
    # only for the catalog's configuration call, and every table request carries the person's own token.
    for person in access.people:
        ensure_credential(client, person, environ=environ)


def run_bootstrap(client: PolarisClient, plan: BootstrapPlan, *, environ: Mapping[str, str] | None = None) -> None:
    """Every step, in order. `client` is logged in as the root principal."""
    for stage in plan.stages:
        ensure_catalog(client, stage, plan.sts)
    for stage in plan.stages:
        ensure_credential(client, stage.trino, environ=environ)
        grant_catalog_access(
            client,
            Grant(stage.trino.principal, f"data-engineer-{stage.stage}", "catalog-admin", MANAGE_CONTENT),
            stage.catalog_name,
        )
        ensure_credential(client, stage.floe, environ=environ)
        grant_catalog_access(
            client,
            Grant(stage.floe.principal, f"data-writer-{stage.stage}", "catalog-writer", MANAGE_CONTENT),
            stage.catalog_name,
        )

    _call(client, "DELETE", f"/principals/{LEGACY_DBT_PRINCIPAL}", ok=(204,), absent=(404,))
    k8s.delete_secret(LEGACY_DBT_SECRET, plan.shared_namespace, environ=environ)

    om = plan.openmetadata
    ensure_credential(client, om, environ=environ)
    for stage in plan.stages:
        grant_catalog_access(
            client,
            Grant(om.principal, "data-reader", f"catalog-reader-{stage.stage}", MANAGE_CONTENT),
            stage.catalog_name,
        )

    # Namespace lifecycle belongs to the artifacts phase (ADR 0002): this stops at handing
    # `olf infra catalog sync-namespaces` a principal that can create and drop namespaces.
    for stage in plan.stages:
        deployer = stage.deployer
        ensure_credential(client, deployer, environ=environ)
        privileges = (MANAGE_METADATA, MANAGE_ACCESS) if plan.person_access else (MANAGE_METADATA,)
        # With people's access, sync-namespaces grants olf-gold-read on each Gold namespace as the deployer.
        for privilege in privileges:
            grant_catalog_access(
                client,
                Grant(deployer.principal, f"namespace-admin-{stage.stage}", "catalog-namespace-admin", privilege),
                stage.catalog_name,
            )

    if plan.person_access:
        reconcile_person_access(
            client, plan.person_access, tuple(stage.catalog_name for stage in plan.stages), environ=environ
        )


def _credential(principal: str, secret: str, keys: str, namespace: str) -> Credential:
    return Credential(principal, secret, f"{keys}_CLIENT_ID", f"{keys}_CLIENT_SECRET", namespace)


def _stage_credential(stage: StageContract, role: str) -> Credential:
    catalog = stage.catalog
    return Credential(
        f"{role}-{stage.name.value}",
        catalog[f"{role}_credentials_secret_name"],
        catalog[f"{role}_client_id_key"],
        catalog[f"{role}_client_secret_key"],
        stage.namespace,
        recreatable=True,
    )


def _person_access(contracts: ProviderContracts, stages: Mapping[str, str], shared: str) -> PersonAccess | None:
    """data_access as Polaris roles and grants, for the Dex that GitHub sign-in deploys (ADR 0019)."""
    identity = contracts.shared.values.get("identity") or {}
    if identity.get("adapter") != "dex":
        return None

    def catalog_role(layers: Mapping[str, str]) -> str:
        # A row is one of three shapes, which olf validates: every layer write, every layer read, or Gold read.
        return "olf-write" if layers.get("gold") == "write" else "olf-read" if len(layers) == 3 else "olf-gold-read"

    assigned: dict[str, Mapping[str, str]] = {"olf-signed-in": {}}
    for role, rows in identity["roles"]["data_access"].items():
        assigned[f"olf-{role}"] = {
            stages[stage]: catalog_role(layers) for stage, layers in rows.items() if stage in stages
        }
    people = tuple(
        _credential(f"trino-people-{stage}", f"polaris-{stage}-trino-people-creds", "POLARIS", shared)
        for stage in sorted(stages)
    )
    return PersonAccess(DEX_CONNECTOR, assigned, people)


def bootstrap_plan(contracts: ProviderContracts) -> BootstrapPlan | None:
    """The plan for every enabled stage whose catalog is Polaris; None when none is (Glue needs no bootstrap).

    Stage catalogs, buckets, namespaces and the Floe and deployer Secrets come from the contract. The other
    Secret names are the environment roots' fixed ones (`stage_catalogs`), which Trino and OpenMetadata read.
    """
    stages = sorted(
        (stage for stage in contracts.stages.values() if stage.catalog.get("catalog_provider") == "polaris"),
        key=lambda stage: stage.name.value,
    )
    if not stages:
        return None
    shared = polaris_service(contracts)[2]
    storage = stages[0].storage
    # A catalog vends credentials when the storage adapter offers an STS, and SeaweedFS, the only storage
    # behind Polaris, always does, on its S3 port.
    sts = (
        CatalogSts(SEAWEEDFS_VENDING_ROLE, storage["region"], storage["endpoint"], storage["endpoint"])
        if str(storage.get("implementation", "")).startswith("storage.s3_compatible.seaweedfs")
        else None
    )
    catalogs = {stage.name.value: stage.catalog["catalog_name"] for stage in stages}
    return BootstrapPlan(
        shared_namespace=shared,
        stages=tuple(
            StageCatalog(
                stage.name.value,
                stage.catalog["catalog_name"],
                stage.storage["bronze"]["bucket_name"],
                stage.storage["silver"]["bucket_name"],
                stage.storage["gold"]["bucket_name"],
                trino=_credential(
                    f"trino-{stage.name.value}", f"polaris-{stage.name.value}-trino-creds", "POLARIS_TRINO", shared
                ),
                floe=_stage_credential(stage, "floe"),
                deployer=_stage_credential(stage, "deployer"),
            )
            for stage in stages
        ),
        openmetadata=_credential("openmetadata", "polaris-om-creds", "POLARIS_OM", shared),
        sts=sts,
        person_access=_person_access(contracts, catalogs, shared),
    )


def polaris_service(contracts: ProviderContracts) -> tuple[str, int, str]:
    """Polaris's Service, port and namespace, from the catalog's namespace-qualified REST URI."""
    rest_uri = next(stage.catalog["rest_uri"] for stage in contracts.stages.values())
    parsed = urllib.parse.urlparse(rest_uri)
    service, _, namespace = (parsed.hostname or "").partition(".")
    return service, parsed.port or 8181, namespace


def bootstrap_from_contract(contracts: ProviderContracts, *, environ: Mapping[str, str]) -> None:
    """Bootstrap Polaris as root through a port-forward, from the applied contract. Nothing to do without it."""
    plan = bootstrap_plan(contracts)
    if plan is None:
        return
    service, port, namespace = polaris_service(contracts)
    log.step("Bootstrapping Polaris...")
    log_prefix = environ.get("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge")
    with k8s.port_forward(
        service, port, namespace, log_path=f"{log_prefix}-polaris-bootstrap.log", environ=environ
    ) as local_port:
        root = PolarisConfig(
            f"http://127.0.0.1:{local_port}",
            "",
            client_id=k8s.secret_value(ROOT_CREDENTIALS_SECRET, "ROOT_CLIENT_ID", namespace, environ=environ),
            client_secret=k8s.secret_value(ROOT_CREDENTIALS_SECRET, "ROOT_CLIENT_SECRET", namespace, environ=environ),
        )
        client = PolarisClient(root)
        client.login()
        run_bootstrap(client, plan, environ=environ)
    _roll_trino_onto_its_credentials(plan, environ=environ)
    # Helm does not wait for Trino while it reads a Polaris credential, which exists only from here on
    # (modules/query/trino), so this is where a deploy finds out that Trino does not come up.
    k8s.wait_for_deployments(TRINO_SELECTOR, plan.shared_namespace, environ=environ)


def trino_credentials(plan: BootstrapPlan) -> set[Credential]:
    """The credentials Trino's pods read from their environment, so only at start: each stage's workload
    catalog and, with people's access, each stage's people catalog."""
    return {stage.trino for stage in plan.stages} | set(plan.person_access.people if plan.person_access else ())


def _roll_trino_onto_its_credentials(plan: BootstrapPlan, *, environ: Mapping[str, str]) -> None:
    """Restart Trino when the credentials it reads differ from those its pods started with.

    Trino holds a catalog's credential from its start, so a rotated one fails every query on that catalog until
    the pods restart. The pod template carries a digest of the credential Secrets: a run that changed none leaves
    it, and so Trino, alone, and a run that failed after storing one is followed by a rerun that still restarts.
    """
    digest = hashlib.sha256()
    for credential in sorted(trino_credentials(plan), key=lambda c: (c.namespace, c.secret)):
        data = k8s.secret_data(credential.secret, credential.namespace, environ=environ)
        digest.update(json.dumps([credential.namespace, credential.secret, data], sort_keys=True).encode())
    namespace = plan.shared_namespace
    changed = k8s.annotate_pod_templates(
        TRINO_SELECTOR, namespace, TRINO_CREDENTIALS_ANNOTATION, digest.hexdigest(), environ=environ
    )
    if changed:
        log.step(f"Restarting Trino in '{namespace}': its Polaris credentials changed...")
    for deployment in changed:
        k8s.wait_for_rollout(deployment, namespace, environ=environ)
