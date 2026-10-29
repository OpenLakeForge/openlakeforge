"""Redeploy preservation drill (#400).

The credentials that moved out of Terraform state are created by in-cluster Jobs, only when missing. The drill
deletes those Jobs so the platform apply has to run their scripts again, then checks that every value is unchanged.
Values are compared, never printed.
"""

from __future__ import annotations

import json

from olf import log, secrets_rotation
from olf.e2e._secret_exposure import migrated_credentials, scan_namespaces
from olf.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise

# The Jobs that create or adopt those credentials, by name: their `openlakeforge.io/job` label is on the Pod
# template, not on the Job, so a label selector would match none. Each name ends in a hash or a Helm revision.
CREDENTIAL_JOB_PREFIXES = (
    "postgresql-credentials-",
    "superset-credentials-",
    "polaris-token-key-",
    "openmetadata-bootstrap-",
    # Re-running the bootstrap Job rotates the ingestion-bot token; the replication Job copies it to the stages.
    "openmetadata-credential-replication-",
)


def check_credential_preservation(cfg: E2EConfig) -> None:
    if cfg.deployment is None:
        log.info("Skipping the credential preservation drill: it needs a resolved deployment.")
        return
    namespaces = scan_namespaces(cfg, load_provider_contracts_or_raise(cfg))
    before = migrated_credentials(cfg, namespaces)
    if not before:
        raise E2EError("no migrated credential found in the cluster, so there is nothing to preserve")
    log.step(f"Re-running the credential Jobs with a platform apply; {len(before)} credentials must not change...")
    try:
        # Before anything is deleted, as the rotation drill does.
        secrets_rotation.check_platform_state(cfg.deployment)
        old = _credential_jobs(cfg)
        if not old:
            raise E2EError("found none of the credential Jobs, so a platform apply would re-run nothing")
        for namespace, name in old:
            kubectl(cfg, ["delete", "job", name, "-n", namespace, "--wait=true"])
        secrets_rotation.apply_platform(cfg.deployment, [])
        # Terraform recreates a Job that is gone, under the same name and a new uid.
        recreated = _credential_jobs(cfg)
        missing = sorted("/".join(key) for key, uid in old.items() if recreated.get(key) in (None, uid))
        if missing:
            raise E2EError("the platform apply did not recreate these Jobs:\n" + "\n".join(missing))
    except secrets_rotation.RotationError as exc:
        raise E2EError(str(exc)) from exc
    after = migrated_credentials(cfg, namespaces)
    changed = sorted(label for label, value in before.items() if after.get(label) != value)
    if changed:
        raise E2EError("a redeploy changed or lost these credentials:\n" + "\n".join(changed))
    log.info(f"All {len(before)} migrated credentials survived the redeploy.")


def _credential_jobs(cfg: E2EConfig) -> dict[tuple[str, str], str]:
    """(namespace, name) -> uid of every credential Job in the cluster."""
    items = json.loads(kubectl(cfg, ["get", "jobs", "-A", "-o", "json"], capture=True))["items"]
    return {
        (item["metadata"]["namespace"], item["metadata"]["name"]): item["metadata"]["uid"]
        for item in items
        if item["metadata"]["name"].startswith(CREDENTIAL_JOB_PREFIXES)
    }
