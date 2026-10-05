"""Service access through the ingress (ADR 0013)."""

from __future__ import annotations

import base64
import os
import shlex
import sys
from pathlib import Path

import typer

from olf.commands._shared import deployment_context, fail

app = typer.Typer(help="Service access through the ingress: local CA trust.")

_CERT_NAME = "openlakeforge-local-ca.crt"


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def trust_steps(cert: Path, *, platform: str, wsl: bool) -> str:
    """The commands that add `cert` to this machine's trust stores; each needs the user's own privileges."""
    path = shlex.quote(str(cert))
    firefox = "Firefox keeps its own store: Settings > Privacy & Security > View Certificates > Authorities > Import."
    if platform == "darwin":
        return "\n".join(
            [
                "Trust it in the System keychain (Safari, Chrome, curl):",
                f"  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain {path}",
                firefox,
            ]
        )
    linux = [
        "Trust it in the system store (curl, Python, Go tools):",
        f"  Debian/Ubuntu: sudo cp {path} /usr/local/share/ca-certificates/{_CERT_NAME} && sudo update-ca-certificates",
        f"  Fedora/RHEL:   sudo cp {path} /etc/pki/ca-trust/source/anchors/{_CERT_NAME} && sudo update-ca-trust",
    ]
    if wsl:
        return "\n".join(
            [
                "Your browser runs on Windows: trust it in the Windows store (Edge, Chrome; Windows asks to confirm):",
                f"  powershell.exe -NoProfile -Command \"Import-Certificate -FilePath '$(wslpath -w {path})' "
                "-CertStoreLocation 'Cert:\\CurrentUser\\Root'\"",
                *linux,
            ]
        )
    return "\n".join(
        [
            *linux,
            "Chrome and Chromium read the NSS store (certutil: libnss3-tools / nss-tools):",
            f'  certutil -d sql:$HOME/.pki/nssdb -A -t C,, -n "OpenLakeForge local CA" -i {path}',
            firefox,
        ]
    )


@app.command("trust")
def trust(
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Local kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
    output: str = typer.Option("", "--output", help=f"Write the certificate here (default: <work dir>/{_CERT_NAME})."),
) -> None:
    """Export the local CA certificate and print how to trust it on this machine.

    olf's own clients verify against the CA without this; it is for browsers
    and other tools on the workstation.
    """
    from olf.access import LOCAL_CA_KEY, LOCAL_CA_SECRET
    from olf.deployment.engine import Toolkit

    context = deployment_context(
        "local",
        profile="",
        namespace="",
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    )
    env = context.command_env(base=os.environ)
    result = Toolkit.default(environ=env).kubectl.get(
        "secret",
        name=LOCAL_CA_SECRET,
        namespace=context.shared_namespace,
        context=context.kube_context,
        kubeconfig=context.paths.kubeconfig_path,
        output=f"jsonpath={{.data.{LOCAL_CA_KEY}}}",
        env=env,
        check=False,
    )
    if not result.ok or not result.stdout.strip():
        detail = result.stderr.strip() or "the secret has no tls.crt"
        raise typer.Exit(code=fail(f"cannot read the local CA ({detail}); deploy the local platform first: olf deploy"))
    cert = Path(output) if output else context.paths.work_root / _CERT_NAME
    cert.parent.mkdir(parents=True, exist_ok=True)
    cert.write_bytes(base64.b64decode(result.stdout))
    typer.echo(f"Wrote the OpenLakeForge local CA to {cert}")
    typer.echo(trust_steps(cert.resolve(), platform=sys.platform, wsl=_is_wsl()))
