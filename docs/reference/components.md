# Bundled components

The open-source components OpenLakeForge deploys, what each does in the
platform, the version this release pins and where its interface is. Use it to
find which component owns a task, or to check a version.

Versions are read from `release/component-catalog.yaml` (the pinned inputs of
release 0.4.0-alpha.1, each image also pinned by digest there) and the project
code lockfile. The full inventory, including Helm charts and the managed
toolchain, is in the [Compatibility matrix](compatibility-matrix.md); how pins
change is in the [Component catalog](component-catalog.md).

A UI is published only when the Deployment Profile declares `spec.access`. In
the URLs, `<base>` is `spec.access.base_domain` (`olf.localhost` by default).
Who may open each UI is in [Services and routes](services-and-routes.md).

---

## Platform services

| Component | Role in OpenLakeForge | Version | UI |
| --- | --- | --- | --- |
| [Apache Polaris](https://polaris.apache.org/) | Iceberg REST catalog. Floe writes Silver and dbt-trino reads and writes Gold through it. | image 1.4.0 (chart 1.4.1) | none |
| [SeaweedFS](https://github.com/seaweedfs/seaweedfs) | S3-compatible object storage for Bronze, Silver, Gold and run artifacts on the local provider. | 4.23 | none published |
| [Trino](https://trino.io/) | SQL engine over the Iceberg catalog. dbt builds Gold through it and Superset reads from it. | image 480 (chart 1.42.2) | `trino.<base>` |
| [Dagster](https://dagster.io/) | Orchestration: one asset graph and one job per data product, one instance per enabled stage. | 1.13.7 | `dagster-<stage>.<base>` |
| [Apache Superset](https://superset.apache.org/) | Dashboards over Gold tables, deployed per stage when `analytics` is on. Runs with a Redis 7.0.10 instance. | image 6.1.0 (chart 0.15.5) | `superset-<stage>.<base>` |
| [OpenMetadata](https://open-metadata.org/) | Governance: domains, data products and lineage, deployed when any stage has `governance` on. | 1.13.6 | `openmetadata.<base>` |
| [OpenSearch](https://opensearch.org/) | Search index for OpenMetadata. | 3.3.2 | none |
| [PostgreSQL](https://www.postgresql.org/) | Metadata databases for the platform services. | 16 | none |
| [Traefik](https://traefik.io/) | Ingress that publishes the routes. | image v3.7.13 (chart 41.6.1) | none |
| [cert-manager](https://cert-manager.io/) | Issues the route certificates from the local CA. | v1.21.2 | none |
| [Dex](https://dexidp.io/) | OIDC issuer that signs people in through GitHub; deployed when `spec.identity.issuer` is `github`. | v2.46.0 | login at `auth.<base>` |
| [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) | The perimeter: checks a user's role before a route opens. | v7.15.5 | none |
| Portal | Landing page that links every published route. | BusyBox 1.37.0 static server | `<base>` |

## Data tooling in the project-code image

These ship in the `project-code` image, which runs the pipelines of your
`lakehouse_code/`. Versions are pinned in `images/project-code/requirements.lock`.

| Component | Role in OpenLakeForge | Version | UI |
| --- | --- | --- | --- |
| [dlt](https://dlthub.com/) | Extracts a source into the Bronze layer. | 1.29.0 | none |
| [Floe](https://github.com/malon64/floe) | Validates Bronze against a domain contract and writes accepted rows to Silver Iceberg tables. Run from Dagster through `dagster-floe`. | `dagster-floe` 0.2.5; Floe image 0.6.11 by default (`FLOE_VERSION`) | none |
| [dbt](https://www.getdbt.com/) with dbt-trino | Builds Gold models on Trino. | dbt-core 1.11.12, dbt-trino 1.10.2 | none |

The Floe runner version is not in the component catalog: `olf` takes it from
`FLOE_VERSION`, defaulting to 0.6.11.

---

## Related

* [Services and routes](services-and-routes.md)
* [Compatibility matrix](compatibility-matrix.md)
* [Component catalog](component-catalog.md)
* [Architecture overview](../architecture/overview.md)
* [Floe validation](../architecture/floe-validation.md)
