from __future__ import annotations

from pathlib import Path

import pytest

from olf.common.tooling.process import CommandResult
from olf.infra import trino


def _result(command: list[str], returncode: int, *, stdout: str, stderr: str) -> CommandResult:
    return CommandResult(tuple(command), returncode, stdout, stderr, 0.0)


def _target(**overrides: str) -> trino.TrinoTarget:
    fields = {
        "provider": "local",
        "kube_context": "kind-openlakeforge-local",
        "namespace": "olf-system",
        "catalog": "lakehouse_prod",
        "stage": "prod",
    }
    return trino.TrinoTarget(**{**fields, **overrides})


def test_stage_catalog_comes_from_the_applied_contract_and_defaults_to_iceberg() -> None:
    assert trino.stage_catalog_name({"OPENLAKEFORGE_QUERY_TRINO_CATALOG": "lakehouse_uat"}) == "lakehouse_uat"
    assert trino.stage_catalog_name({}) == "iceberg"


def test_runtime_user_follows_the_stage_not_the_namespace() -> None:
    assert _target(namespace="lakehouse").user == "olf-prod-runtime"


def test_only_aws_prefixes_glue_namespaces() -> None:
    assert trino.glue_namespace_prefix("aws", "iceberg") == "iceberg_"
    assert trino.glue_namespace_prefix("local", "lakehouse_dev") == ""


def test_exec_args_set_the_stage_catalog_as_the_session_default() -> None:
    args = trino.exec_args(_target(), "SELECT 1", output_format="ALIGNED")

    assert args[args.index("--catalog") + 1] == "lakehouse_prod"
    assert args[args.index("--user") + 1] == "olf-prod-runtime"
    assert args[args.index("-n") + 1] == "olf-system"
    assert args[-2:] == ["--execute", "SELECT 1"]


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Query failed: Access Denied: Cannot select from table x", "denied the query for olf-prod-runtime"),
        ("Query failed: Catalog 'lakehouse_prod' does not exist", "does not look deployed"),
        ('Error from server (NotFound): deployments.apps "trino-coordinator" not found', "does not look deployed"),
        ("Query failed: line 1:8: Column 'x' cannot be resolved", "Column 'x' cannot be resolved"),
    ],
)
def test_run_sql_explains_failures(stderr: str, expected: str) -> None:
    def runner(command, stdin):  # noqa: ANN001, ANN202
        return _result(command, 1, stdout="", stderr=stderr)

    with pytest.raises(trino.TrinoQueryError, match=expected):
        trino.run_sql(_target(), "SELECT 1", output_format="ALIGNED", kubectl="kubectl", runner=runner)


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("Query failed: line 1:8: Column 'x' cannot be resolved", "", "Column 'x' cannot be resolved"),
        ("", "", "the query failed without any output."),
    ],
)
def test_run_sql_falls_back_when_stderr_is_empty(stdout: str, stderr: str, expected: str) -> None:
    def runner(command, stdin):  # noqa: ANN001, ANN202, ARG001
        return _result(command, 1, stdout=stdout, stderr=stderr)

    with pytest.raises(trino.TrinoQueryError) as exc_info:
        trino.run_sql(_target(), "SELECT 1", output_format="ALIGNED", kubectl="kubectl", runner=runner)

    assert expected in str(exc_info.value)


def test_run_sql_returns_output_and_targets_the_kube_context() -> None:
    seen: list[list[str]] = []

    def runner(command, stdin):  # noqa: ANN001, ANN202
        seen.append(list(command))
        return _result(command, 0, stdout="" if "get" in command else "42\n", stderr="")

    assert trino.run_sql(_target(), "SELECT 1", output_format="CSV_HEADER", kubectl="kubectl", runner=runner) == "42\n"
    assert all(command[:3] == ["kubectl", "--context", "kind-openlakeforge-local"] for command in seen)


