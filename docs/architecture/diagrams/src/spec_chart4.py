#!/usr/bin/env python3
"""Chart 4 — Medallion & Catalog Data Path. Bronze→Silver→Gold as layered
lanes, one product (sales/order_revenue) traced end to end. The Iceberg
catalog (Polaris locally, AWS Glue on the AWS POC) governs Silver AND Gold."""
from pathlib import Path
from k8ssvg import Chart, C

c = Chart(1180, 840, "Medallion & Catalog Data Path",
          "source-owned Bronze · domain-owned Silver · product-owned Gold · all governed by the catalog")

LANE_X, LANE_W = 260, 650
CHIP_Y_OFF = 92

# ---------- left column: the buckets ----------
c.cylinder(142, 205, 168, 80, "lakehouse-dev-bronze", color="bronze", sub="owner: source crm")
c.cylinder(142, 445, 168, 80, "lakehouse-dev-silver", color="silver", sub="owner: domain sales")
c.cylinder(142, 685, 168, 80, "lakehouse-dev-gold", color="gold", sub="owner: product order_revenue")
c.edge([(226, 205), (260, 205)], color="bronze")
c.edge([(226, 445), (260, 445)], color="silver")
c.edge([(226, 685), (260, 685)], color="gold")
c.chip(42, 530, 200, 28, "floe/rejected/…/*.csv", color="silver", dashed=True)
c.label(142, 576, "rejected rows, quarantined as CSV", size=10.5, anchor="middle")
c.label(142, 591, "exit 0 = success_or_rejected", size=10, anchor="middle", mono=True)

# ---------- Bronze lane ----------
c.box(LANE_X, 130, LANE_W, 150, "BRONZE — namespace: crm_bronze", color="bronze",
      fill="#8C5A2B10")
c.label(LANE_X + 16, 178, "landed as-is by dlt in the run pod — CSV in the seed products, any raw source", size=12)
for i, name in enumerate(["orders", "order_lines", "products", "channels", "promotions"]):
    c.chip(LANE_X + 20 + i * 124, 130 + CHIP_Y_OFF, 116, 32, name, color="bronze")

# ---------- Bronze -> Silver ----------
c.edge([(500, 280), (500, 370)], color="ephemeral", width=3)
c.icon(612, 284, "job", "Floe domain Job", size=46, variant="ephemeral",
       label2="validate · strict cast · reject")

# ---------- Silver lane ----------
c.box(LANE_X, 370, LANE_W, 150, "SILVER — namespace: sales_silver",
      color="silver", fill="#6B728010")
c.label(LANE_X + 16, 418, "validated Iceberg tables, committed through the catalog", size=12)
for i, name in enumerate(["orders", "order_lines", "products", "channels", "promotions"]):
    c.chip(LANE_X + 20 + i * 124, 370 + CHIP_Y_OFF, 116, 32, name, color="silver")
c.edge([(260, 505), (142, 505), (142, 528)], color="silver", dashed=True)
c.label(200, 497, "rejects", size=10, mono=True, color=C["silver"])

# ---------- Silver -> Gold ----------
c.edge([(500, 520), (500, 610)], color="ephemeral", width=3)
c.icon(600, 524, "job", "run pod · dbt build", size=46, variant="ephemeral",
       label2="dbt-trino → SQL runs in Trino")

# ---------- Gold lane ----------
c.box(LANE_X, 610, LANE_W, 150, "GOLD — namespace: order_revenue_gold",
      color="gold", fill="#A67C0010")
c.label(LANE_X + 16, 658, "business marts, materialized by Trino", size=12)
for i, name in enumerate(["mart_order_revenue_by_day", "mart_…_by_channel",
                          "mart_…_margin_by_product"]):
    c.chip(LANE_X + 20 + i * 212, 610 + CHIP_Y_OFF, 200, 32, name, color="gold")

# ---------- right column ----------
c.cylinder(1035, 205, 200, 74, "openlakeforge-ops", color="storage",
           sub="revisions · reports · logs")

c.box(930, 370, 220, 390, "Iceberg catalog", color="control", fill="#FFFFFF")
c.label(946, 426, "Polaris (local)", size=13, weight="700", color=C["ink"])
c.label(946, 444, "polaris:8181/api/catalog", size=10.5, mono=True)
c.label(946, 478, "AWS Glue (aws POC)", size=13, weight="700", color=C["ink"])
c.label(946, 496, "catalog_type = glue", size=10.5, mono=True)
c.label(946, 534, "warehouse lakehouse_dev", size=11.5, mono=True, color=C["ink"])
c.label(946, 556, "namespaces by owner:", size=11.5)
c.label(946, 574, "source_bronze · domain_silver", size=10.5, mono=True)
c.label(946, 592, "product_gold", size=10.5, mono=True)
c.label(946, 626, "allowed locations only:", size=11.5)
c.label(946, 644, "s3://lakehouse-dev-bronze/", size=10.5, mono=True)
c.label(946, 662, "s3://lakehouse-dev-{silver,gold}/", size=10.5, mono=True)
c.label(946, 696, "writers commit through the", size=11.5)
c.label(946, 712, "catalog — never raw paths", size=11.5)
c.edge([(930, 445), (910, 445)], color="control", dashed=True)
c.edge([(930, 685), (910, 685)], color="control", dashed=True)

# ---------- footer: consumption ----------
c.label(585, 800, "Gold is queried in place: Trino (iceberg.order_revenue_gold.…) → Superset dashboards",
        size=12.5, anchor="middle", color=C["ink"])

n = c.write(str(Path(__file__).resolve().parent.parent / "chart4-medallion-catalog.svg"))
print("chart4 svg:", n, "bytes")
