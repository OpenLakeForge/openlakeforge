from __future__ import annotations

import base64
import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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


class _Response:
    def __init__(self, status: int, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _Trino:
    """A transport that records each request and answers the first with `body`."""

    def __init__(
        self, body: dict[str, Any] | None = None, *, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        self.then: list[_Response] = []  # answers for the requests after the first, in order
        self._answer = _Response(
            status,
            body
            if body is not None
            else {"id": "q1", "columns": [{"name": "c", "type": "integer"}], "data": [[1], [2]]},
            headers,
        )

    def __call__(self, method: str, url: str, *, headers: dict[str, str], data: bytes | None = None, **_: Any):  # noqa: ANN204
        self.requests.append({"method": method, "url": url, "headers": headers, "sql": data})
        return self.then.pop(0) if self.then and len(self.requests) > 1 else self._answer

    @property
    def first(self) -> dict[str, Any]:
        return self.requests[0]


@contextlib.contextmanager
def _forward(_target: trino.TrinoTarget, _environ: object) -> Iterator[int]:
    yield 18080


def _run(trino_service: _Trino, *, runner=None, sql: str = "SELECT 1", **kwargs: Any) -> str:  # noqa: ANN001
    kwargs.setdefault("output_format", "csv")
    return trino.run_sql(
        _target(), sql, kubectl="kubectl", runner=runner, forward=_forward, send=trino_service, **kwargs
    )


def _lookup_runner(password: str):  # noqa: ANN202
    def runner(command, stdin):  # noqa: ANN001, ANN202
        return _result(command, 0, stdout=base64.b64encode(password.encode()).decode(), stderr="")

    return runner


def _basic(headers: dict[str, str]) -> tuple[str, str]:
    scheme, _, encoded = headers["Authorization"].partition(" ")
    assert scheme == "Basic"
    user, _, password = base64.b64decode(encoded).decode().partition(":")
    return user, password


def test_run_sql_sends_the_statement_to_the_forwarded_port_as_the_stage_runtime_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", raising=False)
    service = _Trino()

    assert _run(service) == "c\n1\n2\n"
    assert service.first["url"] == "http://127.0.0.1:18080/v1/statement"
    assert service.first["sql"] == b"SELECT 1"
    assert service.first["headers"]["X-Trino-Catalog"] == "lakehouse_prod"
    assert service.first["headers"]["X-Trino-User"] == "olf-prod-runtime"


def test_run_sql_reads_no_secret_and_sends_no_credential_without_a_named_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", raising=False)
    service = _Trino()

    def runner(command, stdin):  # noqa: ANN001, ANN202
        raise AssertionError(f"kubectl must not run: {command}")

    _run(service, runner=runner)

    assert "Authorization" not in service.first["headers"]


def test_run_sql_authenticates_as_the_principal_with_the_password_from_its_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", "trino-credential")
    service = _Trino()
    looked_up: list[list[str]] = []

    def runner(command, stdin):  # noqa: ANN001, ANN202
        looked_up.append(list(command))
        return _lookup_runner("s3cret")(command, stdin)

    _run(service, runner=runner)

    assert "trino-credential-olf-prod-runtime" in looked_up[0]
    assert looked_up[0][:3] == ["kubectl", "--context", "kind-openlakeforge-local"]
    assert _basic(service.first["headers"]) == ("olf-prod-runtime", "s3cret")
    assert "s3cret" not in service.first["url"]


def test_a_claimed_session_user_is_sent_apart_from_the_authenticated_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", "trino-credential")
    service = _Trino()

    _run(service, runner=_lookup_runner("s3cret"), session_user="olf-dev-runtime")

    assert service.first["headers"]["X-Trino-User"] == "olf-dev-runtime"
    assert _basic(service.first["headers"])[0] == "olf-prod-runtime"


def _no_secret_runner(command, stdin):  # noqa: ANN001, ANN202
    raise AssertionError("an explicit password needs no Secret")


def test_an_explicit_password_replaces_the_one_in_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", "trino-credential")
    service = _Trino()

    _run(service, runner=_no_secret_runner, password="wrong")

    assert _basic(service.first["headers"]) == ("olf-prod-runtime", "wrong")


def test_an_empty_password_sends_no_credential_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", "trino-credential")
    service = _Trino()

    _run(service, runner=_no_secret_runner, password="")

    assert "Authorization" not in service.first["headers"]
    assert service.first["headers"]["X-Trino-User"] == "olf-prod-runtime"


def test_run_sql_stops_when_the_password_secret_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", "trino-credential")
    calls: list[list[str]] = []

    def runner(command, stdin):  # noqa: ANN001, ANN202
        calls.append(list(command))
        return _result(command, 1, stdout="", stderr="forbidden")

    service = _Trino()
    with pytest.raises(trino.TrinoQueryError, match=r"(?s)trino-credential-olf-prod-runtime.*RBAC"):
        _run(service, runner=runner)
    assert len(calls) == 1 and service.requests == []


def test_run_sql_reads_the_credential_secret_from_the_given_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", "from-os-environ")
    service = _Trino()

    def runner(command, stdin):  # noqa: ANN001, ANN202
        raise AssertionError("the given environ names no credential")

    _run(service, runner=runner, environ={})

    assert "Authorization" not in service.first["headers"]


def test_default_runner_gives_the_kubectl_child_the_given_environ(tmp_path: Path) -> None:
    script = tmp_path / "kubectl"
    script.write_text('#!/bin/sh\nprintf "%s" "$KUBECONFIG" | base64\n')
    script.chmod(0o755)
    service = _Trino()

    trino.run_sql(
        _target(),
        "SELECT 1",
        output_format="csv",
        kubectl=str(script),
        environ={"KUBECONFIG": "/given", "OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET": "trino-credential"},
        forward=_forward,
        send=service,
    )

    assert _basic(service.first["headers"]) == ("olf-prod-runtime", "/given")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Access Denied: Cannot select from table x", "denied the query for olf-prod-runtime"),
        ("Catalog 'lakehouse_prod' does not exist", "does not look deployed"),
        ("line 1:8: Column 'x' cannot be resolved", "Column 'x' cannot be resolved"),
    ],
)
def test_run_sql_explains_a_failed_query(monkeypatch: pytest.MonkeyPatch, message: str, expected: str) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", raising=False)
    service = _Trino({"error": {"message": message, "errorName": "X"}})

    with pytest.raises(trino.TrinoQueryError, match=expected):
        _run(service)


