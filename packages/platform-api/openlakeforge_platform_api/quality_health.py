"""Quality signal of a pipeline's latest run, read from the run's quality manifest in the ops bucket (ADR 0023).

The manifest is the only input. One signal per pipeline, scoped like its other signals, so the health routes and the
alert evaluator join it to them: the worst outcome in the manifest wins, and a missing, unreadable, stale,
incomplete or partly unreported manifest is `unknown`, never `healthy`. Table identity is not used: a manifest
written from the baked dbt manifest names no table (#1166), and a product-level signal reads the same either way.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, NamedTuple, Protocol, cast
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from openlakeforge_core.health import (
    HealthState,
    QualityObservation,
    Scope,
    Signal,
    evaluate_quality,
    worst_state,
)
from openlakeforge_core.quality import (
    QualityManifest,
    QualityOutcome,
    QualityRecordError,
    health_quality_fields,
    manifest_key,
)

log = logging.getLogger(__name__)

# `ProductDefinitionSpec.job_name` and `SILVER_DOMAIN_TAG` in packages/runtime/openlakeforge_runtime/product_dagster.py:
# the only jobs that write a quality manifest are product pipelines, and they name their domain on every run. Any
# other job (maintenance) has none, so it gets no quality signal rather than a permanent `unknown`.
PIPELINE_JOB_SUFFIX = "_pipeline"
DOMAIN_TAG = "openlakeforge/silver-domain"

_MISSING = frozenset({"NoSuchKey", "404"})


class ManifestStore(Protocol):
    """The one call the reader makes; boto3's S3 client satisfies it."""

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]: ...  # noqa: N803 - boto3's names


def s3_quality_reader(
    base_uri: str, *, endpoint_url: str | None, region: str, access_key: str, secret_key: str
) -> QualityReader:
    """A reader over its own S3 client. The key is passed explicitly: boto3's credential chain falls through to
    instance metadata, which NetworkPolicy drops, so a missing key would hang until that times out."""
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}),
    )
    return QualityReader(cast("ManifestStore", client), base_uri)


class QualityReadError(Exception):
    """The manifest could not be read or parsed, as opposed to not being there."""


class QualityRead(NamedTuple):
    signal: Signal
    # The manifest exists but could not be used (read error, malformed, foreign), as opposed to being absent.
    unreadable: bool


class QualityReader:
    """Reads one stage's manifests with the stage's own read-only key; `base_uri` is its activation prefix."""

    def __init__(self, client: ManifestStore, base_uri: str) -> None:
        parts = urlsplit(base_uri)
        if parts.scheme != "s3" or not parts.netloc:
            raise ValueError(f"expected an s3:// activation prefix, got {base_uri!r}")
        self._client = client
        self._bucket = parts.netloc
        self._prefix = parts.path.strip("/")

    def read(self, stage: str, domain: str, product: str, run_id: str) -> QualityManifest | None:
        """The run's manifest, or None when the run wrote none.

        Domain, product and run id become key segments, and the run's tags are not trusted: `manifest_key` refuses
        a segment that is not a plain name.
        """
        try:
            key = "/".join(part for part in (self._prefix, manifest_key(domain, product, run_id)) if part)
        except QualityRecordError as error:
            raise QualityReadError(str(error)) from error
        try:
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in _MISSING:
                return None
            raise QualityReadError(f"{key} could not be read: {error}") from error
        except BotoCoreError as error:
            raise QualityReadError(f"{key} could not be read: {error}") from error
        try:
            manifest = QualityManifest.from_json(body.decode())
        except (QualityRecordError, UnicodeDecodeError) as error:
            raise QualityReadError(f"{key} is not a quality manifest: {error}") from error
        # The key is what was asked for; a manifest that says it belongs elsewhere is not evidence about this run.
        if (manifest.stage, manifest.domain, manifest.product, manifest.dagster_run_id) != (
            stage,
            domain,
            product,
            run_id,
        ):
            raise QualityReadError(f"{key} describes another stage, product or run")
        return manifest


def quality_signal(
    reader: QualityReader,
    scope: Scope,
    job: str,
    run_id: str,
    tags: Mapping[str, str],
    *,
    now: datetime,
    max_age: timedelta,
    level: str = "warn",
) -> QualityRead | None:
    """The quality signal of `job`'s run, or None for a job that is not a product pipeline.

    `level` is the alert policy's quality level: with `error`, a WARN reads as a pass, so it neither alerts nor hides a
    failure in the same manifest or in an older run."""
    domain = tags.get(DOMAIN_TAG)
    if domain is None or not job.endswith(PIPELINE_JOB_SUFFIX):
        return None
    product = job.removesuffix(PIPELINE_JOB_SUFFIX)
    unreadable = False
    try:
        manifest = reader.read(scope.stage, domain, product, run_id)
    except QualityReadError as error:
        unreadable = True
        # The store's message can name keys and endpoints: the log has it, the answer does not.
        log.warning("quality manifest of %s run %s in stage %s was not usable: %s", job, run_id, scope.stage, error)
        manifest = None
    return QualityRead(_summarize(scope, manifest, now=now, max_age=max_age, level=level), unreadable)


def _summarize(
    scope: Scope, manifest: QualityManifest | None, *, now: datetime, max_age: timedelta, level: str = "warn"
) -> Signal:
    signals: list[Signal] = []
    unreported = False
    for result in manifest.results if manifest else ():
        fields = health_quality_fields(result)
        if fields is None:
            unreported = True
            continue
        observation = QualityObservation(
            outcome=fields.outcome,
            observed_at=fields.observed_at,
            blocking=fields.blocking,
            run=fields.run,
            evidence_uri=fields.evidence_uri,
        )
        signal = evaluate_quality(scope, observation, now=now, max_age=max_age)
        if level == "error" and signal.outcome is QualityOutcome.WARN:
            # Still subject to the rules below: a pass in an incomplete manifest proves nothing.
            signal = replace(signal, state=HealthState.HEALTHY, reason="quality checks passed")
        signals.append(signal)
    if signals:
        worst = worst_state(signal.state for signal in signals)
        # A check that never reported cannot be averaged into a pass, nor can a manifest the run did not finish
        # writing (dbt's results are added last); a failure still stands.
        if not (worst is HealthState.HEALTHY and (unreported or not (manifest and manifest.complete))):
            # A WARN and a non-blocking ERROR are both warnings: the ERROR speaks, so a policy that ignores WARNs does
            # not depend on the order of the manifest's checks.
            candidates = [signal for signal in signals if signal.state is worst]
            return next((s for s in candidates if s.outcome is QualityOutcome.ERROR), candidates[0])
    return evaluate_quality(scope, None, now=now, max_age=max_age)
