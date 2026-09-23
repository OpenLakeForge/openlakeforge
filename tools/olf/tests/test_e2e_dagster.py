import json
from pathlib import Path
from typing import Any

import pytest
from conftest import E2E_INVENTORY, e2e_cfg

from olf.clients import dagster as dagster_client_module
from olf.clients.base import ServiceClientError
from olf.clients.dagster import DagsterClient, DagsterTransientError
from olf.e2e import _dagster
from olf.e2e._shell import E2EConfig, E2EError


def test_dagster_repository_discovery_finds_all_pipeline_locations_in_merged_location() -> None:
    client = DagsterClient(
        "http://dagster/graphql",
        request_json=lambda _query, _variables=None: {
            "data": {
                "workspaceOrError": {
                    "__typename": "Workspace",
                    "locationEntries": [
                        {
                            "name": "openlakeforge-dagster",
                            "locationOrLoadError": {
                                "__typename": "RepositoryLocation",
                                "repositories": [
                                    {
                                        "name": "__repository__",
                                        "pipelines": [
                                            {"name": "sales_order_revenue_pipeline"},
                                            {"name": "sales_customer_health_pipeline"},
                                            {"name": "supply_chain_inventory_reliability_pipeline"},
                                        ],
                                    }
                                ],
                            },
                        }
                    ],
                }
            }
        },
    )

    assert client.discover_repository("sales_order_revenue_pipeline") == ("openlakeforge-dagster", "__repository__")
    assert client.discover_repository("sales_customer_health_pipeline") == ("openlakeforge-dagster", "__repository__")
    assert client.discover_repository("supply_chain_inventory_reliability_pipeline") == (
        "openlakeforge-dagster",
        "__repository__",
    )


def test_dagster_repository_discovery_finds_job_location() -> None:
    client = DagsterClient(
        "http://dagster/graphql",
        request_json=lambda _query, _variables=None: {
            "data": {
                "workspaceOrError": {
                    "__typename": "Workspace",
                    "locationEntries": [
                        {
                            "name": "openlakeforge-dagster",
                            "locationOrLoadError": {
                                "__typename": "RepositoryLocation",
                                "repositories": [
                                    {
                                        "name": "__repository__",
                                        "jobs": [{"name": "sales_order_revenue_pipeline"}],
                                    }
                                ],
                            },
                        }
                    ],
                }
            }
        },
    )

    assert client.discover_repository("sales_order_revenue_pipeline") == ("openlakeforge-dagster", "__repository__")


def test_dagster_repository_discovery_raises_on_workspace_error() -> None:
    client = DagsterClient(
        "http://dagster/graphql",
        request_json=lambda _query, _variables=None: {"errors": [{"message": "workspace unavailable"}]},
    )

    with pytest.raises(ServiceClientError, match="workspace unavailable"):
        client.discover_repository("supply_chain_inventory_reliability_pipeline")


def test_dagster_repository_discovery_reports_load_errors() -> None:
    client = DagsterClient(
        "http://dagster/graphql",
        request_json=lambda _query, _variables=None: {
            "data": {
                "workspaceOrError": {
                    "__typename": "Workspace",
                    "locationEntries": [
                        {
                            "name": "openlakeforge-dagster",
                            "locationOrLoadError": {
                                "__typename": "PythonError",
                                "message": "user code unreachable",
                            },
                        }
                    ],
                }
            }
        },
    )

    with pytest.raises(ServiceClientError, match="user code unreachable"):
        client.discover_repository("sales_order_revenue_pipeline")


