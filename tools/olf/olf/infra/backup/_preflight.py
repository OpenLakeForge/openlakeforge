"""What `olf infra restore` (#911) checks before it changes anything: the backup directory and the platform.

A backup is restored onto a platform of the same release and profile that is already deployed. Secrets are never
pre-seeded before the platform's first deploy: a new Polaris principal deletes a Secret that is already there
(`polaris_bootstrap.ensure_credential`), so a cluster that is not deployed is refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from olf.infra import k8s
from olf.infra.backup import BackupError
from olf.infra.backup._bundle import decrypt_secrets
from olf.infra.backup._manifest import FORMAT, Manifest, load_manifest, sha256_file, verify_checksums


def is_workload(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}-") for prefix in prefixes)


def preflight(
    directory: Path,
    *,
    passphrase: str,
    release: str,
    profile: Path,
    stage_namespaces: Mapping[str, str],
    shared_namespace: str,
    environ: Mapping[str, str] | None,
) -> tuple[Manifest, list[dict[str, Any]]]:
    """Check the backup and the platform without changing either; return the manifest and the decrypted Secrets.

    Raises `BackupError` when the checksums, release, profile, stages, passphrase or Secrets do not match, or when
    the platform is not deployed.
    """
    if problems := verify_checksums(directory):
        raise BackupError("the backup does not match its checksums:\n  " + "\n  ".join(problems))
    manifest = load_manifest(directory)
    _check_compatible(manifest, release=release, profile=profile, stages=tuple(stage_namespaces))
    secrets = _bundled_secrets(directory, manifest, passphrase, {shared_namespace, *stage_namespaces.values()})
    _require_platform(shared_namespace, environ)
    return manifest, secrets


def _check_compatible(manifest: Manifest, *, release: str, profile: Path, stages: tuple[str, ...]) -> None:
    if manifest.format != FORMAT:
        raise BackupError(f"backup format {manifest.format} is not the format {FORMAT} this olf restores")
    if manifest.provider != "local":
        raise BackupError(f"only the local provider is restored; this backup is from {manifest.provider!r}")
    if manifest.release != release:
        raise BackupError(
            f"this backup is from release {manifest.release}, and olf is {release}. Restore on the release that "
            "took it, then upgrade."
        )
    if not profile.is_file():
        raise BackupError(f"{profile} not found: restore needs the Deployment Profile the backup was taken with")
    if manifest.profile_sha256 != sha256_file(profile):
        raise BackupError(f"{profile} differs from the Deployment Profile this backup was taken with")
    if tuple(sorted(manifest.stages)) != tuple(sorted(stages)):
        raise BackupError(f"the backup holds stages {sorted(manifest.stages)}; the deployment has {sorted(stages)}")


def _bundled_secrets(
    directory: Path, manifest: Manifest, passphrase: str, namespaces: set[str]
) -> list[dict[str, Any]]:
    secrets = decrypt_secrets((directory / "secrets.enc").read_bytes(), passphrase)
    bundled = {(secret["namespace"], secret["name"]) for secret in secrets}
    if bundled != {(ref.namespace, ref.name) for ref in manifest.secrets}:
        raise BackupError("secrets.enc does not hold the Secrets manifest.json lists")
    if stray := sorted({ref.namespace for ref in manifest.secrets} - namespaces):
        raise BackupError(f"the backup holds Secrets for namespaces this deployment does not own: {stray}")
    return secrets


def _require_platform(shared_namespace: str, environ: Mapping[str, str] | None) -> None:
    deployed = k8s.resource_exists("statefulset", "postgresql", shared_namespace, environ=environ) and any(
        is_workload(name, ("polaris",)) for name in k8s.deployment_replica_counts(shared_namespace, environ=environ)
    )
    if not deployed:
        raise BackupError(
            f"no deployed platform in {shared_namespace}. Run `olf infra deploy --phase platform` on the release "
            "and profile of the backup first; restoring Secrets before that deploy makes it replace them."
        )