def test_run_sql_reports_a_refused_credential_with_the_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", raising=False)

    with pytest.raises(trino.TrinoQueryError, match="401"):
        _run(_Trino(status=401))


def test_run_sql_explains_a_port_forward_that_never_started(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", raising=False)

    @contextlib.contextmanager
    def failing(_target: trino.TrinoTarget, _environ: object) -> Iterator[int]:
        raise trino.k8s.KubectlError("Unable to connect to the server")
        yield 0  # pragma: no cover

    with pytest.raises(trino.TrinoQueryError, match="cannot reach the cluster"):
        trino.run_sql(_target(), "SELECT 1", output_format="csv", kubectl="kubectl", forward=failing, send=_Trino())


@pytest.mark.parametrize(
    ("output_format", "expected"),
    [
        ("bare", "1,x\n2,\n"),
        ("csv", "a,b\n1,x\n2,\n"),
    ],
)
def test_results_render_for_scripts_and_for_people(output_format: str, expected: str) -> None:
    result = trino.statements.TrinoResult(["a", "b"], [[1, "x"], [2, None]])

    assert trino.render(result, output_format) == expected


def test_bare_output_prints_booleans_as_trino_does() -> None:
    result = trino.statements.TrinoResult(["a"], [[True], [False]])

    assert trino.render(result, "bare") == "true\nfalse\n"


def test_the_default_forward_targets_the_coordinator_service_in_its_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    @contextlib.contextmanager
    def port_forward(service: str, remote_port: int, namespace: str, **kwargs: Any) -> Iterator[int]:
        seen.update(service=service, remote_port=remote_port, namespace=namespace, **kwargs)
        yield 4242

    monkeypatch.setattr(trino.k8s, "port_forward", port_forward)
    environ = {"KUBECONFIG": "/given"}

    with trino._port_forward(_target(), environ) as port:
        assert port == 4242

    assert (seen["service"], seen["remote_port"], seen["namespace"]) == ("trino", 8080, "olf-system")
    assert seen["kube_context"] == "kind-openlakeforge-local"
    assert seen["environ"] is environ


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", ["SELECT 1"]),
        ("SELECT 1;", ["SELECT 1"]),
        ("SELECT 1; SELECT 2;\n", ["SELECT 1", "SELECT 2"]),
        ("SELECT 'a;b'; SELECT 2", ["SELECT 'a;b'", "SELECT 2"]),
        ("SELECT 'it''s;'; SELECT 2", ["SELECT 'it''s;'", "SELECT 2"]),
        ('SELECT "a;b" FROM t; SELECT 2', ['SELECT "a;b" FROM t', "SELECT 2"]),
        ("SELECT 1 -- one; two\n; SELECT 2", ["SELECT 1 -- one; two", "SELECT 2"]),
        ("SELECT /* a; b */ 1; SELECT 2", ["SELECT /* a; b */ 1", "SELECT 2"]),
        (";;  ;", []),
        ("-- only a comment\n", []),
        ("SELECT 1;\n-- trailing comment", ["SELECT 1"]),
    ],
)
def test_statements_split_outside_quotes_and_comments(sql: str, expected: list[str]) -> None:
    assert trino.split_statements(sql) == expected


