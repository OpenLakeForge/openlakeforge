"""`olf check infra`: Terraform roots and pinned local Helm chart renders."""

from __future__ import annotations

import typer
import yaml

from olf.commands._shared import fail
from olf.commands.checks._shared import _root
from olf.deployment.engine import Toolkit


def infra(repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate.")) -> None:
    """Format/validate Terraform roots and render pinned local Helm charts."""
    root = _root(repo_root)
    tools = Toolkit.default()
    env = {
        "HELM_REPOSITORY_CONFIG": str(root / ".tmp/check-helm/repositories.yaml"),
        "HELM_REPOSITORY_CACHE": str(root / ".tmp/check-helm/cache"),
    }
    roots = (
        "infra/terraform/foundations/local-kind",
        "infra/terraform/foundations/azure-aks",
        "infra/terraform/foundations/aws-eks",
        "infra/terraform/environments/local",
        "infra/terraform/environments/azure-poc",
        "infra/terraform/environments/aws-poc",
    )
    terraform = str(tools.resolver.resolve("terraform"))
    tools.runner.run([terraform, "fmt", "-check", "-recursive", str(root / "infra/terraform")], stream_output=True)
    for relative in roots:
        directory = root / relative
        tools.terraform.init(
            directory,
            extra_args=("-backend=false", "-input=false", "-lockfile=readonly"),
            env=env,
        )
        tools.runner.run([terraform, f"-chdir={directory}", "validate"], env=env, stream_output=True)
    charts = (
        ("seaweedfs", "seaweedfs/seaweedfs", "4.23.0", "infra/helm/values/local/seaweedfs.yaml"),
        ("polaris", "polaris/polaris", "1.4.1", "infra/helm/values/local/polaris.yaml"),
        ("trino", "trino/trino", "1.42.2", "infra/helm/values/local/trino.yaml"),
        ("dagster", "dagster/dagster", "1.13.7", "infra/helm/values/local/dagster.yaml"),
        ("superset", "superset/superset", "0.15.5", "infra/helm/values/local/superset.yaml"),
    )
    repos = (
        ("seaweedfs", "https://seaweedfs.github.io/seaweedfs/helm"),
        ("polaris", "https://downloads.apache.org/polaris/helm-chart"),
        ("trino", "https://trinodb.github.io/charts"),
        ("dagster", "https://dagster-io.github.io/helm"),
        ("superset", "http://apache.github.io/superset/"),
    )
    for name, url in repos:
        tools.helm.repo_add(name, url, env=env)
    tools.helm.repo_update(env=env)
    helm = str(tools.resolver.resolve("helm"))
    for release_name, chart, version, values in charts:
        args = [
            helm,
            "template",
            release_name,
            chart,
            "--version",
            version,
            "--namespace",
            "olf-system",
            "--values",
            str(root / values),
        ]
        if release_name == "superset":
            args.extend(_superset_template_overrides())
        result = tools.runner.run(
            args,
            env=env,
        )
        if release_name == "polaris" and "polaris.persistence.type=relational-jdbc" not in result.stdout:
            raise typer.Exit(code=fail("rendered Polaris chart is not configured for relational JDBC persistence"))
        if release_name == "superset":
            _validate_superset_render(result.stdout)
    typer.echo("Infrastructure checks passed.")


def _superset_template_overrides() -> list[str]:
    """Mirror the Terraform Helm release inputs for local Superset rendering."""
    return [
        "--set",
        "image.repository=ghcr.io/openlakeforge/superset",
        "--set",
        "image.tag=local",
        "--set",
        "image.pullPolicy=Never",
        "--set",
        "extraSecretEnv.SUPERSET_SECRET_KEY=check",
        "--set",
        "supersetNode.connections.db_host=postgresql",
        "--set",
        "supersetNode.connections.db_port=5432",
        "--set",
        "supersetNode.connections.db_user=superset",
        "--set",
        "supersetNode.connections.db_pass=check",
        "--set",
        "supersetNode.connections.db_name=superset",
        "--set-json",
        'extraVolumes=[{"name":"superset-reports","emptyDir":{"sizeLimit":"1Gi"}}]',
        "--set-json",
        'extraVolumeMounts=[{"name":"superset-reports","mountPath":"/app/openlakeforge/reports"}]',
    ]


def _validate_superset_render(rendered: str) -> None:
    """Require the Terraform-owned ephemeral reports volume in rendered manifests."""
    documents = [item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)]
    reports_volume = False
    reports_mount = False
    reports_pvc = False
    for document in documents:
        metadata = document.get("metadata", {})
        if document.get("kind") == "PersistentVolumeClaim" and metadata.get("name") == "superset-reports":
            reports_pvc = True
        pod_spec = document.get("spec", {}).get("template", {}).get("spec", {})
        for volume in pod_spec.get("volumes", []):
            if volume.get("name") == "superset-reports" and "emptyDir" in volume:
                reports_volume = True
        for container in pod_spec.get("containers", []):
            for mount in container.get("volumeMounts", []):
                if mount.get("name") == "superset-reports" and mount.get("mountPath") == "/app/openlakeforge/reports":
                    reports_mount = True
    if not reports_volume or not reports_mount or reports_pvc:
        raise typer.Exit(
            code=fail(
                "rendered Superset chart must include the superset-reports emptyDir and mount, without a reports PVC"
            )
        )
