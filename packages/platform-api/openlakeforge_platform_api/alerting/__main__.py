"""`python -m openlakeforge_platform_api.alerting`: one evaluation, for the CronJob (ADR 0023, decision 1).

Exits 1 when the bucket cannot be read or written, so the Job's retained failures show it, and 2 when the settings or
the environment are wrong. A receiver that is down is not a failure of the run: the notification stays queued.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import boto3
from botocore.config import Config
from openlakeforge_services.dagster import DagsterClient
from openlakeforge_services.notify import Notifier, WebhookNotifier, quiet_transport_logs

from openlakeforge_platform_api.alerting.run import run_once
from openlakeforge_platform_api.alerting.settings import EvaluatorSettings
from openlakeforge_platform_api.alerting.store import AlertObjectStore, AlertStore, StoreError
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    quiet_transport_logs()
    try:
        settings = EvaluatorSettings.from_file(args.settings)
        notifiers = _notifiers(settings)
        client = _object_store(settings)
    except (SettingsError, ValueError, OSError) as error:
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
            now=args.now or datetime.now(UTC),
        )
    except StoreError as error:
        log.error("alert state is unavailable, nothing was delivered: %s", error)
        return 1
    log.info(
        "evaluated: %d new notifications, %d delivered, %d failed, %d expired",
        summary.notifications,
        summary.delivered,
        summary.failed,
        summary.expired,
    )
    return 0


def _notifiers(settings: EvaluatorSettings) -> dict[str, Notifier]:
    notifiers: dict[str, Notifier] = {}
    for receiver in settings.receivers:
        url = os.environ.get(receiver.url_env)
        if not url:
            raise SettingsError(f"receiver {receiver.name!r}: {receiver.url_env} is not set")
        notifiers[receiver.name] = WebhookNotifier(receiver.name, url, ca_file=settings.ca_file)
    return notifiers


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
