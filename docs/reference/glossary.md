# Glossary

The terms the OpenLakeForge documentation uses, each in a line or two, in
alphabetical order. Each entry names the file or command that defines it.

| Term | Meaning |
| --- | --- |
| Access | The `spec.access` block of the Deployment Profile. Declaring it publishes the user-facing services through one ingress at `<service>.<base_domain>` names; omitting it publishes nothing. |
| Artifacts phase | The third `olf deploy` phase. It deploys everything derived from `lakehouse_code/`, so a code change reruns only this phase. |
| Base domain | `spec.access.base_domain`, `olf.localhost` by default. Every route host ends with it, and the portal is served at the base domain itself. |
| Bronze | The landing layer: source data as files in object storage, loaded by dlt and not yet validated. Namespace `<source>_bronze`. |
| Canonical role | One of `platform-admin`, `data-engineer`, `analyst` and `viewer`, highest first, defined in `release/identity-roles.yaml`. A role decides which services a person can open. |
| Capability | A per-stage optional service: `analytics` (Superset) or `governance` (OpenMetadata). The preset sets the defaults. |
| Catalog | The Apache Iceberg catalog that registers the tables. Polaris locally, AWS Glue on AWS. Each stage has its own, `lakehouse_<stage>`. |
| Component catalog | `release/component-catalog.yaml`: the pinned versions and image digests of everything a release ships. |
| Dagster | The orchestrator. It runs the data path as an asset graph, with one job per data product. |
| Data product | A unit a domain publishes: it reads Silver tables of its domain and publishes Gold tables. Each has one Dagster job, `<id>_pipeline`. |
| dbt-trino | The dbt adapter that builds Gold models by running SQL on Trino. |
| Deployment Profile | `openlakeforge.yaml`: provider, preset, stages, access and identity for one deployment. |
| Descriptor | A YAML file that names logical things only: `governance.yml` and each `source.yaml`. Physical names are derived, never written. |
| Dex | The OIDC issuer OpenLakeForge deploys to sign people in through GitHub. |
| dlt | The loader that extracts a source into Bronze. |
| Domain | A business area that owns a set of Silver tables and the data products built on them. |
| Floe | The engine that validates Bronze against a domain's contract and writes accepted rows to Silver; rejected rows go to a quarantine location. |
| Foundation phase | The first `olf deploy` phase: it creates the cluster and registry. |
| Gold | The business layer: marts that dbt-trino builds from Silver. Namespace `<product>_gold`. |
| Iceberg | The open table format for Silver and Gold tables. |
| Identity | The `spec.identity` block: which OIDC issuer signs people in (`github` or `external`) and how its claims map to canonical roles. Without it, authentication is off. |
| Issuer (access) | `spec.access.issuer`: the certificate issuer for the routes. `local-ca` signs with a local CA; `none` serves plain HTTP. |
| Issuer (identity) | `spec.identity.issuer`: the OIDC issuer, `github` or `external`. |
| Lakehouse | The whole project: the sources, domains, products and dashboards that `lakehouse_code/` holds, with domains and products named in `governance.yml`. |
| Medallion layers | Bronze, Silver and Gold: three layers, each owned by one tool. |
| Namespace (catalog) | A single-level catalog schema: `<source>_bronze`, `<domain>_silver` or `<product>_gold`. Not a Kubernetes namespace. |
| Perimeter | The oauth2-proxy layer in front of every route except the login page. It admits a user only when their role holds a grant for the service. |
| Platform phase | The second `olf deploy` phase: it deploys the shared services in `olf-system` and each stage's services. |
| Portal | The landing page at the base domain that links every published route. |
| Preset | `slim` or `full`. `full` turns on `analytics` and `governance` by default. |
| Project revision | An immutable build of the project (descriptors, contracts, dbt, Dagster code, image digest) made by `olf project build` and promoted between stages unchanged. |
| Provider contract | A provider-neutral description of a capability (catalog, storage, access, identity), so local, AWS and Azure expose the same shape with different implementations. |
| Provider | The deployment target: `local` (kind), `aws` or `azure`. |
| Resource | A table-like entity a source exposes in Bronze, declared under `resources` in `source.yaml`. |
| Route | A published URL for a user-facing service, defined in `contracts.tf`. See [Components and services](components.md#service-urls). |
| Silver | The validated layer: Iceberg tables Floe writes per domain. Namespace `<domain>_silver`. |
| Source | One system you ingest from, declared in `lakehouse_code/bronze/<source>/source.yaml`. |
| Stage | A lifecycle boundary: `dev`, `uat` or `prod`. Stages share one cluster; each has its own namespace, Dagster, buckets and catalog. |
| Topology | The effective shape `olf profile resolve` computes from a profile: stages with their capabilities resolved, plus access and identity. |
| Trino | The SQL engine over the Iceberg catalog, used by dbt, Superset and `olf query`. |

---

## Related

* [Components and services](components.md)
* [Concepts](../get-started/concepts.md)
* [Architecture overview](../architecture/overview.md)
