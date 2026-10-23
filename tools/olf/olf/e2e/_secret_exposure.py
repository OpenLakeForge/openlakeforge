"""Identity credential exposure scan (#181).

The identity Secrets are generated in-cluster and Terraform never reads them
(docs/architecture/identity/identity-credentials.md), so any copy of their value in
state, a ConfigMap, the diagnostics bundle or a log is a leak. The scan reads
the current values from the cluster and looks for those exact strings. A
failure names the surface and the Secret, never the value.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from olf import k8s, log
from olf.commands import diagnostics
from olf.e2e._health import health_namespaces
from olf.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise
from olf.tooling.terraform import external_state_options

# Below this a value is likelier to be an ordinary word in the haystack than a
# credential. Generated identity secrets are 32 characters.
MIN_VALUE_LENGTH = 16
# The perimeter module names its cookie Secret from a release name the contract
# does not carry (docs/architecture/identity/identity-credentials.md).
COOKIE_SECRET = ("oauth2-proxy-cookie", "cookie-secret")
TRINO_INTERNAL_SECRET = ("trino-internal-secret", "internal-communication.shared-secret")
# Created in-cluster by the Polaris module: "<release>-token-broker", release "polaris".
POLARIS_TOKEN_KEY_SECRET = ("polaris-token-broker", "symmetric.key")
# Created in-cluster by the OpenMetadata module: "<release>-fernet-key", release "openmetadata".
OPENMETADATA_FERNET_SECRET = ("openmetadata-fernet-key", "fernet-key")
TRINO_CREDENTIAL_PREFIX = "trino-credential-"
LOG_TAIL_LINES = 2000

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
            # Kubernetes carries Secret data base64-encoded, and `-o json` and Terraform
            # state escape quotes, backslashes and newlines: those forms are leaks too.
            forms = (value, base64.b64encode(value.encode()).decode(), json.dumps(value)[1:-1])
            if any(form in text for form in forms):
                found.append(f"{surface} contains the value of Secret {label}")
    return found


def scan(credentials: Mapping[str, str], surfaces: Surfaces) -> None:
    problems = find_exposures(credentials, surfaces)
    if problems:
        raise E2EError("identity credentials are exposed:\n" + "\n".join(problems))


def check_secret_exposure(cfg: E2EConfig) -> None:
    contracts = load_provider_contracts_or_raise(cfg)
    identity = contracts["shared"]["identity"]
    log.step("Scanning state, ConfigMaps, diagnostics and logs for identity credentials...")
    namespaces = scan_namespaces(cfg, contracts)
    bundles = {f"diagnostics bundle of {ns}": _diagnostics_bundle(cfg, ns) for ns in namespaces}
    configmaps = {
        f"ConfigMaps in {ns}": _kubectl_text(cfg, ["get", "configmap", "-n", ns, "-o", "json"]) for ns in namespaces
    }
    surfaces = {**_state_surfaces(cfg), **configmaps, **bundles, **_log_surfaces(cfg, namespaces)}
    scan(_credentials(cfg, identity), surfaces)


def scan_namespaces(cfg: E2EConfig, contracts: Mapping[str, Any]) -> tuple[str, ...]:
    """The shared namespace and every stage's, not only the selected one: the Trino passwords of all stages are
    scanned for, so a PROD password leaked in PROD's namespace must be found by a run that selected DEV."""
    stages = (spec.get("namespace") for spec in (contracts.get("stages") or {}).values())
    return tuple(dict.fromkeys((*health_namespaces(cfg), *filter(None, stages))))


def _credentials(cfg: E2EConfig, identity: Mapping[str, Any]) -> dict[str, str]:
    namespace = cfg.platform_namespace

    def read(name: str, key: str) -> str:
        # secret_value puts the key in a JSONPath, where `.` separates fields.
        return k8s.secret_value(name, key.replace(".", "\\."), namespace, kube_context=cfg.kube_context)

    clients = [(spec["secret_ref"]["name"], spec["secret_ref"]["key"]) for spec in identity.get("clients", {}).values()]
    # An external issuer's client Secrets are operator-provided, so they may be absent or short; without an
    # issuer the Trino credentials are still deployed and scanned.
    found: dict[str, str] = {}
    known = [COOKIE_SECRET, POLARIS_TOKEN_KEY_SECRET, OPENMETADATA_FERNET_SECRET]
    for name, key in [*known, *clients, *_trino_secret_refs(cfg)]:
        try:
            value = read(name, key)
        except k8s.KubectlError:
            log.info(f"Not scanning for Secret {name}/{key}: it is not in {namespace}.")
            continue
        if len(value) < MIN_VALUE_LENGTH:
            # Operator-chosen values can be short; a short needle is noise.
            log.info(f"Not scanning for Secret {name}/{key}: its value is shorter than {MIN_VALUE_LENGTH} characters.")
            continue
        found[f"{name}/{key}"] = value
    return found


def _trino_secret_refs(cfg: E2EConfig) -> list[tuple[str, str]]:
    """The Trino workload credentials (ADR 0016) are named by principal, and which
    principals exist is the Job's call, so the Secrets themselves are listed. None
    exist until the credential Job has run, which is not an error."""
    text = _kubectl_text(cfg, ["get", "secrets", "-n", cfg.platform_namespace, "-o", "name"])
    if text is None:
        raise E2EError(f"cannot list the Secrets in {cfg.platform_namespace} to find the Trino credentials")
    names = sorted(line.removeprefix("secret/") for line in text.split())
    return [(name, "password") for name in names if name.startswith(TRINO_CREDENTIAL_PREFIX)] + [
        TRINO_INTERNAL_SECRET
    ]


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
