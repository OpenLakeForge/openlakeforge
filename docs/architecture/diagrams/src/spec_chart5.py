#!/usr/bin/env python3
"""Chart 5 — Provider Contracts. Engines consume the v3 shared platform and
selected-stage bindings, which resolve to a different adapter per deployment
target. Grey means the Terraform module is the same one local uses; orange means
it was swapped. Current Terraform roots still emit v2 during the v3 transition.
Sources: contracts.tf and docs/architecture/provider-contracts.md."""
from pathlib import Path
from k8ssvg import Chart, C

c = Chart(1180, 830, "Provider Contracts",
          "v3 shared platform + selected stage · engines consume interfaces, never implementations")

LABEL_X, LABEL_W = 52, 248
COL_W, COL_GAP = 270, 10
COL_X = [310, 590, 870]

# ---------- engines band ----------
c.box(52, 100, 1076, 158, "Engines — identical in all three targets",
      color="platform", fill="#FFFFFF", title_size=13)
ENGINES = [("deploy", "Dagster", "blue"), ("deploy", "Trino", "blue"),
           ("job", "Floe", "ephemeral"), ("deploy", "Superset", "blue"),
           ("pod", "dbt", "blue"), ("deploy", "OpenMetadata", "blue")]
for i, (kind, name, variant) in enumerate(ENGINES):
    c.icon(52 + 1076 * (i + 0.5) / 6, 138, kind, name, size=44, variant=variant)
c.label(590, 244, "same images · same Helm charts · same SQL · same contracts",
        size=11, anchor="middle")

# ---------- connector ----------
c.edge([(590, 258), (590, 300)], color="platform", width=3)
c.label(602, 282, "shared fields + selected stage", size=10.5, mono=True, color=C["platform"])

# ---------- matrix header ----------
HEAD_Y = 312
HEADERS = [("Local · kind", "storage"), ("Azure POC · AKS", "storage"),
           ("AWS POC · EKS", "managed")]
c.label(LABEL_X + 4, HEAD_Y + 20, "contract", size=12, weight="700", color=C["control"])
c.label(LABEL_X + 4, HEAD_Y + 36, "what consumers read", size=10, mono=True)
for x, (name, col) in zip(COL_X, HEADERS):
    c.badge(x, HEAD_Y, COL_W, 40, [name], color=col, size=13)

# ---------- matrix rows ----------
# (contract, fields, [(adapter, terraform source, reuses-local?) per column])
ROWS = [
    ("storage", "endpoint · buckets · region", [
        ("SeaweedFS S3 :8333", "storage/seaweedfs", True),
        ("SeaweedFS S3 on AKS", "storage/seaweedfs", True),
        ("Amazon S3", "storage/aws-s3", False)]),
    ("catalog", "catalog_type · uri · warehouse", [
        ("Polaris REST 1.4.1", "catalog/polaris", True),
        ("Polaris REST 1.4.1", "catalog/polaris", True),
        ("AWS Glue", "catalog/aws-glue", False)]),
    ("metadata database", "host · db names · secret refs", [
        ("PostgreSQL in-cluster", "storage/postgresql", True),
        ("PostgreSQL in-cluster", "storage/postgresql", True),
        ("RDS PostgreSQL", "storage/rds-postgresql", False)]),
    ("artifacts + images", "registry · artifact base URI", [
        ("kind image load", "foundations/local-kind", True),
        ("ACR push", "foundations/azure-aks", False),
        ("ECR · S3", "foundations/aws-eks", False)]),
    ("identity + secrets", "principals · secret names", [
        ("K8s Secrets (dev-only)", "identity.local_development_credentials", True),
        ("K8s Secrets · AKS OIDC ready", "identity.azure_workload_identity_ready", False),
        ("EKS Pod Identity", "identity.aws_pod_identity", False)]),
]
ROW_Y, ROW_H, ROW_PITCH = 372, 68, 88
for i, (name, fields, cells) in enumerate(ROWS):
    y = ROW_Y + i * ROW_PITCH
    c.label(LABEL_X + 4, y + 28, name, size=12.5, weight="700", color=C["ink"])
    c.label(LABEL_X + 4, y + 46, fields, size=9.5, mono=True)
    for x, (adapter, source, reused) in zip(COL_X, cells):
        c.cell(x, y, COL_W, ROW_H, adapter, source,
               color="storage" if reused else "managed")

n = c.write(str(Path(__file__).resolve().parent.parent / "chart5-provider-contracts.svg"))
print("chart5 svg:", n, "bytes")
