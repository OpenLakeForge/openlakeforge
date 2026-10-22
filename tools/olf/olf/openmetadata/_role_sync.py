"""The CronJob that grants OpenMetadata roles from GitHub teams (ADR 0014, #25).

OpenMetadata 1.13.6 cannot map roles from the token, so a job reads the mapped teams' members and patches
users. It runs in-cluster on the pinned `ingestion-base` image (the only pinned image with Python and, in
the image's Airflow dependency set, `cryptography`; the script falls back to `openssl`), with the script
mounted from a ConfigMap because that image has no `olf`. Built here and applied by `deploy-metadata`, like
the ingestion CronJob; the App credentials reach the pod through secretKeyRefs, so no manifest holds them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from olf import github_app
from olf.openmetadata._auth import AUTOMATION_JWT_KEY, AUTOMATION_SECRET
from olf.profile import GitHubSpec

NAME = "openmetadata-role-sync"
# Inside the 15 min native-service revocation bound of identity-sessions-and-recovery.md.
SCHEDULE = "*/5 * * * *"
_SCRIPT_DIR = "/etc/olf/role-sync"


def github_api(host: str | None) -> str:
    return github_app.api_base(host or "github.com")


def _secret_env(name: str, secret: str, key: str) -> dict[str, Any]:
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}


def role_sync_manifests(
    github: GitHubSpec, *, image: str, namespace: str, om_url: str, precedence: list[str]
) -> list[dict[str, Any]]:
    labels = {"openlakeforge.io/job": NAME}
    meta = {"namespace": namespace, "labels": labels}
    app = github.oauth_app_secret_ref.name
    container = {
        "name": "sync",
        "image": image,
        "command": ["python3", f"{_SCRIPT_DIR}/role_sync.py"],
        "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
        "env": [
            {"name": "GITHUB_API", "value": github_api(github.host)},
            {"name": "GITHUB_ORG", "value": github.organization},
            {"name": "ROLE_TEAMS", "value": json.dumps({role: list(slugs) for role, slugs in github.teams.items()})},
            {"name": "ROLE_PRECEDENCE", "value": json.dumps(precedence)},
            {"name": "OM_URL", "value": om_url},
            _secret_env("OM_JWT", AUTOMATION_SECRET, AUTOMATION_JWT_KEY),
            # Fixed key names `olf github connect` writes.
            _secret_env("APP_ID", app, "app-id"),
            _secret_env("PRIVATE_KEY", app, "private-key"),
        ],
        "volumeMounts": [{"name": "script", "mountPath": _SCRIPT_DIR, "readOnly": True}],
    }
    pod = {
        "restartPolicy": "Never",
        "containers": [container],
        "volumes": [{"name": "script", "configMap": {"name": NAME}}],
    }
    script = (Path(__file__).with_name("role_sync_script.py")).read_text()
    return [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": NAME, **meta}, "data": {"role_sync.py": script}},
        {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": NAME, **meta},
            "spec": {
                "schedule": SCHEDULE,
                "concurrencyPolicy": "Forbid",
                "successfulJobsHistoryLimit": 1,
                "failedJobsHistoryLimit": 3,
                "jobTemplate": {
                    "spec": {
                        "backoffLimit": 0,
                        "activeDeadlineSeconds": 240,
                        "template": {"metadata": {"labels": labels}, "spec": pod},
                    }
                },
            },
        },
    ]


def apply_role_sync(namespace: str, *, om_url: str) -> str:
    """Apply the CronJob when the profile signs in with GitHub and the App Secret holds its credentials.

    Returns what happened, for the log. An `external` issuer, or authentication off, never syncs: OIDC
    alone cannot enumerate members, so those users stay at the read baseline.
    """
    import yaml

    from olf import config, k8s
    from olf.profile import load_deployment_profile

    profile_path = config.project_root() / "openlakeforge.yaml"
    if not profile_path.is_file():
        return "no role sync: no Deployment Profile"
    identity = load_deployment_profile(profile_path, distribution_root=config.distribution_root()).identity
    if identity is None or identity.github is None:
        return "no role sync: the profile does not sign in with GitHub"
    github = identity.github
    try:
        has_app = bool(k8s.secret_value(github.oauth_app_secret_ref.name, "app-id", namespace))
    except k8s.KubectlError:
        has_app = False
    if not has_app:
        return "no role sync: the GitHub Secret has no 'app-id' (a hand-made OAuth App); run 'olf github connect'"
    catalog = yaml.safe_load((config.distribution_root() / "release/component-catalog.yaml").read_text())
    image = catalog["components"]["images"]["openmetadata_ingestion"]
    roles = yaml.safe_load((config.distribution_root() / "release/identity-roles.yaml").read_text())
    for manifest in role_sync_manifests(
        github, image=image, namespace=namespace, om_url=om_url, precedence=list(roles["precedence"])
    ):
        k8s.apply_manifest(manifest)
    return f"OpenMetadata roles sync from GitHub teams every 5 minutes ({NAME})"
