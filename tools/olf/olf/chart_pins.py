"""Source-mode Helm chart pins, readable without any deployment state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ChartDefault:
    """A catalog chart's source-mode fallback (no catalog entry, or a
    non-installed run) plus how it must be prepared.

    `chart_ref` is the repo-relative reference Helm resolves once the repo
    named `name.split("/", 1)[0]`-equivalent is added (`<repo>/<chart>`,
    matching `CatalogChart.reference`); `repository`/`version` are the
    literal defaults every provider config used to hardcode per chart before
    this table existed.
    """

    chart_ref: str
    repository: str
    version: str
    repack: bool = False
    variant: str | None = None


CHART_DEFAULTS: Mapping[str, ChartDefault] = {
    "trino": ChartDefault(chart_ref="trino/trino", repository="https://trinodb.github.io/charts", version="1.42.2"),
    "dagster": ChartDefault(
        chart_ref="dagster/dagster",
        repository="https://dagster-io.github.io/helm",
        version="1.13.7",
        repack=True,
        variant="no-schema",
    ),
    "seaweedfs": ChartDefault(
        chart_ref="seaweedfs/seaweedfs",
        repository="https://seaweedfs.github.io/seaweedfs/helm",
        version="4.23.0",
    ),
    "polaris": ChartDefault(
        chart_ref="polaris/polaris",
        repository="https://downloads.apache.org/polaris/helm-chart",
        version="1.4.1",
    ),
    "openmetadata": ChartDefault(
        chart_ref="open-metadata/openmetadata",
        repository="https://helm.open-metadata.org",
        version="1.13.6",
    ),
    "openmetadata-dependencies": ChartDefault(
        chart_ref="open-metadata/openmetadata-dependencies",
        repository="https://helm.open-metadata.org",
        version="1.13.6",
    ),
    "superset": ChartDefault(
        chart_ref="superset/superset",
        repository="https://apache.github.io/superset/",
        version="0.15.5",
    ),
    "traefik": ChartDefault(
        chart_ref="traefik/traefik",
        repository="https://traefik.github.io/charts",
        version="41.6.1",
    ),
    "cert-manager": ChartDefault(
        chart_ref="jetstack/cert-manager",
        repository="https://charts.jetstack.io",
        version="v1.21.2",
    ),
}
