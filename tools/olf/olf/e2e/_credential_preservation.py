"""Redeploy preservation drill (#400).

The credentials that moved out of Terraform state are created by in-cluster Jobs, only when missing. The drill
deletes those Jobs so the platform apply has to run their scripts again, then checks that every value is unchanged.
Values are compared, never printed.
"""

from __future__ import annotations

from olf import log, secrets_rotation
from olf.e2e._secret_exposure import migrated_credentials, scan_namespaces
from olf.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise

# The `openlakeforge.io/job` labels of the Jobs that create or adopt those credentials.
CREDENTIAL_JOBS = ("postgresql-credentials", "superset-credentials", "polaris-token-key", "governance-bootstrap")


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
        kubectl(
            cfg,
            ["delete", "job", "-A", "-l", f"openlakeforge.io/job in ({','.join(CREDENTIAL_JOBS)})", "--wait=true"],
        )
        secrets_rotation.apply_platform(cfg.deployment, [])
    except secrets_rotation.RotationError as exc:
        raise E2EError(str(exc)) from exc
    after = migrated_credentials(cfg, namespaces)
    changed = sorted(label for label, value in before.items() if after.get(label) != value)
    if changed:
        raise E2EError("a redeploy changed or lost these credentials:\n" + "\n".join(changed))
    log.info(f"All {len(before)} migrated credentials survived the redeploy.")
