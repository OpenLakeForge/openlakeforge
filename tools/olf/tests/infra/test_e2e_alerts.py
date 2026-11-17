from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import HTTPServer
from pathlib import Path

import pytest
import requests
import yaml
from openlakeforge_core.quality import PipelineOutcome, QualityManifest, QualityOutcome

from olf.infra.e2e import _alerts, alert_receiver_stub
from olf.infra.e2e._shell import E2EError

ROOT = Path(__file__).parents[4]
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def stub() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), alert_receiver_stub.make_handler(alert_receiver_stub.Receiver()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_the_stub_records_posts_without_credentials_and_answers_the_set_status(stub: str) -> None:
    headers = {"Idempotency-Key": "i-1:opened", "Authorization": "Bearer secret-value-1234567890"}
    assert requests.post(f"{stub}/hook", json={"a": 1}, headers=headers, timeout=5).status_code == 200
    requests.put(f"{stub}/_status/503", timeout=5)
    assert requests.post(f"{stub}/hook", data="{}", timeout=5).status_code == 503

    records = requests.get(f"{stub}/_records", timeout=5).json()
    assert [json.loads(r["body"]) for r in records] == [{"a": 1}, {}]
    assert records[0]["headers"] == {"content-type": "application/json", "idempotency-key": "i-1:opened"}
    assert "secret-value" not in json.dumps(records)

    requests.delete(f"{stub}/_records", timeout=5)
    assert requests.get(f"{stub}/_records", timeout=5).json() == []


def test_the_stub_serves_only_its_hook_and_valid_statuses(stub: str) -> None:
    assert requests.post(f"{stub}/other", data="{}", timeout=5).status_code == 404
    assert requests.put(f"{stub}/_status/abc", timeout=5).status_code == 400
    assert requests.put(f"{stub}/_status/99", timeout=5).status_code == 400


CRONJOB = {
    "metadata": {"namespace": "olf-system"},
    "spec": {
        "jobTemplate": {
            "metadata": {"labels": {"openlakeforge.io/job": "alert-evaluator"}},
            "spec": {
                "ttlSecondsAfterFinished": 3600,
                "template": {"spec": {"containers": [{"name": "evaluator", "command": ["python", "-m", "x"]}]}},
            },
        }
    },
}


def test_the_evaluation_job_keeps_the_pod_template_and_adds_only_the_e2e_flags() -> None:
    job = _alerts.evaluation_job(CRONJOB, "eval-1", now=NOW, state_prefix="alerts/e2e/")

    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["python", "-m", "x"]
    assert container["args"] == ["--state-prefix", "alerts/e2e/", "--now", "2026-11-16T12:00:00+00:00"]
    assert job["metadata"]["labels"] == {"openlakeforge.io/job": "alert-evaluator"}
    assert "ttlSecondsAfterFinished" not in job["spec"]
    assert "args" not in CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]


def test_the_evaluation_job_without_a_clock_leaves_the_clock_alone() -> None:
    job = _alerts.evaluation_job(CRONJOB, "eval-1", now=None, state_prefix="alerts/e2e/")
    assert job["spec"]["template"]["spec"]["containers"][0]["args"] == ["--state-prefix", "alerts/e2e/"]


def test_the_evaluation_job_can_read_its_own_settings_and_ca() -> None:
    job = _alerts.evaluation_job(CRONJOB, "eval-1", now=None, state_prefix="p/", settings_configmap="cm")

    pod = job["spec"]["template"]["spec"]
    assert pod["containers"][0]["args"] == ["--state-prefix", "p/", "--settings", "/e2e/alerts.json"]
    assert pod["containers"][0]["volumeMounts"] == [{"name": "e2e", "mountPath": "/e2e", "readOnly": True}]
    assert pod["volumes"] == [{"name": "e2e", "configMap": {"name": "cm"}}]
    assert "volumes" not in CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]


def test_the_e2e_settings_trust_the_stubs_ca_and_change_nothing_else() -> None:
    original = {"ca_file": None, "receivers": [{"name": "on-call"}], "s3_region": "r"}
    assert json.loads(_alerts.e2e_settings(json.dumps(original))) == {**original, "ca_file": "/e2e/ca.crt"}
    assert (
        json.loads(_alerts.e2e_settings(json.dumps({**original, "ca_file": "/ca/ca.crt"})))["ca_file"] == "/e2e/ca.crt"
    )


