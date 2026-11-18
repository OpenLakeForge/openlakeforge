"""`python -m openlakeforge_platform_api.quality_sync`: one bounded quality publication pass for one stage (#797).

The CronJob of `modules/governance/quality-reconciler` runs this on the platform API image, as the alert evaluator
does, because that image already holds the service clients and boto3. It is the same `QualityPublisher` the operator
command `olf infra openmetadata quality-sync` drives by hand, configured from the pod's environment instead of the
provider contract.

Exit 1 when OpenMetadata or the bucket stopped answering, so the Job's retained failure shows it while the rest stays
pending in the stage's summary; 2 when the environment is wrong. A table that is not registered yet is not a failure
of the run: its results stay pending until it is.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from openlakeforge_core.quality_sync import OmStatus
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.openmetadata import OpenMetadataClient
from openlakeforge_services.quality_publisher import (
    DEFAULT_LIMIT,
    QualityCatalog,
    QualityPublisher,
    ReconcileReport,
    StoreUnavailableError,
    SyncStore,
)

from openlakeforge_platform_api.settings import SettingsError

MAX_LIMIT = 200
_STAGE = re.compile(r"[a-z0-9][a-z0-9-]*")

log = logging.getLogger("openlakeforge_platform_api.quality_sync")


@dataclass(frozen=True)
class ReconcilerSettings:
    """What one stage's reconciler needs. The credentials are not here: the bucket key and the ingestion bot's token
    come from Secret-backed variables, so nothing a log or an error repeats can carry them."""

    stage: str
    # s3://<ops bucket>/activations/<stage>/, the only prefix this pod's key can write.
    artifact_base_uri: str
    openmetadata_url: str
    database_fqn: str
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    limit: int = DEFAULT_LIMIT
    warn_status: OmStatus = OmStatus.ABORTED
    silver_schemas: Mapping[str, str] | None = None
    gold_schemas: Mapping[str, str] | None = None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> ReconcilerSettings:
        stage = environ.get("OPENLAKEFORGE_STAGE", "")
        if not _STAGE.fullmatch(stage):
            raise SettingsError("OPENLAKEFORGE_STAGE must be a plain lowercase stage name")
        base_uri = environ.get("OPENLAKEFORGE_ARTIFACT_BASE_URI", "")
        parts = urlsplit(base_uri)
        # Another stage's prefix is refused here, before any request: the stage in the name and the prefix agree.
        if parts.scheme != "s3" or not parts.netloc or parts.path.strip("/") != f"activations/{stage}":
            raise SettingsError(f"OPENLAKEFORGE_ARTIFACT_BASE_URI must be s3://<ops bucket>/activations/{stage}/")
        url = environ.get("OPENMETADATA_URL", "")
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
            raise SettingsError("OPENMETADATA_URL must be an http(s) base URL")
        database = environ.get("OPENLAKEFORGE_CATALOG_DATABASE_FQN", "")
        if not database.endswith(f".lakehouse_{stage}"):
            raise SettingsError(
                f"OPENLAKEFORGE_CATALOG_DATABASE_FQN must be the lakehouse_{stage} root of stage {stage}"
            )
        try:
            limit = int(environ.get("OPENLAKEFORGE_QUALITY_SYNC_LIMIT") or DEFAULT_LIMIT)
        except ValueError as exc:
            raise SettingsError("OPENLAKEFORGE_QUALITY_SYNC_LIMIT must be a whole number") from exc
        if not 1 <= limit <= MAX_LIMIT:
            raise SettingsError(f"OPENLAKEFORGE_QUALITY_SYNC_LIMIT must be between 1 and {MAX_LIMIT}")
        warn = environ.get("OPENLAKEFORGE_QUALITY_WARN_STATUS") or OmStatus.ABORTED.value
        if warn not in (OmStatus.ABORTED.value, OmStatus.FAILED.value):
            raise SettingsError("OPENLAKEFORGE_QUALITY_WARN_STATUS must be Aborted or Failed: a WARN is never Success")
        return cls(
            stage=stage,
            artifact_base_uri=base_uri,
            openmetadata_url=url.rstrip("/"),
            database_fqn=database,
            s3_endpoint_url=environ.get("OPENLAKEFORGE_S3_ENDPOINT_URL") or None,
            s3_region=environ.get("OPENLAKEFORGE_S3_REGION") or "us-east-1",
            limit=limit,
            warn_status=OmStatus(warn),
            silver_schemas=_schema_map("OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON", environ),
            gold_schemas=_schema_map("OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON", environ),
        )


def _schema_map(name: str, environ: Mapping[str, str]) -> dict[str, str] | None:
    raw = environ.get(name, "")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise SettingsError(f"{name} must map names to schema FQNs")
    return value


def reconcile(settings: ReconcilerSettings, *, store: SyncStore, client: OpenMetadataClient) -> ReconcileReport:
    """One pass over this stage's manifests. The publisher refuses a database root or schema of another stage."""
    try:
        publisher = QualityPublisher(
            store=store,
            base_uri=settings.artifact_base_uri,
            stage=settings.stage,
            catalog=QualityCatalog(client),
            database_fqn=settings.database_fqn,
            warn_status=settings.warn_status,
            silver_schemas=settings.silver_schemas,
            gold_schemas=settings.gold_schemas,
        )
    except ValueError as exc:
        raise SettingsError(str(exc)) from exc
    return publisher.reconcile(limit=settings.limit)


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    del argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env = os.environ if environ is None else environ
    try:
        settings = ReconcilerSettings.from_environ(env)
        store = _object_store(settings, env)
        client = _catalog_client(settings, env)
    except SettingsError as error:
        log.error("quality reconciler is misconfigured: %s", error)
        return 2
    try:
        report = reconcile(settings, store=store, client=client)
    except SettingsError as error:
        log.error("quality reconciler is misconfigured: %s", error)
        return 2
    except StoreUnavailableError as error:
        log.error("stage %s: the ops bucket is unavailable, nothing was recorded: %s", settings.stage, error)
        return 1
    except ServiceClientError as error:
        # The status only: the response text of a service can echo a request.
        log.error("stage %s: OpenMetadata refused a request: HTTP %s", settings.stage, error.status or "no response")
        return 1
    summary = report.summary
    log.info(
        "stage %s: %d runs, %d published, %d waiting for table registration, %d failed, %d skipped; "
        "pending %s, given up %s, lag %s",
        settings.stage,
        report.runs_examined,
        report.published,
        report.deferred,
        report.failed,
        report.skipped,
        "?" if summary is None else summary.pending,
        "?" if summary is None else summary.failed,
        "none" if summary is None or summary.lag_seconds is None else f"{summary.lag_seconds}s",
    )
    if report.interrupted:
        log.error("stage %s: OpenMetadata or the bucket stopped answering; the rest stays pending", settings.stage)
        return 1
    return 0


def _object_store(settings: ReconcilerSettings, environ: Mapping[str, str]) -> SyncStore:
    """The key is passed explicitly: boto3's credential chain falls through to instance metadata, which
    NetworkPolicy drops, so a missing key would hang until that times out."""
    access_key = environ.get("AWS_ACCESS_KEY_ID")
    secret_key = environ.get("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        raise SettingsError("AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set")
    client: SyncStore = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}),
    )
    return client


def _catalog_client(settings: ReconcilerSettings, environ: Mapping[str, str]) -> OpenMetadataClient:
    token = environ.get("OPENMETADATA_TOKEN")
    if not token:
        raise SettingsError("OPENMETADATA_TOKEN must be set, from the ingestion bot's Secret")
    return OpenMetadataClient(base_url=settings.openmetadata_url, token=token)


if __name__ == "__main__":
    sys.exit(main())
