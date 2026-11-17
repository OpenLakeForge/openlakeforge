"""The evaluator's read of a stage's alert policy from the revision it runs (ADR 0023, decision 10)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml
from _alerting_fakes import POLICY, FakeBucket, publish
from botocore.exceptions import ClientError
from openlakeforge_core.project_activation import ProjectActivation, activation_key
from openlakeforge_core.project_revision import revision_prefix, sidecar_key

from openlakeforge_platform_api.alerting.policy import read_stage_policy
from openlakeforge_platform_api.alerting.store import AlertStore

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
CACHE = "alerts/state/v1/policy/dev.json"


def read(bucket: FakeBucket, **kwargs: Any) -> Any:
    return read_stage_policy("dev", AlertStore(bucket, "s3://ops/alerts/"), NOW, **kwargs)


def cached(bucket: FakeBucket) -> dict[str, Any]:
    return json.loads(bucket.objects[CACHE])


def test_a_stage_never_activated_has_an_empty_policy_and_no_warning() -> None:
    found = read(FakeBucket())

    assert found.policy.rules == ()
    assert found.resolved.for_job("anything") == {}
    assert found.revision is None and found.unreadable is None


def test_the_policy_of_the_active_revision_is_read_resolved_and_cached() -> None:
    bucket = FakeBucket()
    activation = publish(bucket)

    found = read(bucket)

    assert found.unreadable is None
    assert found.revision == activation.activation_revision
    assert found.policy.routes == {"on-call": "ops"}
    assert found.resolved.for_job("order_revenue_job")["run_failed"].routes == ("on-call",)
    assert cached(bucket)["revision"] == activation.activation_revision


def test_a_revision_without_alerts_yml_has_an_empty_policy_and_no_warning() -> None:
    bucket = FakeBucket()
    activation = publish(bucket, alerts=None)

    found = read(bucket)

    assert found.policy.rules == () and found.unreadable is None
    assert found.revision == activation.activation_revision


def test_the_cached_policy_is_used_without_reading_the_revision_again() -> None:
    bucket = FakeBucket()
    activation = publish(bucket)
    read(bucket)
    for key in [k for k in bucket.objects if k.startswith("project/revisions/")]:
        del bucket.objects[key]

    found = read(bucket)

    assert found.unreadable is None and found.revision == activation.activation_revision
    assert found.policy.routes == {"on-call": "ops"}


def test_a_new_activation_replaces_the_cache() -> None:
    bucket = FakeBucket()
    publish(bucket)
    read(bucket)
    second = publish(bucket, alerts=POLICY.replace("run_failed", "missed_run"), seed="b")

    found = read(bucket)

    assert found.revision == second.activation_revision
    assert "missed_run" in found.resolved.for_job("order_revenue_job")
    assert cached(bucket)["revision"] == second.activation_revision


def corrupt_alerts(bucket: FakeBucket, activation: ProjectActivation) -> None:
    key = f"{revision_prefix(activation.project_revision)}/descriptors/lakehouse_code/alerts.yml"
    bucket.objects[key] = POLICY.replace("run_failed", "missed_run").encode()


def test_a_digest_mismatch_keeps_the_last_good_policy_and_says_why() -> None:
    bucket = FakeBucket()
    publish(bucket)
    good = read(bucket)
    activation = publish(bucket, seed="b")
    corrupt_alerts(bucket, activation)

    found = read(bucket)

    assert found.policy == good.policy
    assert found.revision == good.revision
    assert found.unreadable is not None and "digest" in found.unreadable
    assert cached(bucket)["revision"] == good.revision


def test_an_invalid_policy_keeps_the_last_good_policy() -> None:
    bucket = FakeBucket()
    publish(bucket)
    good = read(bucket)
    publish(bucket, alerts=POLICY.replace("kind: AlertPolicy", "kind: Other"), seed="b")

    found = read(bucket)

    assert found.policy == good.policy and found.revision == good.revision
    assert found.unreadable is not None and "not valid" in found.unreadable


def test_a_policy_naming_an_unknown_product_is_unusable() -> None:
    bucket = FakeBucket()
    publish(bucket, alerts=POLICY.replace('product: "*"', "product: ghost").replace('domain: "*"', "domain: sales"))

    found = read(bucket)

    assert found.unreadable is not None and found.revision is None


def test_with_no_last_good_policy_an_unreadable_one_is_empty_and_warns() -> None:
    bucket = FakeBucket()
    activation = publish(bucket)
    corrupt_alerts(bucket, activation)

    found = read(bucket)

    assert found.policy.rules == ()
    assert found.revision is None
    assert found.unreadable is not None
    assert CACHE not in bucket.objects


def test_a_missing_sidecar_is_unreadable() -> None:
    bucket = FakeBucket()
    activation = publish(bucket)
    del bucket.objects[sidecar_key(activation.project_revision)]

    found = read(bucket)

    assert found.unreadable is not None and "sidecar" in found.unreadable


def test_a_pointer_to_a_missing_activation_is_unreadable() -> None:
    bucket = FakeBucket()
    activation = publish(bucket)
    del bucket.objects[activation_key(activation.activation_revision, stage="dev")]

    assert read(bucket).unreadable is not None


class _RevisionsDown(FakeBucket):
    """Answers the alert objects but not the activations: a grant that was withdrawn, or a partial outage."""

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key.startswith(("activations/", "project/")):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "GetObject")
        return super().get_object(Bucket=Bucket, Key=Key)


def test_revisions_that_cannot_be_read_keep_the_cached_policy_and_warn() -> None:
    bucket = _RevisionsDown()
    publish(bucket)
    bucket.objects[CACHE] = json.dumps(
        {
            "schema_version": 1,
            "revision": "sha256:" + "1" * 64,
            "alerts": yaml.safe_load(POLICY),
            "governance": {"domains": [{"name": "sales", "products": [{"id": "order_revenue"}]}]},
        }
    ).encode()

    found = read(bucket)

    assert found.unreadable is not None and "AccessDenied" in found.unreadable
    assert found.policy.routes == {"on-call": "ops"}
    assert found.revision == "sha256:" + "1" * 64


def test_a_corrupt_cache_is_ignored_and_rewritten() -> None:
    bucket = FakeBucket()
    activation = publish(bucket)
    bucket.objects[CACHE] = b"{not json"

    found = read(bucket)

    assert found.unreadable is None
    assert cached(bucket)["revision"] == activation.activation_revision


def test_a_failed_cache_write_does_not_hide_a_good_policy() -> None:
    bucket = FakeBucket()
    bucket.failing_writes = ("alerts/",)
    publish(bucket)

    found = read(bucket)

    assert found.unreadable is None and found.policy.routes == {"on-call": "ops"}


@pytest.mark.parametrize("from_cache", [False, True])
def test_a_route_to_a_receiver_the_profile_dropped_is_reported_but_still_used(from_cache: bool) -> None:
    bucket = FakeBucket()
    publish(bucket)
    if from_cache:
        read(bucket, receivers=["ops"])

    found = read(bucket, receivers=["pager"])

    assert found.unreadable is not None and "ops" in found.unreadable
    assert found.policy.routes == {"on-call": "ops"}


def test_every_declared_receiver_is_no_warning() -> None:
    bucket = FakeBucket()
    publish(bucket)

    assert read(bucket, receivers=["ops", "pager"]).unreadable is None


def test_stages_read_their_own_pointer() -> None:
    bucket = FakeBucket()
    publish(bucket, stage="prod")

    assert read(bucket).revision is None
    assert read_stage_policy("prod", AlertStore(bucket, "s3://ops/alerts/"), NOW).revision is not None
