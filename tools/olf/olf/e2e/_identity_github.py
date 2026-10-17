"""The Dex adapter's e2e login (#384): Dex's real GitHub connector against a stub.

CI cannot sign in to github.com. The deployment sets `spec.identity.github.host`
to `STUB_NAME.<shared namespace>.svc`, and this module runs the stub there for
the length of the check: a throwaway pod serving `github_stub.py` over a
cert-manager certificate from the local CA, which Dex trusts as `rootCA`. The
stub's users are fixed at pod start, so no control endpoint exists.

The stub pod carries the label `openlakeforge.io/e2e=github-stub`, which is what
the network policy names to let Dex reach it.

The browser walk is the real one except the hop to GitHub's authorize page: the
stub's name resolves only in the cluster, so the walk answers that redirect
itself, as the stub would, with `code=<login>`.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
import yaml

from olf import log
from olf.e2e._identity import REQUEST_TIMEOUT
from olf.e2e._shell import E2EConfig, E2EError, kubectl

STUB_NAME = "olf-e2e-github"
# What the Dex NetworkPolicy names to allow egress to the stub (port 8443).
LABEL = "openlakeforge.io/e2e=github-stub"
LOCAL_CA_ISSUER = "local-ca"  # modules/access/cert-manager


def stub_host(cfg: E2EConfig) -> str:
    return f"{STUB_NAME}.{cfg.platform_namespace}.svc"


def dex_points_at_stub(cfg: E2EConfig) -> bool:
    jsonpath = "jsonpath={.data.config\\.yaml}"
    out = kubectl(cfg, ["get", "configmap", "dex", "-n", cfg.platform_namespace, "-o", jsonpath], capture=True)
    connectors = json.loads(out)["connectors"]
    return bool(connectors) and connectors[0]["config"].get("hostName") == stub_host(cfg)


def _remove(cfg: E2EConfig) -> None:
    kubectl(
        cfg,
        ["delete", "pod,service,certificate,secret", STUB_NAME, f"{STUB_NAME}-tls", "-n", cfg.platform_namespace]
        + ["--ignore-not-found"],
    )


@contextlib.contextmanager
def github_stub(cfg: E2EConfig, users: Mapping[str, list[str]]) -> Iterator[None]:
    """Serve the stub with `users` ({login: [`<org>:<slug>`]}); always removed."""
    namespace = cfg.platform_namespace
    catalog = yaml.safe_load((cfg.distribution_root / "release/component-catalog.yaml").read_text())
    image = catalog["components"]["images"]["k8s_bootstrap"]
    certificate = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": STUB_NAME, "namespace": namespace},
        "spec": {
            "secretName": f"{STUB_NAME}-tls",
            "dnsNames": [stub_host(cfg)],
            "issuerRef": {"kind": "ClusterIssuer", "name": LOCAL_CA_ISSUER},
        },
    }
    script = (Path(__file__).parent / "github_stub.py").read_text()
    spec = {
        "containers": [
            {
                "name": STUB_NAME,
                "image": image,
                "command": ["python3", "-c", script],
                # The logins are throwaway test names, not credentials.
                "env": [{"name": "STUB_USERS", "value": json.dumps(users)}],
                "volumeMounts": [{"name": "tls", "mountPath": "/tls"}],
                # Ready only once the server listens: without it the pod is
                # Ready at container start and Dex's first exchange is refused.
                "readinessProbe": {"tcpSocket": {"port": 8443}, "periodSeconds": 1},
            }
        ],
        "volumes": [{"name": "tls", "secret": {"secretName": f"{STUB_NAME}-tls"}}],
    }
    _remove(cfg)
    try:
        with tempfile.TemporaryDirectory() as scratch:
            manifest = Path(scratch) / "certificate.yaml"
            manifest.write_text(yaml.safe_dump(certificate))
            kubectl(cfg, ["apply", "-f", str(manifest)])
        kubectl(cfg, ["wait", "--for=condition=Ready", f"certificate/{STUB_NAME}", "-n", namespace, "--timeout=120s"])
        overrides = json.dumps({"spec": spec})
        run = ["run", STUB_NAME, "-n", namespace, "--restart=Never", f"--image={image}", f"--labels={LABEL}"]
        run.append(f"--overrides={overrides}")
        kubectl(cfg, run)
        kubectl(cfg, ["expose", "pod", STUB_NAME, "-n", namespace, "--port=443", "--target-port=8443"])
        kubectl(cfg, ["wait", "--for=condition=Ready", f"pod/{STUB_NAME}", "-n", namespace, "--timeout=180s"])
        yield
    finally:
        _remove(cfg)


class StubAuthorize(requests.adapters.BaseAdapter):
    """Answers the redirect to the stub's authorize page as the stub would: straight back with `code=<login>`.

    The stub's name resolves only in the cluster, so the host cannot fetch the
    page. Mounted on the session, it also answers every later sign-in the
    session is sent through, e.g. after the proxy clears its cookie on a denial.
    """

    def __init__(self, login: str) -> None:
        super().__init__()
        self.login = login

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: Any = True,
        cert: Any = None,
        proxies: Any = None,
    ) -> requests.Response:
        query = parse_qs(urlsplit(str(request.url)).query)
        try:
            back = f"{query['redirect_uri'][0]}?{urlencode({'code': self.login, 'state': query['state'][0]})}"
        except KeyError as exc:
            raise E2EError(f"the authorize redirect to the stub lacks {exc}: {request.url}") from exc
        reply = requests.Response()
        reply.status_code = 302
        reply.headers["Location"] = back
        reply.url = str(request.url)
        reply.request = request
        reply.raw = io.BytesIO(b"")
        return reply

    def close(self) -> None:
        pass


def sign_in_with_stub(url: str, login: str, stub: str) -> tuple[requests.Session, requests.Response]:
    """Open `url` and follow redirects to the end, signing in as `login` at the stub.

    The response is the last one: the page `url` serves once signed in, or
    Dex's error page when the connector refused the login.
    """
    browser = requests.Session()
    browser.mount(f"https://{stub}", StubAuthorize(login))
    return browser, browser.get(url, timeout=REQUEST_TIMEOUT)


def log_skip() -> None:
    log.info(
        f"Skipping the Dex perimeter checks: Dex does not point at {STUB_NAME}; "
        "set spec.identity.github.host to it (see the e2e profile in the PR description)."
    )