def test_run_sql_runs_each_statement_and_prints_the_ones_that_return_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", raising=False)
    service = _Trino({"id": "q0"})  # CREATE SCHEMA returns no columns
    service.then = [_Response(200, {"id": "q1", "columns": [{"name": "c", "type": "integer"}], "data": [[1], [2]]})]

    output = _run(service, sql="CREATE SCHEMA x; SELECT 1;", output_format="bare")

    assert [request["sql"] for request in service.requests] == [b"CREATE SCHEMA x", b"SELECT 1"]
    assert output == "1\n2\n"


def test_the_default_transport_ignores_environment_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    """The password rides this request, so a workstation proxy must never see it."""
    import http.server
    import threading

    def server(hits: list[str]) -> http.server.HTTPServer:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                hits.append(self.path)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                pass

        listener = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        return listener

    direct: list[str] = []
    via_proxy: list[str] = []
    target, proxy = server(direct), server(via_proxy)
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_port}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    try:
        response = trino._loopback_session().request(
            "GET", f"http://127.0.0.1:{target.server_port}/v1/info", headers={"Authorization": "Basic secret"}
        )
    finally:
        for listener in (target, proxy):
            listener.shutdown()
            listener.server_close()

    assert response.status_code == 204
    assert (direct, via_proxy) == (["/v1/info"], [])


def test_a_use_statement_applies_to_the_statements_after_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET", raising=False)
    service = _Trino({"id": "q0"}, headers={"X-Trino-Set-Schema": "order_revenue_gold"})
    service.then = [_Response(200, {"id": "q1", "columns": [{"name": "c", "type": "integer"}], "data": [[1]]})]

    output = _run(service, sql="USE order_revenue_gold; SELECT 1", output_format="bare")

    assert output == "1\n"
    assert "X-Trino-Schema" not in service.requests[0]["headers"]
    assert service.requests[1]["headers"]["X-Trino-Schema"] == "order_revenue_gold"
