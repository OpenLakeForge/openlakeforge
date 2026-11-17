"""`python -m openlakeforge_platform_api.alerting`: one evaluation, for the CronJob (ADR 0023, decision 1).

Exits 1 when the bucket cannot be read or written, so the Job's retained failures show it, and 2 when the settings or
the environment are wrong. A receiver that is down is not a failure of the run: the notification stays queued.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime

import boto3
import certifi
from botocore.config import Config
from openlakeforge_core.alert_policy import load_alert_policy
from openlakeforge_core.descriptors import LakehouseDescriptorError
from openlakeforge_services.dagster import DagsterClient
from openlakeforge_services.notify import Notifier, WebhookNotifier, quiet_transport_logs

from openlakeforge_platform_api.alerting.run import run_once
from openlakeforge_platform_api.alerting.service_health import StageHealthReader, stage_health_reader
from openlakeforge_platform_api.alerting.settings import EvaluatorSettings
from openlakeforge_platform_api.alerting.store import AlertObjectStore, AlertStore, StoreError
from openlakeforge_platform_api.quality_health import QualityReader, s3_quality_reader
from openlakeforge_platform_api.settings import SettingsError

SETTINGS_ENV = "OPENLAKEFORGE_ALERTS_SETTINGS"
DEFAULT_SETTINGS_PATH = "/config/alerts.json"
ACCESS_KEY_ENV = "AWS_ACCESS_KEY_ID"
SECRET_KEY_ENV = "AWS_SECRET_ACCESS_KEY"

log = logging.getLogger("openlakeforge_platform_api.alerting")


def _aware(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError("--now needs a timezone, e.g. 2026-11-16T12:00:00+00:00")
    return moment


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m openlakeforge_platform_api.alerting")
    parser.add_argument("--settings", default=os.environ.get(SETTINGS_ENV, DEFAULT_SETTINGS_PATH))
    # The two flags below are for the e2e only (ADR 0023, decision 3); the CronJob never passes them.
    parser.add_argument("--now", type=_aware, help="fix the evaluation clock")
    parser.add_argument("--state-prefix", help="keep state and history under this prefix of the ops bucket")
    parser.add_argument("--policy-file", help="evaluate every stage by this alerts.yml instead of its revision's")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    quiet_transport_logs()
    try:
        override = load_alert_policy(args.policy_file) if args.policy_file else None
        settings = EvaluatorSettings.from_file(args.settings)
        notifiers = _notifiers(settings)
        client = _object_store(settings)
        quality = _quality_readers(settings)
    except (SettingsError, ValueError, OSError, LakehouseDescriptorError) as error:
        log.error("alert evaluator is misconfigured: %s", error)
        return 2

    store = AlertStore(client, settings.state_base_uri, prefix=args.state_prefix)
    dagster = {stage: DagsterClient(url) for stage, url in settings.dagster_graphql_urls.items()}
    try:
        summary = run_once(
            settings=settings,
            store=store,
            dagster=dagster,
            notifiers=notifiers,
            quality=quality,
            policy_override=override,
            health=_health_reader(settings),
            now=args.now or datetime.now(UTC),
        )
    except StoreError as error:
        log.error("alert state is unavailable, nothing was delivered: %s", error)
        return 1
    for stage in summary.policy_unreadable:
        log.warning("alert policy of stage %s was not usable this run; see the warning above", stage)
    log.info(
        "evaluated: %d new notifications, %d delivered, %d failed, %d expired",
        summary.notifications,
        summary.delivered,
        summary.failed,
        summary.expired,
    )
    return 0


def _health_reader(settings: EvaluatorSettings) -> StageHealthReader | None:
    """Without the API's URL no service condition is read: no incident opens for one and none is resolved."""
    if settings.platform_api_url is None or settings.platform_api_token_file is None:
        return None
    return stage_health_reader(settings.platform_api_url, settings.platform_api_token_file)


def combined_ca_bundle(local_ca: str, directory: str | None = None) -> str:
    """A bundle of the public roots plus the cluster's CA, written to a fresh file; returns its path.

    `requests` replaces its trust store with `verify=<file>` instead of adding to it, so passing the cluster CA
    alone would make every public webhook (Slack, ...) fail TLS. The CA mount is read-only, hence the temp dir.
    """
    with open(certifi.where(), encoding="ascii") as public, open(local_ca, encoding="ascii") as local:
        bundle = public.read().rstrip("\n") + "\n" + local.read().rstrip("\n") + "\n"
    handle, path = tempfile.mkstemp(prefix="ca-bundle-", suffix=".pem", dir=directory)
    with os.fdopen(handle, "w", encoding="ascii") as out:
        out.write(bundle)
    return path


def _notifiers(settings: EvaluatorSettings) -> dict[str, Notifier]:
    ca_file = combined_ca_bundle(settings.ca_file) if settings.ca_file else None
    notifiers: dict[str, Notifier] = {}
    for receiver in settings.receivers:
        url = os.environ.get(receiver.url_env)
        if not url:
            raise SettingsError(f"receiver {receiver.name!r}: {receiver.url_env} is not set")
        notifiers[receiver.name] = WebhookNotifier(receiver.name, url, ca_file=ca_file)
    return notifiers


def _quality_readers(settings: EvaluatorSettings) -> dict[str, QualityReader]:
    """A reader per stage with a manifest prefix, all on the evaluator's own key: it holds Read and List on every
    stage's quality prefix (ADR 0023, decision 3), so no per-stage key is mounted here."""
    if not settings.quality_manifests:
        return {}
    access_key = os.environ.get(ACCESS_KEY_ENV)
    secret_key = os.environ.get(SECRET_KEY_ENV)
    if not access_key or not secret_key:
        raise SettingsError(f"{ACCESS_KEY_ENV} and {SECRET_KEY_ENV} must be set")
    return {
        stage: s3_quality_reader(
            base_uri,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            access_key=access_key,
            secret_key=secret_key,
        )
        for stage, base_uri in settings.quality_manifests.items()
    }


def _object_store(settings: EvaluatorSettings) -> AlertObjectStore:
    """The key is passed explicitly: boto3's credential chain falls through to instance metadata, which
    NetworkPolicy drops, so a missing key would hang until that times out."""
    access_key = os.environ.get(ACCESS_KEY_ENV)
    secret_key = os.environ.get(SECRET_KEY_ENV)
    if not access_key or not secret_key:
        raise SettingsError(f"{ACCESS_KEY_ENV} and {SECRET_KEY_ENV} must be set")
    client: AlertObjectStore = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}),
    )
    return client


if __name__ == "__main__":
    sys.exit(main())
