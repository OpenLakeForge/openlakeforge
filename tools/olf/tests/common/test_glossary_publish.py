"""Glossary publishing against the in-memory OpenMetadata: order, idempotence, pending columns, no deletes."""

from pathlib import Path

import pytest
from conftest import write_floe_entity
from fake_openmetadata import FakeOpenMetadata
from openlakeforge_services.governance import MetadataDeployer
from openlakeforge_services.ownership import marker_of
from test_metadata_deployer import GOLD, _deployer, _Recorder

GLOSSARY = """\
apiVersion: openlakeforge.io/v1alpha1
kind: Glossary
name: sales
displayName: Sales Vocabulary
description: Shared commercial definitions.
owner: {type: team, ref: sales_data}
terms:
  - name: NetRevenue
    displayName: Net Revenue
    description: Revenue after discounts.
    synonyms: [Net Sales]
    status: Approved
    relatedTerms: [sales.GrossRevenue]
    references:
      - {name: Finance policy, endpoint: "https://example.internal/finance"}
  - name: GrossRevenue
    displayName: Gross Revenue
    description: Revenue at list price.
  - name: EmeaNetRevenue
    displayName: EMEA Net Revenue
    description: Net revenue booked in EMEA.
    parent: NetRevenue
"""

SCHEMA = """\
version: 2
models:
  - name: order_revenue
    columns:
      - name: revenue
        meta:
          openlakeforge:
            glossaryTerms: [sales.NetRevenue]
      - name: not_crawled
        meta:
          openlakeforge:
            glossaryTerms: [sales.GrossRevenue]
"""


def _project(root: Path) -> list[Path]:
    (root / "bronze" / "crm").mkdir(parents=True)
    source = root / "bronze" / "crm" / "source.yaml"
    source.write_text(
        "apiVersion: openlakeforge.io/v1beta1\nkind: Source\nname: crm\ndisplayName: CRM\n"
        "description: crm source.\nstatus: active\nresources:\n  - name: orders\n    description: Raw sales orders.\n"
    )
    governance = root / "governance.yml"
    governance.write_text(
        "apiVersion: openlakeforge.io/v1beta2\nkind: Governance\n"
        "domains:\n  - name: sales\n    displayName: Sales\n    description: Sales domain\n"
        "    glossaryTerms: [sales.GrossRevenue]\n"
        "    products:\n      - id: order_revenue\n        displayName: Sales Order Revenue\n"
        "        description: Revenue from orders.\n        glossaryTerms: [sales.NetRevenue]\n"
    )
    write_floe_entity(root, "sales", "orders", "crm")
    (root / "glossaries").mkdir()
    (root / "glossaries" / "sales.yml").write_text(GLOSSARY)
    models = root / "gold" / "models" / "order_revenue"
    models.mkdir(parents=True)
    (models / "order_revenue.sql").write_text("select 1")
    (models / "schema.yml").write_text(SCHEMA)
    return [governance, source]


def _deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MetadataDeployer, FakeOpenMetadata]:
    deployer = _deployer(
        tmp_path, allow_missing_assets=False, progress=_Recorder(), descriptor_paths=_project(tmp_path)
    )
    fake = FakeOpenMetadata()
    fake.seed("teams", "sales_data", {"teamType": "Group"})
    fake.seed("tables", f"{GOLD}.order_revenue", {"columns": [{"name": "revenue", "tags": []}]})
    monkeypatch.setattr(deployer.client, "request", fake.request)
    return deployer, fake


def test_terms_are_published_with_the_marker_before_anything_is_tagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, fake = _deploy(tmp_path, monkeypatch)

    deployer.deploy()

    glossary = fake.entities["glossaries"]["sales"]
    term = fake.entities["glossaryTerms"]["sales.NetRevenue"]
    assert marker_of(glossary) and marker_of(term)
    assert glossary["owners"] == [{"id": fake.entities["teams"]["sales_data"]["id"], "type": "team"}]
    assert term["synonyms"] == ["Net Sales"] and term["entityStatus"] == "Approved"
    gross = fake.entities["glossaryTerms"]["sales.GrossRevenue"]["id"]
    assert [(r["term"]["id"], r["relationType"]) for r in term["relatedTerms"]] == [(gross, "relatedTo")]
    held_by_gross = fake.entities["glossaryTerms"]["sales.GrossRevenue"]["relatedTerms"]
    assert [relation["term"]["id"] for relation in held_by_gross] == [term["id"]]  # the reciprocal relation
    writes = [(method, path) for method, path in fake.writes if "glossaryTerms" in path or "/tags" in path]
    first_tag = next(i for i, (method, path) in enumerate(fake.calls) if method == "PATCH" and "dataProducts" in path)
    assert all(fake.calls.index(call) < first_tag for call in writes if call[0] == "POST")