def test_the_state_prefix_is_inside_the_evaluators_grant_and_off_its_real_paths() -> None:
    main_tf = (ROOT / "infra/terraform/environments/local/main.tf").read_text()
    (granted,) = re.findall(r'alerts_prefix\s*=\s*"([^"]+)"', main_tf)
    assert _alerts.STATE_PREFIX.startswith(granted)
    assert _alerts.STATE_PREFIX != granted
    # The evaluator's own layout under the base prefix is `state/` and `history/` (alerting/store.py).
    assert not any(_alerts.STATE_PREFIX.removeprefix(granted).startswith(real) for real in ("state/", "history/"))


def _receiver_cronjob(*receivers: tuple[str, str]) -> dict:
    env = [
        {"name": f"OPENLAKEFORGE_ALERT_RECEIVER_{i}", "valueFrom": {"secretKeyRef": {"name": n, "key": k}}}
        for i, (n, k) in enumerate(receivers)
    ] + [{"name": "TMPDIR", "value": "/tmp"}]
    return {"spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "e", "env": env}]}}}}}}


@pytest.mark.parametrize(
    ("receivers", "runs"),
    [
        ([(_alerts.RECEIVER_REF_NAME, _alerts.RECEIVER_KEY)], True),
        ([("slack-hook", "url")], False),
        ([(_alerts.RECEIVER_REF_NAME, "other-key")], False),
        ([(_alerts.RECEIVER_REF_NAME, _alerts.RECEIVER_KEY), ("slack-hook", "url")], False),
        ([], False),
    ],
)
def test_the_scenario_runs_only_when_the_stub_is_the_sole_receiver(receivers: list, runs: bool) -> None:
    assert _alerts.sole_receiver_is_the_stub(_receiver_cronjob(*receivers)) is runs


@pytest.mark.parametrize(
    ("status", "outcome"),
    [({}, None), ({"active": 1}, None), ({"succeeded": 1}, "succeeded"), ({"failed": 1}, "failed")],
)
def test_job_outcome(status: dict, outcome: str | None) -> None:
    assert _alerts.job_outcome({"status": status}) == outcome


def _record(**body: object) -> dict:
    return {"headers": {"idempotency-key": "k"}, "body": json.dumps(body)}


def test_a_stub_policy_admits_only_the_evaluator_on_the_stubs_port() -> None:
    policy = _alerts.stub_network_policy("olf-system")
    label_key, label_value = _alerts.LABEL.split("=")
    spec = policy["spec"]
    assert policy["metadata"] == {"name": _alerts.STUB_NAME, "namespace": "olf-system"}
    assert spec["podSelector"] == {"matchLabels": {label_key: label_value}}
    assert spec["policyTypes"] == ["Ingress"]
    (rule,) = spec["ingress"]
    assert rule["from"] == [{"podSelector": {"matchLabels": {"openlakeforge.io/job": _alerts.EVALUATOR}}}]
    assert rule["ports"] == [{"protocol": "TCP", "port": alert_receiver_stub.PORT}]


def test_the_evaluation_job_pods_carry_the_label_the_stub_policy_admits() -> None:
    cronjob = copy.deepcopy(CRONJOB)
    cronjob["spec"]["jobTemplate"]["spec"]["template"]["metadata"] = {
        "labels": {"openlakeforge.io/job": "alert-evaluator"}
    }
    pod = _alerts.evaluation_job(cronjob, "j", now=None, state_prefix="p/")["spec"]["template"]["metadata"]["labels"]
    assert pod == {"openlakeforge.io/job": _alerts.EVALUATOR}
    tf = (ROOT / "infra/terraform/modules/observability/alerts/main.tf").read_text()
    assert '"openlakeforge.io/job" = "alert-evaluator"' in tf


def test_an_underscore_field_in_the_real_payload_is_still_checked() -> None:
    body = {"schema": "s", "_credentials": "hunter2-hunter2-hunter2", "links": {}}
    (found,) = _alerts.payloads([_record(**body)])
    problems = _alerts.payload_problems(found, link=None, secrets={"a secret": "hunter2-hunter2-hunter2"})
    assert "unlisted field '_credentials'" in problems
    assert any("a secret" in p for p in problems)
    with pytest.raises(E2EError, match="harness field"):
        _alerts.payloads([_record(_idempotency_key="forged")])


def test_exactly_one_names_what_was_missing_or_repeated() -> None:
    found = _alerts.payloads([_record(condition="run_failed", event="opened"), _record(condition="quality")])
    assert _alerts.exactly_one(found, "x", condition="run_failed")["_idempotency_key"] == "k"
    with pytest.raises(E2EError, match="got 0 of 2"):
        _alerts.exactly_one(found, "x", condition="missed_run")
    with pytest.raises(E2EError, match="got 2 of 3"):
        _alerts.exactly_one([found[0], found[0], found[1]], "x", condition="run_failed")


def test_the_run_link_is_the_stages_perimeter_route_or_nothing() -> None:
    routes = {"stage/dev/orchestration": "https://dagster-dev.olf.localhost/"}
    assert _alerts.expected_run_link(routes, "dev", "r1") == "https://dagster-dev.olf.localhost/runs/r1"
    assert _alerts.expected_run_link(routes, "prod", "r1") is None


GOOD = {
    "schema": "openlakeforge.alert.v1",
    "run_id": "r1",
    "links": {"run": "https://d/runs/r1"},
    "_idempotency_key": "k",
}


def test_a_clean_payload_has_no_problems() -> None:
    assert _alerts.payload_problems(GOOD, link="https://d/runs/r1", secrets={"url": "https://x/" + "a" * 20}) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"tags": {"a": "b"}}, "unlisted field 'tags'"),
        ({"links": {"run": "https://d/runs/r1", "logs": "u"}}, "unlisted link 'logs'"),
        ({"links": {"run": "https://other/runs/r1"}}, "run link is"),
        ({"summary": "failed with token-" + "z" * 20}, "contains the value of Secret tag"),
    ],
)
def test_payload_problems_name_each_breach(change: dict, expected: str) -> None:
    problems = _alerts.payload_problems(
        {**GOOD, **change}, link="https://d/runs/r1", secrets={"tag": "token-" + "z" * 20}
    )
    assert any(expected in problem for problem in problems)


