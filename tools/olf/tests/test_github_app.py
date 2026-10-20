from __future__ import annotations

import base64
import html
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from olf import github_app
from olf.cli import app
from olf.profile import AccessSpec, GitHubOAuthRef, GitHubSpec, IdentitySpec

CLIENT_SECRET, PEM = "s3cr3t-client-secret", "-----BEGIN RSA PRIVATE KEY-----\nkey-material\n"


class _Stay(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:
        return None  # surface the 302 as an HTTPError


_no_redirects = urllib.request.build_opener(_Stay)


def _get(url: str) -> tuple[int, str]:
    try:
        with _no_redirects.open(url, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def test_manifest_content() -> None:
    manifest = github_app.build_manifest(
        name="openlakeforge-" + "x" * 40,
        callback_url="http://auth.olf.localhost/callback",
        redirect_url="http://127.0.0.1:1/done",
        homepage="http://olf.localhost",
    )
    assert len(manifest["name"]) == 34
    assert manifest["callback_urls"] == ["http://auth.olf.localhost/callback"]
    assert manifest["public"] is False and "hook_attributes" not in manifest
    assert manifest["default_permissions"] == {"members": "read", "emails": "read"}


def test_the_state_is_checked_and_the_code_returned() -> None:
    seen: dict[str, str] = {}
    threads: list[threading.Thread] = []

    def browser(url: str) -> None:
        def run() -> None:
            _, page = _get(url)
            seen["form"] = page
            state = re.search(r"state=([\w-]+)", page).group(1)  # type: ignore[union-attr]
            done = url + "done"
            seen["wrong"] = str(_get(f"{done}?code=evil&state=nope")[0])
            seen["redirect"] = _get(f"{done}?code=good&state={state}")[0]

        threads.append(threading.Thread(target=run))
        threads[-1].start()

    nxt = github_app.wait_for_code(
        host="github.com",
        organization="acme",
        open_url=browser,
        manifest_for=lambda redirect: github_app.build_manifest(
            name="n", callback_url="c", redirect_url=redirect, homepage="h"
        ),
        on_code=lambda code: "http://127.0.0.1:1/install" if code == "good" else "bad",
    )
    # wait_for_code returns once the server has answered; the browser thread
    # records that answer only after, so read `seen` once it has finished.
    threads[0].join(timeout=10)
    assert nxt == "http://127.0.0.1:1/install" and seen["wrong"] == "404"
    assert seen["redirect"] == 302
    assert "https://github.com/organizations/acme/settings/apps/new?state=" in seen["form"]
    assert "127.0.0.1" in html.unescape(seen["form"])


def test_a_timeout_writes_nothing() -> None:
    with pytest.raises(github_app.GitHubConnectError, match="nothing was written"):
        github_app.wait_for_code(
            host="github.com",
            organization="acme",
            manifest_for=lambda _r: {},
            open_url=lambda _u: None,
            on_code=lambda _c: "",
            timeout=0.2,
        )


def test_a_failed_conversion_shows_an_error_page_and_is_raised() -> None:
    seen: dict[str, Any] = {}

    def browser(url: str) -> None:
        def run() -> None:
            state = re.search(r"state=([\w-]+)", _get(url)[1]).group(1)  # type: ignore[union-attr]
            seen["got"] = _get(f"{url}done?code=c&state={state}")

        seen["thread"] = thread = threading.Thread(target=run)
        thread.start()

    def boom(_code: str) -> str:
        raise github_app.GitHubConnectError("GitHub rejected the manifest code")

    with pytest.raises(github_app.GitHubConnectError, match="rejected"):
        github_app.wait_for_code(
            host="github.com", organization="acme", manifest_for=lambda _r: {}, open_url=browser, on_code=boom
        )
    seen["thread"].join()
    assert seen["got"][0] == 500


def test_connect_stores_the_secret_on_stdin_and_prints_no_credential(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    applied: list[str] = []
    kubectl = SimpleNamespace(apply_from_manifest=lambda manifest, **kw: applied.append(manifest))
    github = GitHubSpec("acme", {}, GitHubOAuthRef(name="github-oauth-app"))
    topology = SimpleNamespace(
        profile_name="demo",
        access=AccessSpec(base_domain="olf.localhost", issuer="none"),
        identity=IdentitySpec(issuer="github", github=github),
    )
    context = SimpleNamespace(
        topology=topology,
        shared_namespace="olf-system",
        kube_context="kind-olf",
        paths=SimpleNamespace(kubeconfig_path=tmp_path / "kc"),
        command_env=lambda base: {},
    )
    seen: dict[str, Any] = {}

    def wait(**kw: Any) -> str:
        seen["manifest"] = kw["manifest_for"]("http://127.0.0.1:9/done")
        return str(kw["on_code"]("code123"))

    monkeypatch.setattr("olf.commands.github.deployment_context", lambda *a, **k: context)
    monkeypatch.setattr("olf.deployment.engine.Toolkit.default", lambda **k: SimpleNamespace(kubectl=kubectl))
    monkeypatch.setattr("olf.deployment.kube_ops.namespace_exists", lambda *a, **k: True)
    monkeypatch.setattr(github_app, "wait_for_code", wait)
    monkeypatch.setattr(
        github_app,
        "convert_code",
        lambda code, host: {"client_id": "Iv1.abc", "client_secret": CLIENT_SECRET, "pem": PEM, "slug": "olf-demo"},
    )
    result = CliRunner().invoke(app, ["github", "connect", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert seen["manifest"]["callback_urls"] == ["http://auth.olf.localhost/callback"]
    (secret,) = applied
    data = {k: base64.b64decode(v).decode() for k, v in json.loads(secret)["data"].items()}
    assert data == {"client-id": "Iv1.abc", "client-secret": CLIENT_SECRET, "private-key": PEM}
    assert "https://github.com/apps/olf-demo/installations/new" in result.output
    everything = result.output + capsys.readouterr().out + caplog.text
    for value in ("Iv1.abc", CLIENT_SECRET, "key-material", "code123"):
        assert value not in everything
