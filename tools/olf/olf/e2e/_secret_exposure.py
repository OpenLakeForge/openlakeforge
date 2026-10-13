"""Identity credential exposure scan (#181).

The identity Secrets are generated in-cluster and Terraform never reads them
(docs/architecture/identity-credentials.md), so any copy of their value in
state, a ConfigMap, the diagnostics bundle or a log is a leak. The scan reads
the current values from the cluster and looks for those exact strings. A
failure names the surface and the Secret, never the value.

Member passwords and action tokens have no value to look for in advance, so
the diagnostics bundle is checked for their link shapes instead.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from olf import k8s, log
from olf.commands import diagnostics
from olf.e2e._health import health_namespaces
from olf.e2e._identity import KEYCLOAK_ADMIN_SECRET
from olf.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise
from olf.tooling.terraform import external_state_options

# Below this a value is likelier to be an ordinary word in the haystack than a
# credential. Generated identity secrets are 32 characters.
MIN_VALUE_LENGTH = 16
# The perimeter module names its cookie Secret from a release name the contract
# does not carry (docs/architecture/identity-credentials.md).
COOKIE_SECRET = ("oauth2-proxy-cookie", "cookie-secret")
LOG_TAIL_LINES = 2000
# `login-actions/action-token` is the path Keycloak mails for an action token.
# The (?<![\w-]) keeps `access_token=` and `X-token=` out; `<` skips `<redacted>`.
ACTION_LINK = re.compile(
    r"execute-actions-email|login-actions/action-token|(?<![\w-])(?:password|token)=[^\s&\"'<][^\s&\"']{7,}"
)

Surfaces = Mapping[str, str | None]


def find_exposures(credentials: Mapping[str, str], surfaces: Surfaces) -> list[str]:
    """One message per (surface, credential) pair where the surface holds the value.

    `credentials` maps a Secret label to its current value; `None` marks a
    surface that could not be read, which is skipped out loud rather than passed.
    """
    short = sorted(label for label, value in credentials.items() if len(value) < MIN_VALUE_LENGTH)
    if short:
        raise E2EError(
            f"refusing to scan for {', '.join(short)}: shorter than {MIN_VALUE_LENGTH} characters, "
            "so matches would be noise"
        )
    found: list[str] = []
    for surface, text in surfaces.items():
        if text is None:
            log.info(f"Skipping the exposure scan of {surface}: unavailable.")
            continue
        for label, value in credentials.items():
            # Kubernetes carries Secret data base64-encoded, so that form is a leak too.
            if value in text or base64.b64encode(value.encode()).decode() in text:
                found.append(f"{surface} contains the value of Secret {label}")
    return found


def find_action_links(surfaces: Surfaces) -> list[str]:
    return [
        f"{surface} contains a Keycloak action link or a password/token query string"
        for surface, text in surfaces.items()
        if text is not None and ACTION_LINK.search(text)
    ]


def scan(credentials: Mapping[str, str], surfaces: Surfaces, *, link_surfaces: Surfaces) -> None:
    problems = find_exposures(credentials, surfaces) + find_action_links(link_surfaces)
    if problems:
        raise E2EError("identity credentials are exposed:\n" + "\n".join(problems))


def check_secret_exposure(cfg: E2EConfig) -> None:
    identity = load_provider_contracts_or_raise(cfg)["shared"]["identity"]
    if identity.get("adapter") != "keycloak":
        log.info("Skipping the credential exposure scan: the Keycloak adapter is not deployed.")
        return
    log.step("Scanning state, ConfigMaps, diagnostics and logs for identity credentials...")
    namespaces = health_namespaces(cfg)
    bundles = {f"diagnostics bundle of {ns}": _diagnostics_bundle(cfg, ns) for ns in namespaces}
    configmaps = {
        f"ConfigMaps in {ns}": _kubectl_text(cfg, ["get", "configmap", "-n", ns, "-o", "json"]) for ns in namespaces
    }
    surfaces = {**_state_surfaces(cfg), **configmaps, **bundles, **_log_surfaces(cfg, namespaces)}
    scan(_credentials(cfg, identity), surfaces, link_surfaces=bundles)


def _credentials(cfg: E2EConfig, identity: Mapping[str, Any]) -> dict[str, str]:
    namespace = cfg.platform_namespace
    required = [(KEYCLOAK_ADMIN_SECRET, "password")] + [
        (spec["secret_ref"]["name"], spec["secret_ref"]["key"]) for spec in identity["clients"].values()
    ]
    found = {
        f"{name}/{key}": k8s.secret_value(name, key, namespace, kube_context=cfg.kube_context) for name, key in required
    }
    for name, key in [COOKIE_SECRET, *_smtp_password_refs(cfg)]:
        try:
            value = k8s.secret_value(name, key, namespace, kube_context=cfg.kube_context)
        except k8s.KubectlError:
            log.info(f"Not scanning for Secret {name}/{key}: it is not in {namespace}.")
            continue
        if len(value) < MIN_VALUE_LENGTH:
            # Operator-chosen (SMTP) values can be short; a short needle is noise.
            log.info(f"Not scanning for Secret {name}/{key}: its value is shorter than {MIN_VALUE_LENGTH} characters.")
            continue
        found[f"{name}/{key}"] = value
    return found


def _smtp_password_refs(cfg: E2EConfig) -> list[tuple[str, str]]:
    """The SMTP Secret is named by the profile, which the contract does not carry;
    the realm Job's SMTP_PASSWORD reference is where the deployed name shows."""
    text = _kubectl_text(cfg, ["get", "jobs", "-n", cfg.platform_namespace, "-o", "json"])
    refs: list[tuple[str, str]] = []
    for job in json.loads(text or '{"items": []}')["items"]:
        for container in job["spec"]["template"]["spec"].get("containers", []):
            for env in container.get("env", []):
                ref = env.get("valueFrom", {}).get("secretKeyRef")
                if env["name"] == "SMTP_PASSWORD" and ref:
                    refs.append((ref["name"], ref["key"]))
    return sorted(set(refs))