def test_a_nested_term_is_published_under_its_parents_fqn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deployer, fake = _deploy(tmp_path, monkeypatch)

    deployer.deploy()

    assert "sales.NetRevenue.EmeaNetRevenue" in fake.entities["glossaryTerms"]


def test_product_domain_and_crawled_column_are_tagged_and_an_uncrawled_column_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, fake = _deploy(tmp_path, monkeypatch)

    deployer.deploy()

    def tags(collection: str, fqn: str) -> list[str]:
        return [tag["tagFQN"] for tag in fake.entities[collection][fqn]["tags"]]

    assert tags("dataProducts", "order_revenue") == ["sales.NetRevenue"]
    assert tags("domains", "sales") == ["sales.GrossRevenue"]
    column = fake.entities["tables"][f"{GOLD}.order_revenue"]["columns"][0]
    assert [tag["tagFQN"] for tag in column["tags"]] == ["sales.NetRevenue"]
    assert column["tags"][0]["source"] == "Glossary"
    assert len(fake.entities["tables"][f"{GOLD}.order_revenue"]["columns"]) == 1  # no stub for the pending column


def test_a_repeat_deploy_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deployer, fake = _deploy(tmp_path, monkeypatch)
    deployer.deploy()
    fake.calls.clear()

    deployer.deploy()

    assert fake.writes == []


def test_human_tags_and_terms_survive_and_a_removed_term_is_reported_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, fake = _deploy(tmp_path, monkeypatch)
    fake.seed("glossaries", "ops", {"description": "made by hand"})
    fake.seed("glossaryTerms", "ops.Uptime", {"description": "made by hand"})
    deployer.deploy()
    human = {"tagFQN": "PII.Sensitive", "source": "Classification", "labelType": "Manual", "state": "Confirmed"}
    fake.entities["tables"][f"{GOLD}.order_revenue"]["columns"][0]["tags"].append(human)
    # A term OLF published earlier that Git no longer declares.
    fake.seed(
        "glossaryTerms", "sales.Retired", {"extension": fake.entities["glossaryTerms"]["sales.NetRevenue"]["extension"]}
    )
    fake.calls.clear()
    progress = _Recorder()
    deployer.progress = deployer._reconciler.progress = progress

    deployer.deploy()

    assert fake.writes == []
    assert human in fake.entities["tables"][f"{GOLD}.order_revenue"]["columns"][0]["tags"]
    assert "ops.Uptime" in fake.entities["glossaryTerms"] and "sales.Retired" in fake.entities["glossaryTerms"]
    warnings = [message for level, message in progress.messages if level == "warn"]
    assert any("sales.Retired" in message for message in warnings)
    assert not any("ops.Uptime" in message for message in warnings)


def test_a_missing_owner_team_fails_before_any_glossary_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deployer, fake = _deploy(tmp_path, monkeypatch)
    del fake.entities["teams"]["sales_data"]

    with pytest.raises(Exception, match="sales_data"):
        deployer.deploy()
    assert "glossaries" not in fake.entities


def _drift_lines(deployer: MetadataDeployer) -> list[str]:
    return [item.line() for item in deployer.drift()]


