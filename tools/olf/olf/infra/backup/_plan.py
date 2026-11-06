"""What a backup holds, derived from the applied provider contract rather than listed by hand.

The names mirror what the local Terraform root and the bootstrap Jobs create: databases from
`environments/local/main.tf` (`stage_databases`) and `polaris`, Polaris principal Secrets from
`polaris_bootstrap.bootstrap_plan`, the rest from the modules that mint them. A layer a stage has off
(analytics, governance, Dex) contributes nothing, so a Slim deployment is not asked for a Superset database.
"""

from __future__ import annotations

from dataclasses import dataclass

from olf.common.provider_contracts import ProviderContracts
from olf.infra.backup import BackupError
from olf.infra.backup._manifest import SecretRef
from olf.infra.polaris_bootstrap import ROOT_CREDENTIALS_SECRET, bootstrap_plan

LAYERS = ("bronze", "silver", "gold")
# The Polaris metastore. Dumped before every object copy so each pointer in it names files already in the copy.
POLARIS_DATABASE = ("polaris", "polaris")
OPENMETADATA_DATABASE = ("openmetadata_db", "openmetadata_user")
DEX_DATABASE = ("dex", "dex")
LOCAL_CA_SECRET = "local-ca"


@dataclass(frozen=True)
class BackupPlan:
    databases: tuple[tuple[str, str], ...]  # (name, owner)
    buckets: tuple[str, ...]
    # Names only: the keys of each Secret are read from the cluster when it is bundled.
    secrets: tuple[SecretRef, ...]


def plan_from_contract(
    contracts: ProviderContracts, *, shared_namespace: str, github_oauth_secret: str | None = None
) -> BackupPlan:
    """`github_oauth_secret` is the Secret `spec.identity.github.oauth_app_secret_ref` names; it is the
    operator's, created out-of-band, so the contract does not carry it."""
    provider = contracts.deployment.get("provider")
    if provider != "local":
        # The names below are the local root's; a cloud root names its databases and catalog differently, and
        # its managed services back up their own state (#531: cloud restore follows #47).
        raise BackupError(f"olf infra backup supports the local provider only; this deployment is {provider!r}.")
    shared = contracts.shared.values
    stages = sorted(contracts.stages.values(), key=lambda stage: stage.name.value)
    governed = bool(contracts.governed_stages)
    dex = (shared.get("identity") or {}).get("adapter") == "dex"

    databases = [POLARIS_DATABASE]
    databases += [(f"dagster_{stage.name.value}",) * 2 for stage in stages]
    databases += [(f"superset_{name.value}",) * 2 for name in sorted(contracts.analytics_stages, key=lambda n: n.value)]
    databases += [OPENMETADATA_DATABASE] if governed else []
    databases += [DEX_DATABASE] if dex else []

    buckets = [stage.storage[layer]["bucket_name"] for stage in stages for layer in LAYERS]
    buckets.append(shared["ops_storage"]["bucket_name"])

    secrets: list[tuple[str, str]] = []
    polaris = bootstrap_plan(contracts)
    if polaris is not None:
        secrets.append((shared_namespace, ROOT_CREDENTIALS_SECRET))
        credentials = [polaris.openmetadata]
        for stage in polaris.stages:
            credentials += [stage.trino, stage.floe, stage.deployer]
        if polaris.person_access is not None:
            credentials += polaris.person_access.people
        secrets += [(credential.namespace, credential.secret) for credential in credentials]
    secrets += [(stage.namespace, "superset-credentials") for stage in contracts.analytics_stages.values()]
    if governed:
        secrets += [(shared_namespace, name) for name in ("openmetadata-fernet-key", "openmetadata-admin")]
        secrets.append((shared_namespace, "openmetadata-ingestion-bot"))
        if dex:
            secrets.append((shared_namespace, "openmetadata-automation-bot"))
    if github_oauth_secret:
        secrets.append((shared_namespace, github_oauth_secret))
    # cert-manager, and so the local CA, exists for TLS ingress or for Trino's own certificate
    # (`environments/local/main.tf`, module "cert_manager"); a port-forward deployment has neither.
    ingress = (shared.get("access") or {}).get("implementation") == "access.ingress"
    trino_tls = any(stage.query.get("ca_configmap_name") for stage in stages)
    if ingress or trino_tls:
        secrets.append((shared_namespace, LOCAL_CA_SECRET))

    return BackupPlan(
        databases=tuple(databases),
        buckets=tuple(buckets),
        secrets=tuple(SecretRef(namespace, name, ()) for namespace, name in dict.fromkeys(secrets)),
    )