def _state_surfaces(cfg: E2EConfig) -> dict[str, str | None]:
    surfaces: dict[str, str | None] = {}
    for root in (cfg.contract_terraform_dir, cfg.foundation_terraform_dir):
        if root is None:
            continue
        _, state = external_state_options(root, os.environ, create=False)
        path = state or root / "terraform.tfstate"
        surfaces[f"Terraform state of {root.name}"] = path.read_text() if path.is_file() else None
    return surfaces


def _kubectl_text(cfg: E2EConfig, args: list[str]) -> str | None:
    try:
        return kubectl(cfg, args, capture=True)
    except E2EError:
        return None


def _log_surfaces(cfg: E2EConfig, namespaces: tuple[str, ...]) -> dict[str, str | None]:
    # Every pod, not a hand-picked list: a Job's pod, an oauth2-proxy replica and a
    # bootstrap pod are all places a credential could be echoed.
    surfaces: dict[str, str | None] = {}
    for namespace in namespaces:
        names = _kubectl_text(cfg, ["get", "pods", "-n", namespace, "-o", "name"])
        if names is None:
            surfaces[f"pod logs in {namespace}"] = None
        for pod in (names or "").split():
            surfaces[f"logs of {pod} in {namespace}"] = _kubectl_text(
                cfg, ["logs", "-n", namespace, pod, "--all-containers", f"--tail={LOG_TAIL_LINES}"]
            )
    return surfaces


def _diagnostics_bundle(cfg: E2EConfig, namespace: str) -> str | None:
    """What `olf diagnostics collect` writes for the namespace, joined into one text."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            diagnostics.collect(Path(tmp), namespace=namespace, kube_context=cfg.kube_context)
        except Exception as exc:  # an unavailable bundle is a skip, not a failure
            log.info(f"olf diagnostics collect failed for {namespace}: {type(exc).__name__}")
            return None
        return "\n".join(path.read_text(errors="replace") for path in sorted(Path(tmp).iterdir()))
