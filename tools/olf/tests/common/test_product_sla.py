"""The product `sla` block: offline validation, the dependency warning and what a deploy publishes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from conftest import write_two_product_fixture
from fake_openmetadata import FakeOpenMetadata
from openlakeforge_core import (
    LakehouseDescriptorError,
    duration_minutes,
    load_governance_descriptor,
    load_lakehouse_inventory,
    parse_sla,
)
from openlakeforge_services.governance import freshness_policy
from test_business_metadata import GOVERNANCE as BUSINESS_GOVERNANCE
from test_business_metadata import _deploy, _fake
from test_product_dependencies import _model, _schema

from olf.common import contracts_check

REPO_ROOT = Path(__file__).resolve().parents[4]
SLA: dict[str, Any] = {
    "tier": "GOLD",
    "freshness": {"maximumAge": "PT4H", "readyBy": "09:00", "timezone": "Europe/Paris"},
    "availability": {"schedule": "0 7 * * *", "gracePeriod": "PT30M"},
    "minimumQualityScore": 95,
}
GOVERNANCE = BUSINESS_GOVERNANCE + (
    "        sla:\n"
    "          tier: GOLD\n"
    "          freshness: {maximumAge: PT26H30M, readyBy: '09:00', timezone: Europe/Paris}\n"
    "          availability: {schedule: '0 7 * * *', gracePeriod: PT30M}\n"
    "          minimumQualityScore: 95\n"
)


def _with(**changes: Any) -> dict[str, Any]:
    sla = json.loads(json.dumps(SLA))
    for path, value in changes.items():
        *parents, leaf = path.split("__")
        target = sla
        for key in parents:
            target = target[key]
        if value is None:
            del target[leaf]
        else:
            target[leaf] = value
    return sla


def test_a_complete_sla_parses() -> None:
    sla = parse_sla(SLA, where="governance.yml: product 'order_revenue'")

    assert (sla.tier, sla.maximum_age_minutes, sla.ready_by, sla.timezone) == ("GOLD", 240, "09:00", "Europe/Paris")
    assert (sla.schedule, sla.grace_period, sla.minimum_quality_score) == ("0 7 * * *", "PT30M", 95)


def test_a_tier_alone_is_a_valid_sla() -> None:
    assert parse_sla({"tier": "CUSTOM"}, where="p").maximum_age is None


@pytest.mark.parametrize(
    ("sla", "message"),
    [
        (_with(tier="Gold"), r"p\.sla\.tier: must be one of"),
        (_with(tier=None), r"p\.sla\.tier"),
        (_with(freshness__maximumAge="4 hours"), r"p\.sla\.freshness\.maximumAge: must be a positive ISO 8601"),
        (_with(freshness__maximumAge="P1M"), r"p\.sla\.freshness\.maximumAge"),
        (_with(freshness__maximumAge="PT"), r"p\.sla\.freshness\.maximumAge"),
        (_with(freshness__maximumAge="PT0H"), r"p\.sla\.freshness\.maximumAge"),
        (_with(freshness__maximumAge=None), r"p\.sla\.freshness\.maximumAge: is required"),
        (_with(freshness__readyBy="9am"), r"p\.sla\.freshness\.readyBy: must be a 24-hour HH:MM"),
        (_with(freshness__readyBy="24:00"), r"p\.sla\.freshness\.readyBy"),
        (_with(freshness__timezone="Europe/Pariss"), r"p\.sla\.freshness\.timezone: must be an IANA timezone"),
        (_with(freshness__timezone=None), r"p\.sla\.freshness\.timezone: is required with readyBy"),
        (_with(availability__schedule="every day"), r"p\.sla\.availability\.schedule: not a valid 5-field cron"),
        (_with(availability__schedule="0 25 * * *"), r"p\.sla\.availability\.schedule.*hour"),
        (_with(availability__schedule="0 7 * *"), r"p\.sla\.availability\.schedule.*got 4"),
        (_with(availability__schedule="*/0 7 * * *"), r"p\.sla\.availability\.schedule.*step"),
        (_with(availability__gracePeriod="soon"), r"p\.sla\.availability\.gracePeriod"),
        (_with(availability__schedule=None), r"p\.sla\.availability\.schedule: is required"),
        (_with(minimumQualityScore=101), r"p\.sla\.minimumQualityScore: must be a number from 0 to 100"),
        (_with(minimumQualityScore=True), r"p\.sla\.minimumQualityScore"),
        ({**SLA, "responseTime": 200}, r"p\.sla: must not contain \['responseTime'\]"),
        ({}, r"p\.sla: must be a non-empty object"),
    ],
)
def test_an_invalid_sla_is_refused_with_the_path_of_the_bad_value(sla: dict[str, Any], message: str) -> None:
    with pytest.raises(LakehouseDescriptorError, match=message):
        parse_sla(sla, where="p")


@pytest.mark.parametrize("schedule", ["0 7 * * *", "*/15 0-6 * jan-mar mon-fri", "30 1 1,15 * 0", "0 0 * * 7"])
def test_common_cron_expressions_are_accepted(schedule: str) -> None:
    assert parse_sla({"tier": "BRONZE", "availability": {"schedule": schedule}}, where="p").schedule == schedule


def test_durations_round_up_to_whole_minutes() -> None:
    assert [duration_minutes(v) for v in ("PT26H", "P1DT2H", "PT90S", "PT1S", "P1W")] == [1560, 1560, 2, 1, 10080]


def test_the_json_schema_agrees_with_the_loader_on_the_shape() -> None:
    schema = json.loads((REPO_ROOT / "docs/schema/governance.schema.json").read_text(encoding="utf-8"))
    document = yaml.safe_load(BUSINESS_GOVERNANCE)
    document["domains"][0]["products"][0]["sla"] = SLA
    jsonschema.validate(document, schema)

    for bad in (_with(tier="Gold"), {**SLA, "extra": 1}, _with(freshness__timezone=None)):
        document["domains"][0]["products"][0]["sla"] = bad
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, schema)


def test_the_loader_refuses_an_invalid_sla_in_governance_yml(tmp_path: Path) -> None:
    path = tmp_path / "governance.yml"
    document = yaml.safe_load(BUSINESS_GOVERNANCE)
    document["domains"][0]["products"][0]["sla"] = _with(freshness__maximumAge="soon")
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(LakehouseDescriptorError, match=r"products\[\]|order_revenue.*sla\.freshness\.maximumAge"):
        load_governance_descriptor(path)


def test_the_inventory_carries_the_sla(tmp_path: Path) -> None:
    write_two_product_fixture(tmp_path)
    path = tmp_path / "lakehouse_code" / "governance.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["domains"][0]["products"][0]["sla"] = {"tier": "SILVER", "freshness": {"maximumAge": "PT26H"}}
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    products = {p.id: p for p in load_lakehouse_inventory(tmp_path).products}

    assert products["widgets_alpha"].sla is not None
    assert products["widgets_alpha"].sla.maximum_age_minutes == 1560
    assert products["widgets_beta"].sla is None


def _consumer_project(root: Path, producer_age: str, consumer_age: str) -> None:
    write_two_product_fixture(root)
    _model(root, "widgets_alpha", "dim_widget", "select 1 as widget_id from {{ source('widgets', 'source') }}")
    _model(root, "widgets_beta", "mart_widgets", "select * from {{ ref('dim_widget') }}")
    _schema(root, "widgets_alpha", dim_widget="public")
    path = root / "lakehouse_code" / "governance.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for product, age in zip(document["domains"][0]["products"], (producer_age, consumer_age), strict=True):
        product["sla"] = {"tier": "SILVER", "freshness": {"maximumAge": age}}
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def test_contracts_check_warns_when_a_consumer_promises_fresher_data_than_its_producer(tmp_path: Path) -> None:
    _consumer_project(tmp_path, producer_age="PT26H", consumer_age="PT4H")

    result = contracts_check._check_product_dependencies(tmp_path)

    assert result.ok
    assert len(result.warnings) == 1
    assert "widgets_beta" in result.warnings[0]
    assert "PT4H" in result.warnings[0]
    assert "widgets_alpha" in result.warnings[0]


def test_contracts_check_is_quiet_when_the_consumer_allows_at_least_its_producer_s_age(tmp_path: Path) -> None:
    _consumer_project(tmp_path, producer_age="PT26H", consumer_age="PT27H")

    assert contracts_check._check_product_dependencies(tmp_path).warnings == []


def test_the_demo_project_has_no_freshness_warning() -> None:
    assert contracts_check._check_product_dependencies(REPO_ROOT).warnings == []


def test_a_deploy_publishes_the_native_sla_and_the_policy_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake()
    _deploy_sla(tmp_path, fake, monkeypatch)

    product = fake.entities["dataProducts"]["order_revenue"]
    assert product["sla"] == {"tier": "GOLD", "dataFreshness": 1590, "dataQuality": 95}
    assert json.loads(product["extension"]["olfFreshnessPolicy"]) == {
        "tier": "GOLD",
        "maximumAge": "PT26H30M",
        "gracePeriod": "PT30M",
        "minimumQualityScore": 95,
        "readyBy": "09:00",
        "schedule": "0 7 * * *",
        "timezone": "Europe/Paris",
    }
    assert "olfFreshnessPolicy" in {p["name"] for p in fake.types["dataProduct"]["customProperties"]}


def test_a_repeat_deploy_writes_nothing_and_leaves_a_person_s_sla_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake()
    _deploy_sla(tmp_path / "first", fake, monkeypatch)
    fake.entities["dataProducts"]["order_revenue"]["sla"]["availability"] = 99.9
    fake.calls.clear()

    _deploy_sla(tmp_path / "second", fake, monkeypatch)

    assert fake.writes == []
    assert fake.entities["dataProducts"]["order_revenue"]["sla"]["availability"] == 99.9


def test_a_changed_declaration_patches_only_the_changed_sla_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake()
    _deploy_sla(tmp_path / "first", fake, monkeypatch)

    _deploy_sla(tmp_path / "second", fake, monkeypatch, governance=GOVERNANCE.replace("PT26H30M", "PT20H"))

    assert fake.entities["dataProducts"]["order_revenue"]["sla"]["dataFreshness"] == 1200


def test_drift_reports_a_changed_sla_and_is_empty_after_a_clean_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake()
    fake.seed("databases", "polaris.lakehouse_dev", {})
    models = tmp_path / "gold" / "models" / "order_revenue"
    models.mkdir(parents=True)
    (models / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: order_revenue\n    meta: {openlakeforge: {role: mart}}\n", encoding="utf-8"
    )
    deployer = _deploy_sla(tmp_path, fake, monkeypatch)
    assert deployer.drift() == []

    fake.entities["dataProducts"]["order_revenue"]["sla"]["dataFreshness"] = 60
    fake.entities["dataProducts"]["order_revenue"]["extension"]["olfFreshnessPolicy"] = "{}"
    changed = [item.detail for item in deployer.drift() if item.kind == "changed" and item.fqn == "order_revenue"]

    assert len(changed) == 1
    assert "sla/dataFreshness" in changed[0]
    assert "extension/olfFreshnessPolicy" in changed[0]


def test_a_hand_edited_native_sla_that_the_server_will_not_rewrite_warns_and_the_deploy_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake()
    fake.seed("databases", "polaris.lakehouse_dev", {})
    deployer = _deploy_sla(tmp_path / "first", fake, monkeypatch)
    fake.entities["dataProducts"]["order_revenue"]["sla"]["dataFreshness"] = 60
    fake.ignored_paths.add("/sla/dataFreshness")  # 1.13.6 stores an sla-only patch only by accident

    deployer = _deploy_sla(tmp_path / "second", fake, monkeypatch)

    warnings = [text for level, text in deployer.progress.messages if level == "warn"]
    assert any("order_revenue" in text and "does not record an sla-only change" in text for text in warnings)
    assert "sla/dataFreshness" in " ".join(item.detail for item in deployer.drift() if item.fqn == "order_revenue")


def test_a_policy_extension_that_does_not_read_back_still_fails_the_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openlakeforge_services.openmetadata import OpenMetadataError

    fake = _fake()
    fake.ignored_paths.add("/extension/olfFreshnessPolicy")

    with pytest.raises(OpenMetadataError, match="olfFreshnessPolicy"):
        _deploy_sla(tmp_path, fake, monkeypatch)


def _deploy_sla(
    root: Path, fake: FakeOpenMetadata, monkeypatch: pytest.MonkeyPatch, *, governance: str = GOVERNANCE
) -> Any:
    import test_business_metadata as base

    monkeypatch.setattr(base, "GOVERNANCE", governance)
    return _deploy(root, fake, monkeypatch)


def test_the_e2e_check_names_each_sla_field_that_differs() -> None:
    from olf.infra.e2e._assertions import sla_mismatches

    sla = parse_sla(SLA, where="p")
    native = {"tier": "GOLD", "dataFreshness": 240, "dataQuality": 95}
    policy = freshness_policy(sla)

    assert sla_mismatches("p", {"sla": native, "extension": {"olfFreshnessPolicy": policy}}, sla) == []
    problems = sla_mismatches("p", {"sla": {**native, "dataFreshness": 60}, "extension": {}}, sla)
    assert [p.split(" is ")[0] for p in problems] == ["p: sla.dataFreshness", "p: extension olfFreshnessPolicy"]


@pytest.mark.parametrize(
    "change",
    [
        {"tier": "SILVER"},
        {"freshness__maximumAge": "PT5H"},
        {"minimumQualityScore": 90},
        {"availability__gracePeriod": "PT1H"},
    ],
)
def test_every_declared_sla_edit_changes_the_policy_extension(change: dict[str, Any]) -> None:
    # OpenMetadata 1.13.6 records no change for `sla` itself, so each edit must also move a recorded field.
    before = freshness_policy(parse_sla(SLA, where="p"))
    after = freshness_policy(parse_sla(_with(**change), where="p"))

    assert before != after


def test_contracts_check_compares_exact_durations_not_rounded_minutes(tmp_path: Path) -> None:
    _consumer_project(tmp_path, producer_age="PT61S", consumer_age="PT1M")

    assert len(contracts_check._check_product_dependencies(tmp_path).warnings) == 1


@pytest.mark.parametrize("duration", ["P", "PT", "P1DT", "PT0H", "P0D", "P0DT0M", "1H"])
def test_the_json_schema_refuses_an_empty_or_zero_duration(duration: str) -> None:
    schema = json.loads((REPO_ROOT / "docs/schema/governance.schema.json").read_text(encoding="utf-8"))
    document = yaml.safe_load(BUSINESS_GOVERNANCE)
    document["domains"][0]["products"][0]["sla"] = _with(freshness__maximumAge=duration)

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


@pytest.mark.parametrize("duration", ["PT1S", "P1DT2H", "PT26H30M", "P1W", "P0DT5M"])
def test_the_json_schema_accepts_a_positive_duration(duration: str) -> None:
    schema = json.loads((REPO_ROOT / "docs/schema/governance.schema.json").read_text(encoding="utf-8"))
    document = yaml.safe_load(BUSINESS_GOVERNANCE)
    document["domains"][0]["products"][0]["sla"] = _with(freshness__maximumAge=duration)

    jsonschema.validate(document, schema)