def test_drift_is_empty_after_a_deploy_and_reports_what_git_dropped_or_the_crawl_lacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, fake = _deploy(tmp_path, monkeypatch)
    fake.entities["tables"][f"{GOLD}.order_revenue"]["columns"].append({"name": "not_crawled", "tags": []})
    deployer.deploy()
    assert not [line for line in _drift_lines(deployer) if "glossary" in line or "sales." in line]

    marker = fake.entities["glossaryTerms"]["sales.NetRevenue"]["extension"]
    fake.seed("glossaryTerms", "sales.Retired", {"extension": marker})
    fake.entities["glossaryTerms"]["sales.NetRevenue"]["synonyms"] = ["edited by hand"]
    del fake.entities["dataProducts"]["order_revenue"]["tags"]
    fake.entities["tables"][f"{GOLD}.order_revenue"]["columns"].pop()

    lines = _drift_lines(deployer)

    assert any(line.startswith("stale") and "sales.Retired" in line for line in lines)
    assert any(line.startswith("changed") and "sales.NetRevenue" in line and "synonyms" in line for line in lines)
    assert any("order_revenue" in line and "glossary tags not attached" in line for line in lines)
    assert not any("not_crawled" in line for line in lines)  # pending, not drift


def test_a_link_on_an_uncrawled_column_is_warned_as_pending_on_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, _ = _deploy(tmp_path, monkeypatch)
    progress = _Recorder()
    deployer.progress = deployer._reconciler.progress = progress

    deployer.deploy()

    warnings = [message for level, message in progress.messages if level == "warn"]
    assert any("pending" in message and f"{GOLD}.order_revenue.not_crawled" in message for message in warnings)


def test_drift_reports_a_changed_owner_and_a_missing_declared_relation_but_not_extra_relations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, fake = _deploy(tmp_path, monkeypatch)
    deployer.deploy()
    net = fake.entities["glossaryTerms"]["sales.NetRevenue"]
    assert not [line for line in _drift_lines(deployer) if "glossary" in line]

    fake.seed("teams", "other_team", {"teamType": "Group"})
    fake.entities["glossaries"]["sales"]["owners"] = [
        {"id": fake.entities["teams"]["other_team"]["id"], "type": "team"}
    ]
    net["relatedTerms"].append({"term": {"id": "x", "fullyQualifiedName": "ops.Uptime"}, "relationType": "relatedTo"})
    assert any("glossary sales" in line and "owners" in line for line in _drift_lines(deployer))
    assert not any("relatedTerms" in line for line in _drift_lines(deployer))

    net["relatedTerms"] = []
    fake.entities["glossaries"]["sales"]["owners"] = [
        {"id": fake.entities["teams"]["sales_data"]["id"], "type": "team"}
    ]
    lines = _drift_lines(deployer)
    assert any("sales.NetRevenue" in line and "relatedTerms" in line for line in lines)
    assert not any("owners" in line for line in lines)


def test_a_column_whose_last_declaration_left_git_keeps_its_tag_and_is_warned_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, fake = _deploy(tmp_path, monkeypatch)
    deployer.deploy()
    schema = tmp_path / "gold" / "models" / "order_revenue" / "schema.yml"
    schema.write_text(SCHEMA.replace("glossaryTerms: [sales.NetRevenue]", "glossaryTerms: [sales.GrossRevenue]", 1))
    progress = _Recorder()
    deployer.progress = deployer._reconciler.progress = progress

    deployer.deploy()

    column = fake.entities["tables"][f"{GOLD}.order_revenue"]["columns"][0]
    assert "sales.NetRevenue" in [tag["tagFQN"] for tag in column["tags"]]  # kept, never removed
    warnings = [message for level, message in progress.messages if level == "warn"]
    assert any("revenue -> sales.NetRevenue" in message for message in warnings)


def test_drift_reports_an_extra_owner_and_a_status_that_differs_from_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, fake = _deploy(tmp_path, monkeypatch)
    deployer.deploy()
    assert not [line for line in _drift_lines(deployer) if "glossary" in line]

    fake.seed("teams", "other_team", {"teamType": "Group"})
    owners = fake.entities["glossaries"]["sales"]["owners"]
    owners.append({"id": fake.entities["teams"]["other_team"]["id"], "type": "team"})
    fake.entities["glossaryTerms"]["sales.NetRevenue"]["entityStatus"] = "Draft"

    lines = _drift_lines(deployer)

    assert any("glossary sales" in line and "owners" in line for line in lines)
    assert any("sales.NetRevenue" in line and "entityStatus" in line for line in lines)
