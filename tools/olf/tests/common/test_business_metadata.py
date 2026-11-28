"""What a deploy publishes of the v1beta2 business fields: owners, tags and the lifecycle extension."""

from pathlib import Path

import pytest
from fake_openmetadata import FakeOpenMetadata
from openlakeforge_services.openmetadata import OpenMetadataError
from openlakeforge_services.ownership import tag_operations
from test_metadata_deployer import GOLD, _deployer, _descriptors, _Recorder

GOVERNANCE = """\
apiVersion: openlakeforge.io/v1beta2
kind: Governance
domains:
  - name: sales
    displayName: Sales
    description: Sales domain
    domainType: Consumer-aligned
    owner: {type: team, ref: sales_data}
    experts: [{type: user, ref: finance_analyst}]
    classification: Internal
    products:
      - id: order_revenue
        displayName: Sales Order Revenue
        description: Revenue from orders.
        type: REPORTS
        tier: Tier2
        lifecycle: PRODUCTION
        classification: Confidential
        reviewers: [{type: user, ref: finance_lead}]
        termsOfUse: {allowed: [internal_reporting], prohibited: [external_redistribution]}
"""


def _fake(*, org_owner: bool = False) -> FakeOpenMetadata:
    fake = FakeOpenMetadata()
    fake.seed("teams", "sales_data", {"teamType": "Organization" if org_owner else "Group"})
    for name in ("finance_analyst", "finance_lead"):
        fake.seed("users", name, {})
    fake.seed("tags", "Tier.Tier1", {})
    fake.seed("tags", "Tier.Tier2", {})
    fake.seed("tags", "Tier.Tier3", {})
    fake.seed("tables", f"{GOLD}.order_revenue", {"columns": [{"name": "revenue", "description": "from dbt"}]})
    return fake


def _deploy(root: Path, fake: FakeOpenMetadata, monkeypatch: pytest.MonkeyPatch, revision: str = "rev1") -> None:
    paths = _descriptors(root)
    paths[0].write_text(GOVERNANCE)
    model = root / "gold" / "models" / "order_revenue" / "order_revenue.sql"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_text("select 1")
    deployer = _deployer(
        root, allow_missing_assets=False, progress=_Recorder(), descriptor_paths=paths, revision=revision
    )
    monkeypatch.setattr(deployer.client, "request", fake.request)
    deployer.deploy()


def _tag_names(entity: dict) -> set[str]:
    return {tag["tagFQN"] for tag in entity.get("tags", [])}


def test_a_deploy_publishes_owners_types_tags_and_the_lifecycle_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake()

    _deploy(tmp_path, fake, monkeypatch)

    domain = fake.entities["domains"]["sales"]
    product = fake.entities["dataProducts"]["order_revenue"]
    assert domain["domainType"] == "Consumer-aligned"
    assert [owner["id"] for owner in domain["owners"]] == [fake.entities["teams"]["sales_data"]["id"]]
    assert [expert["type"] for expert in domain["experts"]] == ["user"]
    assert _tag_names(domain) == {"DataSensitivity.Internal"}
    assert product["dataProductType"] == "REPORTS"
    assert product["owners"] == domain["owners"]  # inherited from the domain by the loader
    assert [reviewer["id"] for reviewer in product["reviewers"]] == [fake.entities["users"]["finance_lead"]["id"]]
    assert _tag_names(product) == {"Tier.Tier2", "DataSensitivity.Confidential"}
    assert product["extension"]["olfLifecycle"] == "PRODUCTION"
    assert product["extension"]["olfTermsOfUse"] == (
        '{"allowed": ["internal_reporting"], "prohibited": ["external_redistribution"]}'
    )
    assert "lifecycleStage" not in product
    assert _tag_names(fake.entities["tables"][f"{GOLD}.order_revenue"]) == _tag_names(product)
    assert not _tag_names(fake.entities["tables"].get("polaris.lakehouse_dev.sales_silver.orders", {}))
    assert {"DataSensitivity.Internal", "DataSensitivity.Confidential"} <= set(fake.entities["tags"])
    assert fake.entities["classifications"]["DataSensitivity"]["mutuallyExclusive"] is True


