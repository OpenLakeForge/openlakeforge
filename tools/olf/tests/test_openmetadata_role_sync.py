from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from olf import k8s
from olf.openmetadata import _role_sync
from olf.openmetadata import role_sync_script as script
from olf.profile import GitHubOAuthRef, GitHubSpec

GITHUB = GitHubSpec(
    organization="acme",
    teams={"platform-admin": ("admins",), "data-engineer": ("eng",), "analyst": ("analysts",), "viewer": ("eng",)},
    oauth_app_secret_ref=GitHubOAuthRef(name="github-app"),
)


class FakeOpenMetadata:
    """In-memory users; records PATCHes so tests assert the resulting state, not the calls."""

    def __init__(self, users: list[dict]) -> None:
        self.users = {u["id"]: u for u in users}

    def __call__(self, method: str, url: str, token: str, body=None, content_type: str = ""):  # noqa: ANN001, ANN204
        if "/roles/name/" in url:
            return {"id": "role-ds"}, ""
        if method == "GET":
            return {"data": list(self.users.values()), "paging": {}}, ""
        user = self.users[url.rsplit("/", 1)[1]]
        assert content_type == "application/json-patch+json"
        for op in body:
            if op["path"] == "/isAdmin":
                user["isAdmin"] = op["value"]
            else:
                names = {"role-ds": "DataSteward"}
                user["roles"] = [{"id": r["id"], "name": names.get(r["id"], "Other")} for r in op["value"]]
        return {}, ""


def _user(name: str, *, admin: bool = False, write: bool = False, bot: bool = False) -> dict:
    roles = [{"id": "role-ds", "name": "DataSteward"}] if write else []
    return {"id": f"id-{name}", "name": name, "isAdmin": admin, "isBot": bot, "roles": roles}


def test_reconcile_grants_demotes_and_revokes() -> None:
    om = FakeOpenMetadata(
        [
            _user("alice"),  # promoted
            _user("bob", admin=True),  # moved to analysts: demoted
            _user("carol", write=True),  # left every team: revoked
            _user("dave", write=True),  # stays data-engineer
            _user("admin", admin=True),  # local admin: untouched
            _user("ingestion-bot", admin=True, bot=True),
        ]
    )
    held = {"alice": "platform-admin", "bob": "analyst", "dave": "data-engineer"}

    changed = script.reconcile("http://om", "t", held, om)

    assert sorted(changed) == ["alice", "bob", "carol"]
    state = {u["name"]: (u["isAdmin"], [r["name"] for r in u["roles"]]) for u in om.users.values()}
    assert state == {
        "alice": (True, []),
        "bob": (False, []),
        "carol": (False, []),
        "dave": (False, ["DataSteward"]),
        "admin": (True, []),
        "ingestion-bot": (True, []),
    }
    assert script.reconcile("http://om", "t", held, om) == []  # converged: a second run changes nothing


def test_team_roles_takes_the_highest_role_and_follows_pagination() -> None:
    pages = {
        "https://gh/orgs/acme/teams/admins/members?per_page=100": ([{"login": "Alice"}], ""),
        "https://gh/orgs/acme/teams/eng/members?per_page=100": (
            [{"login": "alice"}, {"login": "Bob"}],
            '<https://gh/page2>; rel="next"',
        ),
        "https://gh/page2": ([{"login": "Carol"}], ""),
    }
    teams = {"platform-admin": ["admins"], "viewer": ["eng"], "data-engineer": []}

    held = script.team_roles("https://gh", "tok", "acme", teams, lambda _m, url, _t, *_a: pages[url])

    assert held == {"alice": "platform-admin", "bob": "viewer", "carol": "viewer"}


def test_app_jwt_is_a_verifiable_rs256_token() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()

    head, claims, signature = script._app_jwt("123", pem, now=1000).split(".")

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    def pad(part: str) -> bytes:
        import base64

        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    key.public_key().verify(pad(signature), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(pad(claims)) == {"iat": 940, "exp": 1540, "iss": "123"}


def test_installation_token_mints_from_the_org_installation() -> None:
    calls: list[tuple[str, str]] = []

    def http(method: str, url: str, _token: str, *_a):  # noqa: ANN002, ANN202
        calls.append((method, url))
        return ({"id": 7} if method == "GET" else {"token": "inst"}), ""

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()

    assert script.installation_token("https://gh", "acme", "1", pem, http) == "inst"
    assert calls == [("GET", "https://gh/orgs/acme/installation"), ("POST", "https://gh/app/installations/7/access_tokens")]


def test_main_names_what_is_missing_without_leaking_values() -> None:
    with pytest.raises(SystemExit) as exc:
        script.main({"GITHUB_ORG": "acme", "OM_JWT": "secret-token"})
    assert "APP_ID" in str(exc.value)
    assert "secret-token" not in str(exc.value)


def test_manifests_carry_secret_references_not_values() -> None:
    configmap, cronjob = _role_sync.role_sync_manifests(
        GITHUB, image="img@sha256:x", namespace="olf-system", om_url="http://openmetadata:8585"
    )

    assert "def reconcile" in configmap["data"]["role_sync.py"]
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["PRIVATE_KEY"]["valueFrom"]["secretKeyRef"] == {"name": "github-app", "key": "private-key"}
    assert env["OM_JWT"]["valueFrom"]["secretKeyRef"]["key"] == "OPENMETADATA_AUTOMATION_JWT"
    assert env["GITHUB_API"]["value"] == "https://api.github.com"
    assert json.loads(env["ROLE_TEAMS"]["value"])["data-engineer"] == ["eng"]
    assert cronjob["spec"]["schedule"] == "*/5 * * * *"
    assert cronjob["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"] == {
        "openlakeforge.io/job": "openmetadata-role-sync"
    }


def test_enterprise_host_selects_its_api_prefix() -> None:
    assert _role_sync.github_api("ghe.example.com") == "https://ghe.example.com/api/v3"


def _apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity, app_id: str | None) -> tuple[str, list[dict]]:  # noqa: ANN001
    applied: list[dict] = []
    (tmp_path / "openlakeforge.yaml").write_text("")
    monkeypatch.setenv("OPENLAKEFORGE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "olf.profile.load_deployment_profile", lambda *_a, **_k: SimpleNamespace(identity=identity)
    )
    monkeypatch.setattr(k8s, "apply_manifest", applied.append)

    def secret(*_args: object, **_kwargs: object) -> str:
        if app_id is None:
            raise k8s.KubectlError("not found")
        return app_id

    monkeypatch.setattr(k8s, "secret_value", secret)
    return _role_sync.apply_role_sync("olf-system", om_url="http://om"), applied


def test_no_sync_without_a_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLAKEFORGE_PROJECT_ROOT", str(tmp_path))
    assert "no Deployment Profile" in _role_sync.apply_role_sync("olf-system", om_url="http://om")


@pytest.mark.parametrize("identity", [None, SimpleNamespace(github=None)])  # authentication off; issuer external
def test_no_sync_for_auth_off_or_external_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: object
) -> None:
    message, applied = _apply(tmp_path, monkeypatch, identity, "1")
    assert "no role sync" in message
    assert applied == []


def test_no_sync_for_a_hand_made_oauth_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    message, applied = _apply(tmp_path, monkeypatch, SimpleNamespace(github=GITHUB), None)
    assert "app-id" in message
    assert applied == []


def test_github_issuer_applies_the_script_and_the_cronjob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, applied = _apply(tmp_path, monkeypatch, SimpleNamespace(github=GITHUB), "1")
    assert [m["kind"] for m in applied] == ["ConfigMap", "CronJob"]