def test_the_quality_manifest_is_complete_and_names_the_floe_outcome() -> None:
    base = QualityManifest("dev", "sales", "orders", "run-1")
    manifest = _alerts.quality_manifest(base, QualityOutcome.ERROR, "error", PipelineOutcome.BLOCKED)
    assert manifest.complete
    assert [(r.engine, r.outcome, r.pipeline_outcome) for r in manifest.results] == [
        ("floe", QualityOutcome.ERROR, PipelineOutcome.BLOCKED)
    ]
    assert QualityManifest.from_json(manifest.to_json()) == manifest


def test_find_schedule_reads_the_selector_and_the_next_two_due_times() -> None:
    data = {
        "repositoriesOrError": {
            "__typename": "RepositoryConnection",
            "nodes": [
                {
                    "name": "__repository__",
                    "location": {"name": "loc"},
                    "schedules": [
                        {"name": "other", "pipelineName": "x", "futureTicks": {"results": []}},
                        {
                            "name": "gold_maintenance_weekly",
                            "pipelineName": "gold_maintenance",
                            "futureTicks": {"results": [{"timestamp": 1_800_000_000}, {"timestamp": 1_800_604_800}]},
                        },
                    ],
                }
            ],
        }
    }
    ref = _alerts.find_schedule(data, "gold_maintenance")
    assert ref is not None
    assert ref.selector() == {
        "repositoryLocationName": "loc",
        "repositoryName": "__repository__",
        "scheduleName": "gold_maintenance_weekly",
    }
    assert (ref.due[1] - ref.due[0]).days == 7
    assert _alerts.find_schedule(data, "absent") is None


def test_the_receiver_is_declared_where_the_e2e_and_the_nightly_expect_it() -> None:
    profile = yaml.safe_load((ROOT / "openlakeforge.conformance.yaml").read_text())
    (receiver,) = profile["spec"]["observability"]["alerts"]["receivers"]
    assert receiver["secret_ref"] == {"name": _alerts.RECEIVER_REF_NAME, "key": _alerts.RECEIVER_KEY}
    workflow = (ROOT / ".github/workflows/local-full-e2e.yml").read_text()
    assert f"create secret generic {_alerts.RECEIVER_REF_NAME}" in workflow
    assert f"--from-literal={_alerts.RECEIVER_KEY}=https://{_alerts.STUB_NAME}.olf-system.svc/hook" in workflow


def test_the_flow_row_for_the_stub_matches_the_label_the_e2e_sets() -> None:
    flows = yaml.safe_load((ROOT / "release/network-flows.yaml").read_text())["flows"]
    (row,) = [r for r in flows if r["id"] == "F41"]
    label_key, label_value = _alerts.LABEL.split("=")
    assert row["from"]["pods"] == {"openlakeforge.io/job": _alerts.EVALUATOR}
    assert row["to"]["pods"] == {label_key: label_value}
    assert (row["ports"], row["tag"]) == ([alert_receiver_stub.PORT], "T")