def test_run_sql_reads_no_secret_and_sends_no_credential_without_a_named_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME", raising=False)
    calls: list[tuple[list[str], str]] = []

    def runner(command, stdin):  # noqa: ANN001, ANN202
        calls.append((list(command), stdin))
        return _result(command, 0, stdout="1\n", stderr="")

    trino.run_sql(_target(), "SELECT 1", output_format="CSV_HEADER", kubectl="kubectl", runner=runner)

    ((query, stdin),) = calls
    assert "get" not in query
    assert stdin == "\n\n"  # no processing credential: no token


def test_run_sql_sends_the_stage_processing_credential_on_stdin_never_in_the_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    monkeypatch.setenv("OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME", "polaris-prod-processing-creds")
    calls: list[tuple[list[str], str]] = []

    def runner(command, stdin):  # noqa: ANN001, ANN202
        calls.append((list(command), stdin))
        encoded = "\n".join(base64.b64encode(value).decode() for value in (b"the-id", b"the-secret"))
        return _result(command, 0, stdout=encoded if "get" in command else "1\n", stderr="")

    trino.run_sql(_target(), "SELECT 1", output_format="CSV_HEADER", kubectl="kubectl", runner=runner)

    (lookup, _), (query, stdin) = calls
    assert "polaris-prod-processing-creds" in lookup
    assert stdin == "the-id\nthe-secret\n"
    assert "the-secret" not in " ".join(query)


def test_the_pod_script_runs_the_cli_on_its_plain_listener_with_the_callers_arguments(tmp_path: Path) -> None:
    import subprocess as sp

    (tmp_path / "trino").write_text('#!/bin/sh\nprintf "%s|%s\\n" "$TRINO_PASSWORD" "$*"\n')
    (tmp_path / "trino").chmod(0o755)

    out = sp.run(
        ["sh", "-c", trino.cli_script(), "trino", "--user", "u"],
        input="pw\n",
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
    ).stdout.strip()

    # Without a stage there is nothing to mint: stdin is not read, no password or HTTPS listener is chosen.
    assert out == "|--user u"


def test_run_sql_stops_when_the_processing_credential_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME", "polaris-prod-processing-creds")
    calls: list[list[str]] = []

    def runner(command, stdin):  # noqa: ANN001, ANN202, ARG001
        calls.append(list(command))
        return _result(command, 1, stdout="", stderr="forbidden")

    with pytest.raises(trino.TrinoQueryError, match=r"(?s)processing credential in namespace olf-system.*RBAC"):
        trino.run_sql(_target(), "SELECT 1", output_format="CSV_HEADER", kubectl="kubectl", runner=runner)
    assert len(calls) == 1


def test_run_sql_reads_the_processing_credential_secret_from_the_given_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME", "from-os-environ")
    calls: list[list[str]] = []

    def runner(command, stdin):  # noqa: ANN001, ANN202, ARG001
        calls.append(list(command))
        return _result(command, 0, stdout="1\n", stderr="")

    trino.run_sql(_target(), "SELECT 1", output_format="CSV_HEADER", kubectl="kubectl", runner=runner, environ={})

    assert len(calls) == 1 and "get" not in calls[0]


def test_default_runner_gives_the_kubectl_child_the_given_environ(tmp_path: Path) -> None:
    script = tmp_path / "kubectl"
    script.write_text('#!/bin/sh\necho "kc=$KUBECONFIG"\n')
    script.chmod(0o755)

    out = trino.run_sql(
        _target(), "SELECT 1", output_format="CSV_HEADER", kubectl=str(script), environ={"KUBECONFIG": "/given"}
    )

    assert out == "kc=/given\n"


def test_service_namespace_follows_the_stage_endpoint_host() -> None:
    dev = {"OPENLAKEFORGE_QUERY_TRINO_HOST": "trino-dev.olf-dev"}
    prod = {"OPENLAKEFORGE_QUERY_TRINO_HOST": "trino-prod.olf-prod.svc.cluster.local"}

    assert trino.service_namespace(dev, "olf-system") == "olf-dev"
    assert trino.service_namespace(prod, "olf-system") == "olf-prod"


