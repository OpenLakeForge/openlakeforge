from __future__ import annotations

import base64
import stat
from pathlib import Path

import yaml

from olf.tooling.aws import AwsSdk


class _Credentials:
    access_key = "access"
    secret_key = "secret"
    token = "token"

    def get_frozen_credentials(self) -> _Credentials:
        return self


class _Session:
    def __init__(self, clients):  # noqa: ANN001
        self.clients = clients

    def client(self, name: str, **_kwargs):  # noqa: ANN003, ANN202
        return self.clients[name]

    def get_credentials(self) -> _Credentials:
        return _Credentials()


def test_eks_update_kubeconfig_writes_token_file_without_exec(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    eks = type(
        "Eks",
        (),
        {
            "describe_cluster": lambda *_args, **_kwargs: {
                "cluster": {"endpoint": "https://eks", "certificateAuthority": {"data": "ca"}}
            }
        },
    )()
    aws = AwsSdk(_Session({"eks": eks}))
    monkeypatch.setattr(aws, "_eks_token", lambda *_args, **_kwargs: "k8s-aws-v1.token")
    path = tmp_path / "aws.yaml"

    aws.eks_update_kubeconfig("cluster", region="eu-west-1", kubeconfig_path=path, alias="context")

    document = yaml.safe_load(path.read_text())
    assert document["users"][0]["user"] == {"tokenFile": str(path.with_suffix(".yaml.token"))}
    assert "exec" not in document["users"][0]["user"]
    assert path.with_suffix(".yaml.token").read_text() == "k8s-aws-v1.token\n"
    assert path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
    assert path.with_suffix(".yaml.token").stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def test_sts_get_caller_identity_uses_sdk() -> None:
    identity = {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/dev"}
    sts = type("Sts", (), {"get_caller_identity": lambda *_args: identity})()

    assert AwsSdk(_Session({"sts": sts})).sts_get_caller_identity() == identity


def test_ecr_get_login_password_decodes_authorization_token() -> None:
    encoded = base64.b64encode(b"AWS:password-value").decode()
    ecr = type(
        "Ecr", (), {"get_authorization_token": lambda *_args: {"authorizationData": [{"authorizationToken": encoded}]}}
    )()

    assert AwsSdk(_Session({"ecr": ecr})).ecr_get_login_password(region="eu-west-1") == "password-value"


def test_eks_describe_cluster_maps_sdk_failures_to_not_ok_result() -> None:
    eks = type(
        "Eks", (), {"describe_cluster": lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("not found"))}
    )()

    result = AwsSdk(_Session({"eks": eks})).eks_describe_cluster("cluster", region="eu-west-1")

    assert not result.ok
    assert "not found" in result.stderr


def test_eks_token_presigns_against_the_sessions_resolved_sts_endpoint() -> None:
    """GovCloud/China regions resolve to a different STS host than the
    commercial partition; the presigned URL must follow the session's own
    endpoint resolution rather than assuming `sts.<region>.amazonaws.com`.
    """
    meta = type("Meta", (), {"endpoint_url": "https://sts.cn-north-1.amazonaws.com.cn"})()
    sts_client = type("Sts", (), {"meta": meta})()

    aws = AwsSdk(_Session({"sts": sts_client}))
    token = aws._eks_token("cluster", region="cn-north-1")  # noqa: SLF001 - exercising the presign directly.

    assert token.startswith("k8s-aws-v1.")
    encoded = token.removeprefix("k8s-aws-v1.")
    padded = encoded + "=" * (-len(encoded) % 4)
    url = base64.urlsafe_b64decode(padded).decode()
    assert url.startswith("https://sts.cn-north-1.amazonaws.com.cn/")


def test_eks_token_refresh_survives_a_transient_failure(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """A single failed STS call must not permanently freeze the token file.

    Giving up on first error would leave the written token to expire (~15
    minutes), after which every kubectl/Helm/Terraform call in a long cloud
    apply fails with an opaque 401 and no way to recover short of restarting.
    """
    import time

    from olf.tooling import aws as aws_module

    attempts: list[str] = []

    def _token(*_args, **_kwargs) -> str:  # noqa: ANN002, ANN003
        attempts.append("call")
        if len(attempts) == 1:
            raise RuntimeError("transient STS failure")
        return "k8s-aws-v1.refreshed"

    monkeypatch.setattr(aws_module, "_EKS_TOKEN_REFRESH_SECONDS", 0.01)
    sdk = AwsSdk(_Session({}))
    monkeypatch.setattr(sdk, "_eks_token", _token)
    token_path = tmp_path / "aws.yaml.token"

    sdk._refresh_eks_token(token_path, cluster_name="c", region="eu-west-1", env={})  # noqa: SLF001
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not token_path.is_file():
        time.sleep(0.01)
    sdk._token_refreshes[token_path].set()  # noqa: SLF001 - stop the daemon.

    assert token_path.is_file(), "refresh gave up after the first failure"
    assert token_path.read_text() == "k8s-aws-v1.refreshed\n"
    assert len(attempts) >= 2