def test_a_repeat_deploy_of_the_business_fields_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake()
    _deploy(tmp_path / "first", fake, monkeypatch)
    fake.calls.clear()

    _deploy(tmp_path / "second", fake, monkeypatch)

    assert fake.writes == []


def test_a_person_s_tag_survives_and_a_conflicting_tier_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake()
    table = fake.entities["tables"][f"{GOLD}.order_revenue"]
    table["tags"] = [
        {"tagFQN": "PII.Sensitive", "source": "Classification", "labelType": "Manual", "state": "Confirmed"},
        {"tagFQN": "Tier.Tier3", "source": "Classification", "labelType": "Manual", "state": "Confirmed"},
    ]

    _deploy(tmp_path, fake, monkeypatch)

    assert _tag_names(table) == {"PII.Sensitive", "Tier.Tier2", "DataSensitivity.Confidential"}


def test_an_unknown_owner_fails_naming_the_reference_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake()
    del fake.entities["teams"]["sales_data"]

    with pytest.raises(OpenMetadataError, match=r"domains\[sales\]\.owner: team 'sales_data' does not exist"):
        _deploy(tmp_path, fake, monkeypatch)

    assert fake.writes == []


def test_an_owner_team_that_is_not_a_group_fails_the_deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(org_owner=True)

    with pytest.raises(OpenMetadataError, match="only lets a team of type Group own entities"):
        _deploy(tmp_path, fake, monkeypatch)

    assert fake.writes == []


def test_a_field_the_server_answers_200_to_and_ignores_fails_the_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake()
    fake.ignored_paths.add("/extension/olfLifecycle")

    with pytest.raises(OpenMetadataError, match="olfLifecycle"):
        _deploy(tmp_path, fake, monkeypatch)


def test_tag_operations_leave_other_classifications_and_derived_tags_alone() -> None:
    current = {
        "tags": [
            {"tagFQN": "Tier.Tier5", "labelType": "Derived"},
            {"tagFQN": "Tier.Tier4", "labelType": "Manual"},
            {"tagFQN": "PII.Sensitive", "labelType": "Manual"},
        ]
    }

    operations = tag_operations(current, ["Tier.Tier1"])

    assert [(op["op"], op["path"]) for op in operations] == [("remove", "/tags/1"), ("add", "/tags/-")]
    assert tag_operations(current, []) == []


def test_a_glossary_term_that_shares_a_classification_name_is_not_ours_to_touch() -> None:
    term = {"tagFQN": "Tier.Business", "source": "Glossary", "labelType": "Manual"}
    lookalike = {"tagFQN": "Tier.Tier1", "source": "Glossary", "labelType": "Manual"}

    operations = tag_operations({"tags": [term, lookalike]}, ["Tier.Tier1"])

    assert [(op["op"], op["path"]) for op in operations] == [("add", "/tags/-")]


def test_a_team_listed_as_expert_fails_before_any_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake()
    monkeypatch.setattr(
        "test_business_metadata.GOVERNANCE",
        GOVERNANCE.replace("{type: user, ref: finance_analyst}", "{type: team, ref: sales_data}"),
    )

    with pytest.raises(OpenMetadataError, match=r"domains\[sales\]\.experts: team 'sales_data' is not allowed"):
        _deploy(tmp_path, fake, monkeypatch)

    assert fake.writes == []


def test_removals_name_the_position_in_the_stored_list_not_in_the_filtered_one() -> None:
    glossary = {"tagFQN": "Business.Revenue", "source": "Glossary", "labelType": "Manual"}
    stale = {"tagFQN": "Tier.Tier4", "source": "Classification", "labelType": "Manual"}

    operations = tag_operations({"tags": [glossary, stale]}, ["Tier.Tier1"])

    assert operations[0] == {"op": "remove", "path": "/tags/1"}