@pytest.mark.parametrize("environ", [{}, {"OPENLAKEFORGE_QUERY_TRINO_HOST": "trino"}])
def test_service_namespace_falls_back_without_a_namespaced_host(environ: dict[str, str]) -> None:
    assert trino.service_namespace(environ, "olf-system") == "olf-system"


def test_service_and_namespace_come_from_each_stages_contract_endpoint() -> None:
    contracts = {
        "stages": {
            "dev": {"query": {"endpoint": "http://trino-dev.olf-dev:8080"}},
            "prod": {"query": {"endpoint": "http://trino-prod.olf-prod.svc.cluster.local:8080"}},
        }
    }

    assert trino.contract_service(contracts, "dev", namespace="olf-system") == ("trino-dev", "olf-dev")
    assert trino.contract_service(contracts, "prod", namespace="olf-system") == ("trino-prod", "olf-prod")
    assert trino.contract_service(None, "dev", namespace="olf-system") == ("trino", "olf-system")


def test_exec_runs_in_the_coordinator_of_the_targets_service() -> None:
    target = trino.TrinoTarget("local", "ctx", "olf-prod", "lakehouse_prod", "prod", service="trino-prod")

    args = trino.exec_args(target, "select 1", output_format="CSV")

    assert args[args.index("-n") + 1 : args.index("--")] == ["olf-prod", "deploy/trino-prod-coordinator"]


def test_the_pod_mints_the_stage_token_and_fails_the_query_when_it_cannot(tmp_path: Path) -> None:
    import subprocess as sp

    (tmp_path / "trino").write_text('#!/bin/sh\nprintf "%s\\n" "$*"\n')
    # Echoes the config curl was given on stdin to a file, so the test can see the secret never rode an argument.
    (tmp_path / "curl").write_text(
        '#!/bin/sh\ncat > "$0.stdin"\n[ -f "$0.fail" ] && exit 22\necho \'{"access_token":"polaris-token","x":1}\'\n'
    )
    for tool in ("trino", "curl"):
        (tmp_path / tool).chmod(0o755)
    (tmp_path / "lakehouse_dev.properties").write_text(
        "iceberg.rest-catalog.oauth2.server-uri=http://polaris/tokens\niceberg.rest-catalog.oauth2.scope=S\n"
    )
    (tmp_path / "glue.properties").write_text("iceberg.catalog.type=glue\n")

    def run(catalog: str, credential: str = "the-id\nthe-secret\n") -> sp.CompletedProcess[str]:
        script = trino.cli_script(stage="dev", catalog=catalog)
        script = script.replace(trino.TRINO_CATALOG_DIR, str(tmp_path))
        # The coordinator holds no credential with a grant: the caller's stdin carries it.
        return sp.run(
            ["sh", "-c", script, "trino", "--user", "u"],
            input=credential,
            capture_output=True,
            text=True,
            env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        )

    assert run("lakehouse_dev", trino.decode_credential("")).stdout.strip() == "--user u"  # no credential, no token
    ok = run("lakehouse_dev")
    assert ok.stdout.strip() == "--extra-credential token=polaris-token --user u"
    sent = (tmp_path / "curl.stdin").read_text()
    assert "client_secret=the-secret" in sent and "http://polaris/tokens" in sent
    assert run("glue").stdout.strip() == "--user u"  # no token endpoint: nothing to send
    assert run("absent").stdout.strip() == "--user u"

    (tmp_path / "curl.fail").touch()
    failed = run("lakehouse_dev")
    assert failed.returncode == 1 and failed.stdout == ""
    assert "cannot mint the Polaris token" in failed.stderr
    assert "the-secret" not in failed.stderr + failed.stdout

    argv = trino.exec_args(_target(stage="dev", catalog="lakehouse_dev"), "SELECT 1", output_format="CSV")
    assert "polaris-token" not in " ".join(argv)
