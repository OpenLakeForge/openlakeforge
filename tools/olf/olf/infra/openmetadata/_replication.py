"""The ingestion-bot Secret's copies in the stage namespaces, kept by the platform phase (#562).

A Secret cannot be read across namespaces, so each governed stage's Dagster needs its own copy of the
one the bootstrap keeps in the shared namespace. Ported from the governance module's former
`replicate-secrets.sh.tftpl` Job.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence

from olf.common import log
from olf.common.provider_contracts import ProviderContracts
from olf.infra import k8s
from olf.infra.openmetadata._auth import INGESTION_BOT_SECRET


def replication_namespaces(contracts: ProviderContracts) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The governed stages' namespaces, and the other enabled stages' ones, as the roots'
    `workload_namespaces` and `revoked_namespaces` compute them."""
    governed = tuple(stage.namespace for stage in contracts.governed_stages.values())
    revoked = tuple(stage.namespace for stage in contracts.stages.values() if stage.governance is None)
    return governed, revoked


def replicate_ingestion_bot(
    shared_namespace: str,
    governed: Sequence[str],
    revoked: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Copy the Secret, exactly its keys and nothing else, into `governed`; delete it from `revoked`.

    A namespace equal to the shared one is skipped either way: the single-namespace cloud roots
    run every stage there, and revoking would delete the source.
    """
    source = k8s.secret_data(INGESTION_BOT_SECRET, shared_namespace, environ=environ)
    if source is None:
        raise k8s.KubectlError(f"Source Secret '{INGESTION_BOT_SECRET}' is missing in '{shared_namespace}'.")
    for target in governed:
        if target == shared_namespace or k8s.secret_data(INGESTION_BOT_SECRET, target, environ=environ) == source:
            continue
        # Replaced rather than applied: an apply keeps a key the source no longer has, and the
        # Dagster pods mount every key as an environment variable.
        k8s.delete_secret(INGESTION_BOT_SECRET, target, environ=environ)
        values = {key: base64.b64decode(value).decode("utf-8") for key, value in source.items()}
        k8s.write_secret(INGESTION_BOT_SECRET, target, values, overwrite=False, environ=environ)
        # The name is spelled out: CodeQL treats any value named like a secret as one, and this is only a name.
        log.info(f"Replicated Secret 'openmetadata-ingestion-bot' into '{target}'.")
    # A stage that turns governance off keeps its namespace, and the copy there stays a valid
    # credential until the bootstrap next rotates the token, which a topology change does not do.
    for namespace in revoked:
        if namespace == shared_namespace:
            continue
        if k8s.secret_data(INGESTION_BOT_SECRET, namespace, environ=environ) is not None:
            k8s.delete_secret(INGESTION_BOT_SECRET, namespace, environ=environ)
            log.info(f"Revoked Secret 'openmetadata-ingestion-bot' from '{namespace}'.")
