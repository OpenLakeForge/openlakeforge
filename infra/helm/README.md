# Helm

The local stack consumes upstream Helm charts through Terraform `helm_release`
resources. Static, non-secret local chart values live in YAML files here.
Terraform modules layer dynamic values on top for generated credentials, service
contract endpoints, Secret references, and bootstrap run markers.

## Structure

```text
infra/helm/
└── values/
    └── local/
        ├── seaweedfs.yaml
        ├── polaris.yaml
        ├── trino.yaml
        ├── superset.yaml
        ├── dagster.yaml
        ├── traefik.yaml
        └── cert-manager.yaml
```

## Local charts

- **SeaweedFS** (`seaweedfs/seaweedfs`)
  Chart source: https://seaweedfs.github.io/seaweedfs/helm
- **Apache Polaris** (`polaris/polaris`)
  Chart source: https://downloads.apache.org/polaris/helm-chart
- **Trino** (`trino/trino`)
  Chart source: https://trinodb.github.io/charts
- **Apache Superset** (`superset/superset`)
  Chart source: http://apache.github.io/superset/
- **Dagster** (`dagster/dagster`)
  Chart source: https://dagster-io.github.io/helm
- **Traefik** (`traefik/traefik`)
  Chart source: https://traefik.github.io/charts
- **cert-manager** (`jetstack/cert-manager`)
  Chart source: https://charts.jetstack.io

## Workflow

```bash
uv run --project tools/olf --locked olf deploy --provider local --phase foundation
uv run --project tools/olf --locked olf deploy --provider local --phase platform
uv run --project tools/olf --locked olf deploy --provider local --phase artifacts
uv run --project tools/olf --locked olf deploy --provider local
uv run --project tools/olf --locked olf forward --provider local
uv run --project tools/olf --locked olf destroy --provider local
uv run --project tools/olf --locked olf destroy --provider local --phase foundation
```
