#!/usr/bin/env python3
"""Chart 1 — Cluster Pod Census. Verified via `helm template` with the
project's own values files: 20 pods at rest (14 Deployments, 6 StatefulSets)."""
from pathlib import Path
from k8ssvg import Chart

c = Chart(1180, 1290, "Cluster Pod Census",
          "olf-system + one olf-<stage> per stage · kind cluster openlakeforge-local · long-lived services + on-demand Jobs")

# namespace boundary
c.box(28, 92, 1124, 1168, "namespaces: olf-system (shared) + olf-<stage>", color="control", title_size=14)

ROW1, ROW2, ROW_ING, ROW3 = 150, 436, 636, 860
IY1, IY2 = 46, 152  # icon y-offsets inside a row-1 card

# --- Row 1 ---
c.box(52, ROW1, 350, 258, "Dagster — 3 pods", color="platform", fill="#FFFFFF")
c.icon(140, ROW1 + IY1, "deploy", "webserver", label2="dagster chart 1.13.7")
c.icon(316, ROW1 + IY1, "deploy", "daemon", label2="K8sRunLauncher")
c.icon(228, ROW1 + IY2, "deploy", "code server", label2="lakehouse_code.definitions")

c.box(422, ROW1, 350, 258, "SeaweedFS — 4 pods", color="platform", fill="#FFFFFF")
c.icon(510, ROW1 + IY1, "sts", "master", label2=":9333")
c.icon(686, ROW1 + IY1, "sts", "volume", label2="maxVolumes 32")
c.icon(510, ROW1 + IY2, "sts", "filer", label2=":8888")
c.icon(686, ROW1 + IY2, "deploy", "s3 gateway", label2=":8333")

c.box(792, ROW1, 350, 258, "Superset — 3 pods", color="platform", fill="#FFFFFF")
c.icon(880, ROW1 + IY1, "deploy", "node", label2=":8088")
c.icon(1056, ROW1 + IY1, "deploy", "worker", label2="celery, solo pool")
c.icon(880, ROW1 + IY2, "sts", "redis", label2="cache + queue")

# --- Row 2 ---
c.box(52, ROW2, 350, 180, "OpenMetadata — 2 pods", color="platform", fill="#FFFFFF")
c.icon(140, ROW2 + IY1, "deploy", "server", label2=":8585")
c.icon(316, ROW2 + IY1, "sts", "opensearch", label2="single node")

c.box(422, ROW2, 168, 180, "PostgreSQL — 1", color="platform", fill="#FFFFFF")
c.icon(506, ROW2 + IY1, "sts", "shared db", label2="metadata + catalog")

c.box(606, ROW2, 168, 180, "Polaris — 1", color="platform", fill="#FFFFFF")
c.icon(690, ROW2 + IY1, "deploy", "catalog", label2=":8181 · JDBC")

c.box(790, ROW2, 168, 180, "Trino — 1", color="platform", fill="#FFFFFF")
c.icon(874, ROW2 + IY1, "deploy", "coordinator", label2="workers: 0 · 2G heap")

c.badge(978, ROW2, 164, 380,
        ["20 pods", "at steady state", "", "14 Deployments", "6 StatefulSets"],
        color="control")

# --- Ingress row: olf-system only; routes are Ingress objects ---
c.box(52, ROW_ING, 906, 180, "Ingress — 5 pods · olf-system", color="platform", fill="#FFFFFF")
ING = [52 + 906 * (i + 0.5) / 5 for i in range(5)]
c.icon(ING[0], ROW_ING + IY1, "deploy", "traefik", label2="hostPort 80/443")
c.icon(ING[1], ROW_ING + IY1, "deploy", "cert-manager", label2="controller · local CA")
c.icon(ING[2], ROW_ING + IY1, "deploy", "cainjector", label2="cert-manager")
c.icon(ING[3], ROW_ING + IY1, "deploy", "webhook", label2="cert-manager")
c.icon(ING[4], ROW_ING + IY1, "deploy", "portal", label2="landing page")

# --- Row 3: ephemeral, grouped by what creates each Job ---
c.box(52, ROW3, 1090, 360,
      "Ephemeral workloads — created on demand, never part of the 20",
      color="ephemeral", fill="#FAF6FC", dashed=True)

# each sub-box spaces its own icons evenly, so a label never hangs off the group
# it belongs to (lint_charts.py enforces this)
def cols(x, w, n):
    return [x + w * (i + 0.5) / n for i in range(n)]


PER_RUN = cols(68, 434, 2)
SCHEDULED = cols(518, 608, 3)
BOOTSTRAP = cols(68, 1058, 6)
EY1 = ROW3 + 82   # icon top, upper sub-boxes
EY2 = ROW3 + 240  # icon top, lower sub-box

# upper left — the per-run pair, the only Jobs tied to a pipeline run
c.box(68, ROW3 + 44, 434, 148, "Per pipeline run · Dagster creates these",
      color="ephemeral", title_size=13)
c.icon(PER_RUN[0], EY1, "job", "dagster run pod", variant="ephemeral", label2="TTL 1h")
c.icon(PER_RUN[1], EY1, "job", "floe runner", variant="ephemeral",
       label2="1 per entity · TTL 1h")

# upper right — everything on a clock, whether K8s' or OpenMetadata's
c.box(518, ROW3 + 44, 608, 148, "Scheduled · cluster clock",
      color="ephemeral", title_size=13)
c.icon(SCHEDULED[0], EY1, "cronjob", "k8s-log-archive", variant="ephemeral",
       label2="*/15 min · keeps 1")
c.icon(SCHEDULED[1], EY1, "cronjob", "OM catalog refresh", variant="ephemeral",
       label2="hourly · keeps 3")
c.icon(SCHEDULED[2], EY1, "job", "OM ingestion", variant="ephemeral",
       label2="OM scheduler · ttl 3600s")

# lower — one-shot, created by Terraform (or a Helm hook) at platform apply
c.box(68, ROW3 + 202, 1058, 142, "Bootstrap · once per platform apply (Phase 1)",
      color="ephemeral", title_size=13)
c.icon(BOOTSTRAP[0], EY2, "job", "polaris bootstrap", variant="ephemeral",
       label2="x2 Jobs · no TTL")
c.icon(BOOTSTRAP[1], EY2, "job", "seaweedfs buckets", variant="ephemeral",
       label2="x4 buckets · no TTL")
c.icon(BOOTSTRAP[2], EY2, "job", "postgresql bootstrap", variant="ephemeral", label2="no TTL")
c.icon(BOOTSTRAP[3], EY2, "job", "openmetadata bootstrap", variant="ephemeral",
       label2="x2 Jobs · no TTL")
c.icon(BOOTSTRAP[4], EY2, "job", "superset init", variant="ephemeral",
       label2="Helm hook · gone on success")
c.icon(BOOTSTRAP[5], EY2, "job", "cert-manager check", variant="ephemeral",
       label2="post-install hook only")

n = c.write(str(Path(__file__).resolve().parent.parent / "chart1-cluster-pod-census.svg"))
print("chart1 svg:", n, "bytes")