def test_dagster_launch_uses_discovered_repository() -> None:
    calls: list[dict[str, Any]] = []

    def request_json(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append({"query": query, "variables": variables})
        if "query Workspace" in query:
            return {
                "data": {
                    "workspaceOrError": {
                        "__typename": "Workspace",
                        "locationEntries": [
                            {
                                "name": "openlakeforge-dagster",
                                "locationOrLoadError": {
                                    "__typename": "RepositoryLocation",
                                    "repositories": [
                                        {
                                            "name": "__repository__",
                                            "pipelines": [{"name": "sales_order_revenue_pipeline"}],
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                }
            }
        return {"data": {"launchRun": {"__typename": "LaunchRunSuccess", "run": {"runId": "run-1"}}}}

    client = DagsterClient("http://dagster/graphql", request_json=request_json)

    assert client.launch("sales_order_revenue_pipeline") == "run-1"
    selector = calls[-1]["variables"]["executionParams"]["selector"]
    assert selector == {
        "repositoryLocationName": "openlakeforge-dagster",
        "repositoryName": "__repository__",
        "pipelineName": "sales_order_revenue_pipeline",
    }


def test_dagster_wait_for_repository_retries_until_job_is_available() -> None:
    responses = [
        {
            "data": {
                "workspaceOrError": {
                    "__typename": "Workspace",
                    "locationEntries": [
                        {
                            "name": "openlakeforge-dagster",
                            "locationOrLoadError": {
                                "__typename": "PythonError",
                                "message": "user code unreachable",
                            },
                        }
                    ],
                }
            }
        },
        {
            "data": {
                "reloadRepositoryLocation": {
                    "__typename": "WorkspaceLocationEntry",
                    "name": "openlakeforge-dagster",
                }
            }
        },
        {
            "data": {
                "workspaceOrError": {
                    "__typename": "Workspace",
                    "locationEntries": [
                        {
                            "name": "openlakeforge-dagster",
                            "locationOrLoadError": {
                                "__typename": "RepositoryLocation",
                                "repositories": [
                                    {
                                        "name": "__repository__",
                                        "jobs": [{"name": "sales_order_revenue_pipeline"}],
                                    }
                                ],
                            },
                        }
                    ],
                }
            }
        },
    ]

    def request_json(_query: str, _variables: dict[str, Any] | None = None) -> dict[str, Any]:
        return responses.pop(0)

    client = DagsterClient(
        "http://dagster/graphql",
        expected_location_names=["openlakeforge-dagster"],
        request_json=request_json,
    )

    assert client.wait_for_repository("sales_order_revenue_pipeline", timeout_seconds=1, delay=0) == (
        "openlakeforge-dagster",
        "__repository__",
    )


def test_expected_repository_location_names_reads_terraform_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        _dagster,
        "terraform_output_json",
        lambda _dir, name: ["openlakeforge-dagster"] if name == "dagster_code_location_names" else None,
    )

    assert _dagster.expected_repository_location_names(e2e_cfg(tmp_path)) == ["openlakeforge-dagster"]


def test_expected_repository_location_names_accepts_split_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    location_names = ["domain-a", "domain-b"]
    monkeypatch.setattr(_dagster, "terraform_output_json", lambda _dir, _name: location_names)

    assert _dagster.expected_repository_location_names(e2e_cfg(tmp_path)) == location_names


@pytest.mark.parametrize(
    "location_names",
    [[], ["openlakeforge-dagster", 1], ["location-a", "location-a"], "openlakeforge-dagster"],
)
def test_expected_repository_location_names_rejects_invalid_terraform_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, location_names: object
) -> None:
    monkeypatch.setattr(_dagster, "terraform_output_json", lambda _dir, _name: location_names)

    with pytest.raises(E2EError, match="non-empty list"):
        _dagster.expected_repository_location_names(e2e_cfg(tmp_path))


def test_dagster_webserver_service_name_selects_the_stage_under_test(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each stage has its own namespace but the same Dagster release name.

    The stage-keyed output still matters: it keeps an E2E run from using the
    Terraform root's selected-stage output when `--stage` names another one.
    """
    monkeypatch.setattr(_dagster, "stage_catalog_name", lambda _cfg: "lakehouse_prod")
    monkeypatch.setattr(
        _dagster,
        "terraform_output_json",
        lambda _dir, _name: {"dev": "dagster-dagster-webserver", "prod": "dagster-dagster-webserver"},
    )

    assert _dagster.dagster_webserver_service_name(e2e_cfg(tmp_path)) == "dagster-dagster-webserver"
    assert _dagster.dagster_release_name(e2e_cfg(tmp_path)) == "dagster"


def test_dagster_webserver_service_name_rejects_a_stage_missing_from_the_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_dagster, "stage_catalog_name", lambda _cfg: "lakehouse_uat")
    monkeypatch.setattr(
        _dagster, "terraform_output_json", lambda _dir, _name: {"dev": "dagster-dev-dagster-webserver"}
    )

    with pytest.raises(E2EError, match="no entry for stage 'uat'"):
        _dagster.dagster_webserver_service_name(e2e_cfg(tmp_path))


def test_expected_user_code_pods_filters_to_configured_locations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_dagster, "stage_catalog_name", lambda _cfg: "lakehouse_dev")
    monkeypatch.setattr(
        _dagster, "terraform_output_json", lambda _dir, _name: {"dev": "dagster-dagster-webserver"}
    )
    monkeypatch.setattr(
        _dagster,
        "kubectl",
        lambda *_args, **_kwargs: json.dumps(
            {
                "items": [
                    {
                        "metadata": {
                            "name": "dagster-dagster-user-deployments-openlakeforge-dagster-abc",
                            "labels": {
                                "app.kubernetes.io/name": "dagster-user-deployments",
                                "app.kubernetes.io/instance": "dagster",
                                "deployment": "openlakeforge-dagster",
                            },
                        }
                    },
                    {
                        "metadata": {
                            "name": "dagster-dagster-user-deployments-sales-dagster-def",
                            "labels": {
                                "app.kubernetes.io/name": "dagster-user-deployments",
                                "app.kubernetes.io/instance": "dagster",
                                "deployment": "sales-dagster",
                            },
                        }
                    },
                    {
                        "metadata": {
                            "name": "dagster-dagster-user-deployments-supply-chain-dagster-ghi",
                            "labels": {
                                "app.kubernetes.io/name": "dagster-user-deployments",
                                "app.kubernetes.io/instance": "dagster",
                                "deployment": "supply-chain-dagster",
                            },
                        }
                    },
                    {
                        "metadata": {
                            "name": "openlakeforge-project-dagster-user-deployments-openlakeforge-dagster-jkl",
                            "labels": {
                                "app.kubernetes.io/name": "dagster-user-deployments",
                                "app.kubernetes.io/instance": "openlakeforge-project",
                                "deployment": "openlakeforge-dagster",
                            },
                        }
                    },
                    {
                        "metadata": {
                            "name": "other-release-user-code",
                            "labels": {
                                "app.kubernetes.io/name": "dagster-user-deployments",
                                "app.kubernetes.io/instance": "other-dagster",
                                "deployment": "openlakeforge-dagster",
                            },
                        }
                    },
                    {"metadata": {"name": "unrelated"}},
                ]
            }
        ),
    )

    assert _dagster.expected_user_code_pods(e2e_cfg(tmp_path), ["openlakeforge-dagster"]) == [
        "dagster-dagster-user-deployments-openlakeforge-dagster-abc",
        "openlakeforge-project-dagster-user-deployments-openlakeforge-dagster-jkl",
    ]
    assert _dagster.expected_user_code_pods(e2e_cfg(tmp_path), ["sales-dagster", "supply-chain-dagster"]) == [
        "dagster-dagster-user-deployments-sales-dagster-def",
        "dagster-dagster-user-deployments-supply-chain-dagster-ghi",
    ]


def test_dagster_client_reloads_every_configured_location() -> None:
    reloaded: list[str] = []

    def request_json(_query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        assert variables is not None
        reloaded.append(variables["repositoryLocationName"])
        return {"data": {"reloadRepositoryLocation": {"__typename": "WorkspaceLocationEntry"}}}

    client = DagsterClient(
        "http://dagster/graphql",
        expected_location_names=["sales-dagster", "supply-chain-dagster"],
        request_json=request_json,
    )

    client.try_reload_repository_locations()

    assert reloaded == ["sales-dagster", "supply-chain-dagster"]


def test_dagster_poll_reports_failure() -> None:
    client = DagsterClient(
        "http://dagster/graphql",
        request_json=lambda _query, _variables=None: {
            "data": {"runOrError": {"__typename": "Run", "status": "FAILURE"}}
        },
    )

    with pytest.raises(ServiceClientError, match="ended with FAILURE"):
        client.poll("sales_order_revenue_pipeline", "run-1", attempts=1, delay=0)


def test_dagster_poll_failure_names_the_failed_steps_error() -> None:
    def request_json(query: str, _variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if "logsForRun" in query:
            events = [
                {},
                {
                    "stepKey": "crm_bronze_resources",
                    "error": {
                        "message": 'Error occurred while executing op "crm_bronze_resources":\n',
                        "cause": {"message": "AccessDenied: PutObject\n", "cause": None},
                    },
                },
            ]
            return {"data": {"logsForRun": {"events": events}}}
        return {"data": {"runOrError": {"__typename": "Run", "status": "FAILURE"}}}

    client = DagsterClient("http://dagster/graphql", request_json=request_json)

    expected = (
        'ended with FAILURE\n  crm_bronze_resources: Error occurred while executing op "crm_bronze_resources":'
        "\n    caused by: AccessDenied: PutObject$"
    )
    with pytest.raises(ServiceClientError, match=expected):
        client.poll("sales_order_revenue_pipeline", "run-1", attempts=1, delay=0)


def test_dagster_poll_retries_transient_graphql_errors() -> None:
    responses: list[Exception | dict[str, Any]] = [
        DagsterTransientError("read timeout"),
        {"data": {"runOrError": {"__typename": "Run", "status": "SUCCESS"}}},
    ]

    def request_json(_query: str, _variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client = DagsterClient("http://dagster/graphql", request_json=request_json)

    client.poll("sales_order_revenue_pipeline", "run-1", attempts=2, delay=0)
    assert responses == []


def test_dagster_launch_retries_transient_failure_and_keeps_launch_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: list[Exception | dict[str, Any]] = [
        DagsterTransientError("HTTP 503"),
        {"data": {"launchRun": {"__typename": "LaunchRunSuccess", "run": {"runId": "run-1"}}}},
    ]
    calls: list[dict[str, Any]] = []

    def request_json(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if "query Workspace" in query:
            return {
                "data": {
                    "workspaceOrError": {
                        "__typename": "Workspace",
                        "locationEntries": [
                            {
                                "name": "openlakeforge-dagster",
                                "locationOrLoadError": {
                                    "__typename": "RepositoryLocation",
                                    "repositories": [
                                        {
                                            "name": "__repository__",
                                            "jobs": [{"name": "sales_order_revenue_pipeline"}],
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                }
            }
        if "ExistingRun" in query:
            return {"data": {"runsOrError": {"__typename": "Runs", "results": []}}}
        calls.append(variables or {})
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(dagster_client_module, "LAUNCH_RETRY_DELAY_SECONDS", 0)
    run_id = DagsterClient("http://dagster/graphql", request_json=request_json).launch(
        "sales_order_revenue_pipeline"
    )
    assert run_id == "run-1"
    assert calls[0]["executionParams"]["executionMetadata"] == calls[1]["executionParams"]["executionMetadata"]
    assert calls[0]["executionParams"]["executionMetadata"]["tags"][0]["key"] == "openlakeforge/e2e-key"
    assert calls[0]["executionParams"].get("tags") is None


def test_dagster_poll_times_out_quickly_for_non_terminal_runs() -> None:
    client = DagsterClient(
        "http://dagster/graphql",
        request_json=lambda _query, _variables=None: {
            "data": {"runOrError": {"__typename": "Run", "status": "STARTED"}}
        },
    )

    with pytest.raises(ServiceClientError, match="did not finish within 1800 seconds"):
        client.poll("sales_order_revenue_pipeline", "run-1", attempts=1, delay=0)


def test_launch_and_poll_dagster_jobs_defaults_to_previous_shell_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    timeouts: list[int] = []
    received_location_names: list[str] = []

    class Client:
        def __init__(self, _url: str, *, expected_location_names: list[str]) -> None:
            received_location_names.extend(expected_location_names)

        def launch(self, _job: str) -> str:
            return "run-1"

        def poll(self, _job: str, _run_id: str, *, timeout_seconds: int) -> None:
            timeouts.append(timeout_seconds)

    monkeypatch.delenv("DAGSTER_JOB_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(_dagster, "DagsterClient", Client)
    monkeypatch.setattr(_dagster, "expected_repository_location_names", lambda _cfg: ["openlakeforge-dagster"])
    monkeypatch.setattr(_dagster, "stage_catalog_name", lambda _cfg: "lakehouse_dev")
    monkeypatch.setattr(
        _dagster, "terraform_output_json", lambda _dir, _name: {"dev": "dagster-dagster-webserver"}
    )
    monkeypatch.setattr(_dagster.k8s, "http_wait", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        _dagster.k8s,
        "port_forward",
        lambda *_args, **_kwargs: __import__("contextlib").nullcontext(),
    )

    local_cfg = E2EConfig(
        env="local",
        suite="full",
        namespace="lakehouse",
        kube_context="kind-openlakeforge-local",
        repo_root=tmp_path,
        distribution_root=tmp_path,
        foundation_terraform_dir=tmp_path / "foundation",
        contract_terraform_dir=tmp_path / "contract",
        inventory=E2E_INVENTORY,
        dagster_local_port=13000,
    )

    _dagster.launch_and_poll_dagster_jobs(local_cfg)

    assert timeouts == [_dagster.DAGSTER_JOB_TIMEOUT_SECONDS] * len(E2E_INVENTORY.job_names)
    assert received_location_names == ["openlakeforge-dagster"]
