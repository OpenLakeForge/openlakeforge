from __future__ import annotations

import base64
import contextlib
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

    def browser(url: str) -> None:
        def run() -> None:
            _, page = _get(url)
            seen["form"] = page
            state = re.search(r"state=([\w-]+)", page).group(1)  # type: ignore[union-attr]
            done = url + "done"
            seen["wrong"] = str(_get(f"{done}?code=evil&state=nope")[0])
            seen["redirect"] = _get(f"{done}?code=good&state={state}")[0]

        threading.Thread(target=run).start()

    nxt = github_app.wait_for_code(
        host="github.com",
        organization="acme",
        open_url=browser,
        manifest_for=lambda redirect: github_app.build_manifest(
            name="n", callback_url="c", redirect_url=redirect, homepage="h"
        ),
        on_code=lambda code: "http://127.0.0.1:1/install" if code == "good" else "bad",
    )
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
        lambda code, host: {
            "id": 42,
            "client_id": "Iv1.abc",
            "client_secret": CLIENT_SECRET,
            "pem": PEM,
            "slug": "olf-demo",
        },
    )
    result = CliRunner().invoke(app, ["github", "connect", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert seen["manifest"]["callback_urls"] == ["http://auth.olf.localhost/callback"]
    (secret,) = applied
    data = {k: base64.b64decode(v).decode() for k, v in json.loads(secret)["data"].items()}
    assert data == {
        "client-id": "Iv1.abc",
        "client-secret": CLIENT_SECRET,
        "private-key": PEM,
        "app-id": "42",
        "app-slug": "olf-demo",
    }
    assert "https://github.com/apps/olf-demo/installations/new" in result.output
    everything = result.output + capsys.readouterr().out + caplog.text
    for value in ("Iv1.abc", CLIENT_SECRET, "key-material", "code123", "BEGIN RSA"):
        assert value not in everything


def _rsa_pem() -> tuple[str, Any]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )
    return pem.decode(), key.public_key()


def test_app_jwt_is_an_rs256_token_issued_by_the_app_and_valid_for_ten_minutes() -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    pem, public = _rsa_pem()
    head, claims, signature = github_app.app_jwt(42, pem, now=1_000_000).split(".")
    pad = lambda part: part + "=" * (-len(part) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(head))) == {"alg": "RS256", "typ": "JWT"}
    payload = json.loads(base64.urlsafe_b64decode(pad(claims)))
    assert payload["iss"] == "42" and payload["exp"] - payload["iat"] == 600
    signed = f"{head}.{claims}".encode()
    public.verify(base64.urlsafe_b64decode(pad(signature)), signed, padding.PKCS1v15(), hashes.SHA256())


@pytest.mark.parametrize(("status", "expected"), [(200, True), (404, False), (401, None), (500, None)])
def test_app_installed_maps_the_installation_lookup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: int, expected: bool | None
) -> None:
    pem, _ = _rsa_pem()
    seen: dict[str, Any] = {}

    def urlopen(request: urllib.request.Request, timeout: float) -> Any:
        seen["url"], seen["auth"] = request.full_url, request.get_header("Authorization")
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "x", {}, None)  # type: ignore[arg-type]
        return contextlib.nullcontext()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert github_app.app_installed(host="github.com", organization="acme", app_id=42, pem=pem) is expected
    assert seen["url"] == "https://api.github.com/orgs/acme/installation"
    out = capsys.readouterr()
    assert seen["auth"].removeprefix("Bearer ") not in out.out + out.err


def test_an_unreadable_key_or_network_failure_is_unknown_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    assert github_app.app_installed(host="github.com", organization="acme", app_id=1, pem="not a key") is None
    pem, _ = _rsa_pem()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert github_app.app_installed(host="github.com", organization="acme", app_id=1, pem=pem) is None


@pytest.mark.parametrize(
    ("release", "explorer", "expected"),
    [
        ("5.15.0-microsoft-standard-WSL2", "/mnt/c/Windows/explorer.exe", "explorer"),
        ("5.15.0-microsoft-standard-WSL2", None, "nothing"),
        ("6.1.0-generic", "/mnt/c/Windows/explorer.exe", "webbrowser"),
    ],
)
def test_open_browser_uses_explorer_on_wsl_and_webbrowser_elsewhere(
    monkeypatch: pytest.MonkeyPatch, release: str, explorer: str | None, expected: str
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(github_app.platform, "release", lambda: release)
    monkeypatch.setattr(github_app.shutil, "which", lambda _name: explorer)
    monkeypatch.setattr(github_app.subprocess, "Popen", lambda argv, **k: opened.append(f"explorer {argv[1]}"))
    monkeypatch.setattr(github_app.webbrowser, "open", lambda url: opened.append(f"webbrowser {url}"))

    github_app.open_browser("http://127.0.0.1:1/")

    assert opened == {
        "explorer": ["explorer http://127.0.0.1:1/"],
        "nothing": [],
        "webbrowser": ["webbrowser http://127.0.0.1:1/"],
    }[expected]
    opened.clear()
    github_app.open_browser("http://127.0.0.1:1/", no_browser=True)
    assert opened == []
