"""Facts about the v1 pod-log archive that its writer and its readers must agree on (ADR 0022)."""

from __future__ import annotations

# How far back each run of the polling archiver (`openlakeforge_runtime/k8s_log_archive.py`) reads every pod's log.
# A v1 object is partitioned by when it was archived, not by when its lines were written, so a line written at `t`
# can sit in any partition up to this long after `t`; a reader of an event-time window lists that much further on.
V1_LOOKBACK_SECONDS = 3600
