"""AWS SDK adapter used by cloud deployment code.

The deployment layer deliberately does not know whether credentials came from
an IAM Identity Center browser session, an existing shared profile, or an
automation identity.  This adapter has no dependency on the ``aws`` binary.
"""

from __future__ import annotations

import base64
import os
import stat
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest

from olf.tooling.process import CommandResult

# EKS honours the presigned token for ~15 minutes from its `X-Amz-Date`.
# Refresh at a third of that so one failed attempt still has a successful
# retry before the token already on disk can expire.
_EKS_TOKEN_REFRESH_SECONDS = 300


class AwsSdk:
    """Small, injectable boto3 facade matching the former CLI adapter."""

    def __init__(self, session: Any | None = None, *_: Any, **__: Any) -> None:
        # Accept the former ``(runner, resolver)`` construction shape during
        # the transition, but never use either object to invoke a cloud CLI.
        self._session_override = session if session is None or hasattr(session, "client") else None
        self._token_refreshes: dict[Path, threading.Event] = {}

    def _session(self, env: Mapping[str, str] | None = None, *, region: str | None = None) -> Any:
        if self._session_override is not None:
            return self._session_override
        # OLF-managed SSO is resolved lazily to keep normal boto3 profiles and
        # workload identities completely conventional.
        from olf.auth import aws_session

        return aws_session(env or os.environ, region=region)

    def sts_get_caller_identity(self, *, env: Mapping[str, str] | None = None) -> Any:
        return self._session(env).client("sts").get_caller_identity()

    def eks_update_kubeconfig(
        self,
        cluster_name: str,
        *,
        region: str,
        kubeconfig_path: Path,
        alias: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        eks = self._session(env, region=region).client("eks", region_name=region)
        cluster = eks.describe_cluster(name=cluster_name)["cluster"]
        token = self._eks_token(cluster_name, region=region, env=env)
        token_path = kubeconfig_path.with_suffix(f"{kubeconfig_path.suffix}.token")
        _write_private(token_path, token + "\n")
        self._refresh_eks_token(token_path, cluster_name=cluster_name, region=region, env=env)
        kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": alias or cluster_name,
                    "cluster": {
                        "server": cluster["endpoint"],
                        "certificate-authority-data": cluster["certificateAuthority"]["data"],
                    },
                }
            ],
            "contexts": [
                {
                    "name": alias or cluster_name,
                    "context": {"cluster": alias or cluster_name, "user": alias or cluster_name},
                }
            ],
            "current-context": alias or cluster_name,
            # tokenFile is deliberately used instead of exec. Kubernetes
            # clients periodically reread it, allowing OLF to refresh it.
            "users": [{"name": alias or cluster_name, "user": {"tokenFile": str(token_path)}}],
        }
        import yaml

        _write_private(kubeconfig_path, yaml.safe_dump(kubeconfig, sort_keys=False))
        return CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0)

    def _refresh_eks_token(
        self,
        token_path: Path,
        *,
        cluster_name: str,
        region: str,
        env: Mapping[str, str] | None,
    ) -> None:
        """Keep the token file fresh while this OLF process remains active.

        EKS accepts the presigned STS token for roughly fifteen minutes from
        its `X-Amz-Date`, so Terraform, Helm, kubectl and port forwarding can
        reread the same tokenFile without an exec plugin. Refreshing on an
        interval well under that leaves room for one failed attempt to be
        retried before the written token can expire - a long cloud apply must
        not die at a 401 because a single STS call blipped.
        """
        if token_path in self._token_refreshes:
            return
        stop = threading.Event()
        self._token_refreshes[token_path] = stop
        environment = dict(env or os.environ)

        def refresh() -> None:
            while not stop.wait(_EKS_TOKEN_REFRESH_SECONDS):
                try:
                    _write_private(token_path, self._eks_token(cluster_name, region=region, env=environment) + "\n")
                except Exception:
                    # Keep going rather than returning: giving up on the first
                    # failure would silently freeze the token file for the rest
                    # of the process, turning a transient error into an opaque
                    # 401 on every later call. The foreground operation still
                    # reports any failed cloud call, and the exception is
                    # deliberately not logged here - an SDK response can carry
                    # sensitive headers.
                    continue

        threading.Thread(target=refresh, name="olf-eks-token-refresh", daemon=True).start()

    def _eks_token(self, cluster_name: str, *, region: str, env: Mapping[str, str] | None = None) -> str:
        session = self._session(env, region=region)
        credentials = session.get_credentials().get_frozen_credentials()
        # Resolve the STS endpoint through the session rather than assuming
        # the commercial partition - GovCloud and China regions resolve to
        # different hosts (and FIPS endpoints resolve differently again).
        endpoint = session.client("sts", region_name=region).meta.endpoint_url
        request = AWSRequest(
            method="GET",
            url=f"{endpoint}/?Action=GetCallerIdentity&Version=2011-06-15",
            headers={"x-k8s-aws-id": cluster_name},
        )
        SigV4QueryAuth(credentials, "sts", region, expires=60).add_auth(request)
        # botocore types `AWSRequest.url` as optional because its constructor
        # allows omitting one; this request is built with a url above, and
        # `add_auth` only rewrites it in place.
        encoded = base64.urlsafe_b64encode(request.url.encode()).decode().rstrip("=")  # type: ignore[union-attr]
        return f"k8s-aws-v1.{encoded}"

    def ecr_get_login_password(self, *, region: str, env: Mapping[str, str] | None = None) -> str:
        response = self._session(env, region=region).client("ecr", region_name=region).get_authorization_token()
        token = response["authorizationData"][0]["authorizationToken"]
        decoded = base64.b64decode(token).decode()
        return decoded.partition(":")[2]

    def eks_describe_cluster(
        self,
        cluster_name: str,
        *,
        region: str,
        env: Mapping[str, str] | None = None,
        check: bool = False,
    ) -> CommandResult:
        try:
            self._session(env, region=region).client("eks", region_name=region).describe_cluster(name=cluster_name)
        except Exception as exc:  # SDK errors have no meaningful process exit code.
            if check:
                raise
            return CommandResult(argv=(), returncode=1, stdout="", stderr=str(exc), duration_seconds=0.0)
        return CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0)


def _write_private(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
        os.chmod(raw_path, stat.S_IRUSR | stat.S_IWUSR)
        Path(raw_path).replace(path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        Path(raw_path).unlink(missing_ok=True)
        raise


# Compatibility name for downstream users importing the original adapter.
AwsCli = AwsSdk
