from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from olf.infra.commands import e2e as e2e_commands
from olf.infra.e2e import _stage_drill as drill
from olf.infra.e2e._shell import E2EError

GOVERNED = {"Secret/polaris-om-creds", "CronJob/openmetadata-trino-ingestion"}


def test_a_governed_stage_must_hold_the_secret_and_the_cronjob() -> None:
    held = {"dev": {"CronJob/openmetadata-trino-ingestion"}}

    assert drill.crawl_problems(held, ["dev"], []) == ["dev has no Secret/polaris-om-creds, but its governance is on"]


def test_an_ungoverned_stage_may_hold_none_of_the_crawl() -> None:
    held = {"prod": {*GOVERNED, "Job/openmetadata-trino-ingestion-123"}}

    assert drill.crawl_problems(held, [], ["prod"]) == [
        "prod still has CronJob/openmetadata-trino-ingestion, but its governance is off",
        "prod still has Job/openmetadata-trino-ingestion-123, but its governance is off",
        "prod still has Secret/polaris-om-creds, but its governance is off",
    ]


def test_a_revoked_stage_and_a_kept_stage_pass_together() -> None:
    assert drill.crawl_problems({"dev": GOVERNED, "prod": set()}, ["dev"], ["prod"]) == []


def test_a_removed_stage_is_reported_while_its_namespace_or_principals_remain() -> None:
    assert drill.namespace_problems({"olf-dev", "olf-prod"}, ["dev"], ["prod"]) == ["namespace olf-prod still exists"]
    assert drill.namespace_problems({"olf-prod"}, ["dev"], []) == ["namespace olf-dev is missing"]

    principals = {"olf-dev-processing", "deployer-dev", "trino-catalog-dev", "olf-prod-processing", "deployer-prod"}

    assert drill.principal_problems(principals, ["dev"], ["prod"]) == [
        "Polaris principal deployer-prod still exists",
        "Polaris principal olf-prod-processing still exists",
    ]


def test_a_re_enabled_stage_needs_all_three_principals_back() -> None:
    assert drill.principal_problems({"olf-prod-processing", "deployer-prod"}, ["prod"], []) == [
        "Polaris principal trino-catalog-prod is missing"
    ]


def test_principals_of_other_stages_and_the_platform_do_not_count_against_a_removed_stage() -> None:
    principals = {"openmetadata", "olf-dev-processing", "trino-people-dev", "root"}

    assert drill.principal_problems(principals, [], ["prod"]) == []


class _Cluster:
    """What `kubectl get` returns for a namespace that holds `crawl` in the namespaces named by its keys."""

    def __init__(self, crawl: dict[str, dict[str, list[str]]]) -> None:
        self.crawl = crawl

    def kubectl(self, cfg: Any, args: list[str], **_: Any) -> str:
        namespace = args[args.index("-n") + 1] if "-n" in args else ""
        held = self.crawl.get(namespace, {})
        if args[1] == "cronjob,job":
            assert "openlakeforge.io/job=openmetadata-trino-ingestion" in args
            items = [{"kind": kind, "metadata": {"name": name}} for kind, names in held.items() for name in names]
            return json.dumps({"items": [item for item in items if item["kind"] != "Secret"]})
        if args[1] == "secrets":
            return "\n".join(f"secret/{name}" for name in held.get("Secret", []) + ["unrelated"])
        return json.dumps({"items": [{"metadata": {"name": name}} for name in self.crawl]})


def test_the_crawl_is_read_from_the_stage_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _Cluster(
        {
            "olf-dev": {"Secret": ["polaris-om-creds"], "CronJob": ["openmetadata-trino-ingestion"]},
            "olf-prod": {"Secret": [], "CronJob": []},
        }
    )
    monkeypatch.setattr(drill, "kubectl", cluster.kubectl)
    cfg: Any = SimpleNamespace()

    drill.check_crawl(cfg, ["dev"], ["prod"])
    with pytest.raises(E2EError, match="dev still has"):
        drill.check_crawl(cfg, [], ["dev"])


def test_a_stage_whose_namespace_is_gone_holds_no_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drill, "kubectl", _Cluster({}).kubectl)
    cfg: Any = SimpleNamespace()

    drill.check_crawl(cfg, [], ["prod"])
    drill.check_namespaces(cfg, [], ["prod"])


def test_the_command_refuses_an_unknown_stage_and_a_call_with_nothing_to_check() -> None:
    runner = CliRunner()

    unknown = runner.invoke(e2e_commands.app, ["stage-drill", "crawl", "--absent", "staging"])
    empty = runner.invoke(e2e_commands.app, ["stage-drill", "principals"])

    assert unknown.exit_code != 0
    assert "unknown stage 'staging'" in unknown.output
    assert empty.exit_code != 0
    assert "--present or --absent" in empty.output
