#!/usr/bin/env python3
"""Render the README and AWS overview charts into docs/assets/.

    python3 docs/architecture/diagrams/src/overview/charts.py

Writes a light and a dark SVG for each chart: architecture-local, stage-promotion
and architecture-aws. Coordinates are hand-placed on a 1320-wide canvas.
"""

from __future__ import annotations

from lib import a, bucket, chip, defs, edge, export, image, legend, node, text, tool, zone

W = 1320


def _data_path(storage_logo, storage_name, buckets, ops_name, catalog, database, om_catalog):
    """Orchestration and data-path zones shared by the local and AWS charts."""
    zone(12, 12, 1296, 128, "ORCHESTRATE")
    zone(12, 156, 1296, 548, "DATA PATH")

    a('<rect class="card" x="214" y="40" width="850" height="84" rx="14"/>')
    chip(228, 52, 60, "dagster")
    text(302, 70, "ORCHESTRATE & MONITOR", "stage")
    text(302, 96, "Dagster", "title big")
    text(420, 96, "webserver · daemon · code server · one isolated run pod per pipeline run")

    y, nh, nw = 214, 116, 160
    node(24, y, 120, nh, None, "INPUT", "Sources", ["CSV examples", "per data product"])
    node(214, y, nw, nh, "dlt", "EXTRACT", "dlt", ["loads raw files", "into Bronze"])
    node(444, y, nw, nh, "floe", "VALIDATE", "Floe", ["technical contracts", "Bronze → Silver"])
    node(674, y, nw, nh, "dbt", "TRANSFORM", "dbt-trino", ["business marts", "Silver → Gold"])
    node(904, y, nw, nh, "trino", "QUERY ENGINE", "Trino", ["runs Gold SQL", "serves queries"])
    node(1134, y, nw, nh, "superset", "DASHBOARDS", "Superset", ["reports over Gold", "full preset"],
         optional=True)

    edge("M294,124 L294,212", "ctl", "launches run pod", 302, 172, "start")
    edge("M524,124 L524,212", "ctl", "1 K8s Job per entity", 532, 172, "start")
    edge("M754,124 L754,212", "ctl", "dagster-dbt", 762, 172, "start")
    edge(f"M144,{y+58} L212,{y+58}", "data", "reads", 178, y + 50)
    edge(f"M834,{y+58} L902,{y+58}", "data", "SQL", 868, y + 50)
    edge(f"M1134,{y+58} L1066,{y+58}", "data", "SQL", 1100, y + 50)

    sb = 404
    a(f'<rect class="band" x="180" y="{sb}" width="920" height="150" rx="14"/>')
    a(f'<rect class="tab" x="912" y="{sb-20}" width="176" height="44" rx="10"/>')
    chip(918, sb - 16, 36, storage_logo, pad=3)
    text(962, sb - 1, "OBJECT STORAGE", "stage")
    text(962, sb + 17, storage_name, "title")
    bt = sb + 46
    bronze, silver, gold = buckets
    bucket(294, bt, 164, bronze[0], bronze[1], "bronze")
    bucket(524, bt, 164, silver[0], silver[1], "silver", True)
    bucket(794, bt, 164, gold[0], gold[1], "gold", True)
    bucket(1010, bt, 132, ops_name[0], ops_name[1], "ops")

    edge(f"M294,{y+nh} L294,{bt-12}", "data", "lands", 302, 370, "start")
    edge(f"M378,{bt+40} L440,{bt+40}", "data")
    edge(f"M504,{y+nh} L504,{bt-12}", "data", "writes Silver", 498, 370, "end")
    edge(f"M608,{bt+40} L710,{bt+40}", "data", "SQL in Trino", 659, bt + 32)
    edge(f"M960,{y+nh} C960,372 820,368 820,{bt-12}", "data", "writes Gold", 872, 386, "end")

    lb = 352
    a(f'<g class="edge lin"><path d="M552,{y+nh} L552,{lb} M782,{y+nh} L782,{lb} M552,{lb} L1206,{lb} '
      f'L1206,{sb-2}" marker-end="url(#ah-lin)"/></g>')
    a(f'<rect class="olbg" x="610" y="{lb-13}" width="118" height="26" rx="6"/>')
    image("openlineage", 616, lb - 9, 106, 20)
    node(1110, sb, 190, 150, "openmetadata", "GOVERNANCE", "OpenMetadata",
         ["lineage from Floe and", "dbt-trino; crawls the", om_catalog, "full preset"],
         optional=True, tcls="title sm", cs=42)
    edge(f"M1150,{sb} C1130,376 1080,368 1050,{y+nh+2}", "lin", "crawls hourly", 1078, 377, "end")

    cy = 586
    logo, stage, title, subs = catalog
    node(444, cy, 330, 112, logo, stage, title, subs)
    edge(f"M524,{cy} L524,{bt+90}", "ctl", "metadata", 532, cy - 10, "start")
    edge(f"M754,{cy} L754,{bt+90}", "ctl")
    logo, title, subs, tcls = database
    node(804, cy, 296, 112, logo, "SERVICE STATE", title, subs, tcls=tcls)


def _access(ty, entry):
    """User → entry hops → oauth2-proxy → Dex ⇄ GitHub."""
    for x, logo, name, sub in entry:
        tool(x, ty, logo, name, sub)
    tool(978, ty, "oauth2proxy", "oauth2-proxy", "session gate")
    tool(1100, ty, "dex", "Dex", "OIDC issuer")
    tool(1226, ty, "github", "GitHub", "login · teams")
    edge(f"M1036,{ty+29} L1096,{ty+29}", "data", "OIDC", 1066, ty + 21)
    edge(f"M1158,{ty+24} L1222,{ty+24}", "data")
    edge(f"M1222,{ty+38} L1160,{ty+38}", "data")
    text(1190, ty + 16, "connector", "elabel", "middle")


def local():
    h = 984
    defs()
    _data_path(
        "seaweedfs", "SeaweedFS",
        (("lakehouse-dev-bronze", "raw CSV, immutable"), ("lakehouse-dev-silver", "Iceberg tables"),
         ("lakehouse-dev-gold", "Iceberg tables")),
        ("openlakeforge-ops", "manifests · reports"),
        ("polaris", "ICEBERG REST CATALOG", "Apache Polaris", ["Floe commits and Trino", "resolves tables here"]),
        ("postgresql", "PostgreSQL", ["Dagster · Polaris ·", "OpenMetadata · Superset"], "title"),
        "catalog via Trino hourly",
    )
    zone(12, 720, 636, 208, "DEPLOY")
    zone(672, 720, 636, 208, "ACCESS")
    ty = 760
    tool(44, ty, "olf", "olf CLI", "deploy · e2e")
    tool(192, ty, "terraform", "Terraform", "platform root")
    tool(340, ty, "helm", "Helm", "pinned charts")
    chip(480, ty, 58, "kind", pad=6, w=86)
    chip(548, ty + 36, 34, "k8s", pad=3)
    text(523, ty + 76, "kind", "tname", "middle")
    text(523, ty + 92, "local Kubernetes", "tsub", "middle")
    edge(f"M104,{ty+29} L188,{ty+29}", "ctl", "applies", 146, ty + 21)
    edge(f"M252,{ty+29} L336,{ty+29}", "ctl", "installs", 294, ty + 21)
    edge(f"M400,{ty+29} L476,{ty+29}", "ctl", "releases on", 438, ty + 21)

    text(716, ty + 34, "User", "tname", "middle")
    text(716, ty + 50, "browser", "tsub", "middle")
    tool(784, ty, "traefik", "Traefik", "ingress")
    chip(846, ty + 38, 34, "certmanager", pad=3)
    text(884, ty + 60, "TLS", "tsub")
    _access(ty, ())
    edge(f"M744,{ty+29} L780,{ty+29}", "data")
    edge(f"M844,{ty+20} L974,{ty+20}", "data", "HTTPS", 912, ty + 12)
    text(990, ty + 128, "then to the portal, Dagster, Superset and OpenMetadata UIs", "tsub", "middle")

    legend(956, ("data", "ctl", "lin", "opt"))
    return export(
        "architecture-local", W, h, "OpenLakeForge architecture",
        "Dagster launches dlt, Floe and dbt-trino. dlt lands raw CSV in the Bronze bucket, Floe validates "
        "it into Silver Iceberg tables through Polaris, and dbt-trino builds Gold marts by running SQL in "
        "Trino. Superset queries Gold through Trino. Floe and dbt-trino send OpenLineage to OpenMetadata, "
        "which crawls the catalog through Trino every hour. olf applies Terraform, which installs Helm "
        "releases on kind. Users reach the UIs through Traefik, oauth2-proxy and Dex with GitHub login.",
    )


def aws():
    h = 936
    defs()
    _data_path(
        "s3", "Amazon S3",
        (("bronze bucket", "raw CSV, per stage"), ("silver bucket", "Iceberg, per stage"),
         ("gold bucket", "Iceberg, per stage")),
        ("ops bucket", "revisions · reports"),
        ("glue", "ICEBERG CATALOG", "AWS Glue Data Catalog",
         ["one shared catalog for every stage,", "databases prefixed lakehouse_<stage>_"]),
        ("rds", "Amazon RDS for PostgreSQL", ["Dagster · OpenMetadata · Superset,",
                                              "one database per stage instance"], "title sm"),
        "Glue catalog via Trino",
    )
    zone(12, 720, 636, 160, "DEPLOY")
    zone(672, 720, 636, 160, "ACCESS")
    ty = 760
    tool(44, ty, "olf", "olf CLI", "deploy · e2e")
    tool(172, ty, "terraform", "Terraform", "infrastructure")
    tool(300, ty, "helm", "Helm", "pinned charts")
    tool(428, ty, "eks", "Amazon EKS", "cluster")
    tool(560, ty, "ecr", "Amazon ECR", "images")
    edge(f"M104,{ty+29} L168,{ty+29}", "ctl", "applies", 136, ty + 21)
    edge(f"M232,{ty+29} L296,{ty+29}", "ctl", "installs", 264, ty + 21)
    edge(f"M360,{ty+29} L424,{ty+29}", "ctl", "onto", 392, ty + 21)
    edge(f"M556,{ty+29} L490,{ty+29}", "data", "pulls", 523, ty + 21)

    text(702, ty + 34, "User", "tname", "middle")
    _access(ty, ((744, "route53", "Route 53", "DNS"), (852, "alb", "ALB", "load balancer")))
    chip(900, ty + 38, 34, "acm", pad=3)
    text(938, ty + 60, "ACM", "tsub")
    edge(f"M724,{ty+29} L740,{ty+29}", "data")
    edge(f"M802,{ty+29} L848,{ty+29}", "data")
    edge(f"M910,{ty+20} L974,{ty+20}", "data", "HTTPS", 948, ty + 12)

    legend(908, ("data", "ctl", "lin", "opt"))
    return export(
        "architecture-aws", W, h, "OpenLakeForge on AWS",
        "The target AWS shape: on Amazon EKS, Dagster launches dlt, Floe and dbt-trino. Data lands in "
        "per-stage Amazon S3 buckets; Silver and Gold are Iceberg tables in the AWS Glue Data Catalog. "
        "Trino runs Gold SQL and serves Superset. Services keep state in Amazon RDS for PostgreSQL. olf "
        "applies Terraform and Helm onto EKS, which pulls images from Amazon ECR. Users come in through "
        "Route 53, an Application Load Balancer with ACM, oauth2-proxy and Dex with GitHub login.",
    )


def _step(x, w, n, label, title, cls=""):
    a(f'<rect class="step {cls}" x="{x}" y="20" width="{w}" height="262" rx="18"/>')
    a(f'<circle class="num {cls}" cx="{x+28}" cy="48" r="14"/>')
    text(x + 28, 53, str(n), "numt", "middle")
    text(x + 52, 44, label, "stage")
    text(x + 52, 64, title, "title")


def _pill(x, y, w, s, cls=""):
    a(f'<rect class="pill {cls}" x="{x}" y="{y}" width="{w}" height="34" rx="17"/>')
    text(x + w / 2, y + 22, s, "ptxt mono" if cls == "mono" else "ptxt", "middle")


def _down(x, y1, y2):
    edge(f"M{x},{y1} L{x},{y2}", "data")


def _stage_box(x, name, label):
    a(f'<rect class="ns {name}" x="{x}" y="340" width="630" height="262" rx="16"/>')
    text(x + 18, 366, f"NAMESPACE olf-{name}", "stage")
    text(x + 18, 390, label, "title")
    chip(x + 430, 352, 36, "dagster", pad=4)
    chip(x + 472, 352, 36, "superset", pad=4)
    text(x + 516, 368, "Dagster, Superset", "tsub")
    text(x + 516, 384, "run sha256:<rev>", "tsub")
    for i, tier in enumerate(("bronze", "silver", "gold")):
        bucket(x + 120 + i * 195, 430, 176, f"lakehouse-{name}-{tier}",
               "raw CSV" if tier == "bronze" else "Iceberg tables", tier, tier != "bronze")
    text(x + 315, 568, f"catalog lakehouse_{name}", "bname", "middle")
    text(x + 315, 588, "its own buckets, catalog and database", "tsub", "middle")


def stages():
    h = 766
    defs()
    _step(12, 250, 1, "WRITE CODE", "Your repository")
    chip(204, 34, 44, "github", pad=6)
    _pill(32, 100, 210, "feature branch")
    _down(137, 134, 154)
    _pill(32, 156, 210, "pull request · review")
    _down(137, 190, 210)
    _pill(32, 212, 210, "merge into main", "strong")

    _step(278, 446, 2, "BUILD ONCE · build-revision", "One image, one revision", "accent")
    a('<rect class="card" x="294" y="92" width="226" height="174" rx="14"/>')
    chip(308, 106, 52, "github", pad=8)
    chip(368, 106, 52, "ecr", pad=6)
    text(308, 182, "Image package", "mtitle")
    text(308, 204, "project-code:<commit-sha>", "mono")
    text(308, 222, "pinned by @sha256:<digest>", "mono")
    text(308, 248, "GHCR, or Amazon ECR on AWS")
    edge("M520,180 L556,180", "data", "digest", 538, 172)
    a('<rect class="card" x="558" y="92" width="152" height="174" rx="14"/>')
    chip(572, 106, 52, "olf", pad=7)
    text(572, 182, "Project revision", "mtitle")
    text(572, 204, "sha256:<rev>", "mono")
    text(572, 226, "code + image digest")
    text(572, 248, "kept in the ops bucket")

    _step(740, 270, 3, "DEPLOY DEV", "Activate and test", "dev")
    _pill(760, 100, 230, "deploy-stage --stage dev", "mono")
    _down(875, 134, 154)
    _pill(760, 156, 230, "runs in olf-dev")
    _down(875, 190, 210)
    _pill(760, 212, 230, "e2e tests pass", "strong")

    _step(1026, 282, 4, "PROMOTE TO PROD", "Same revision", "prod")
    _pill(1046, 100, 242, "approval · prod reviewers", "gate")
    _down(1167, 134, 154)
    _pill(1046, 156, 242, "deploy-stage --stage prod", "mono")
    _down(1167, 190, 210)
    _pill(1046, 212, 242, "no rebuild, same digest", "strong")
    for x1, x2 in ((262, 276), (724, 738), (1010, 1024)):
        edge(f"M{x1},151 L{x2},151", "data")

    a('<rect class="zone" x="12" y="300" width="1296" height="418" rx="18"/>')
    text(28, 322, "ONE KUBERNETES CLUSTER · kind locally, Amazon EKS on AWS", "zlabel")
    _stage_box(24, "dev", "DEV stage")
    _stage_box(666, "prod", "PROD stage")
    a('<rect class="card" x="24" y="618" width="1272" height="86" rx="14"/>')
    text(42, 644, "NAMESPACE olf-system · SHARED BY BOTH STAGES", "stage")
    shared = (("trino", "Trino"), ("polaris", "Polaris / Glue"), ("seaweedfs", "SeaweedFS / S3"),
              ("postgresql", "PostgreSQL"), ("openmetadata", "OpenMetadata"), ("dex", "Dex"))
    for i, (logo, name) in enumerate(shared):
        x = 42 + i * 205
        chip(x, 656, 36, logo, pad=4)
        text(x + 46, 679, name, "tname")
    edge("M875,282 L875,312 Q875,322 865,322 L540,322 Q530,322 530,332 L530,338", "data", "activates", 700, 316)
    edge("M1167,282 L1167,338", "data", "activates", 1175, 318, "start")

    legend(742, ("data", "dev", "prod"))
    return export(
        "stage-promotion", W, h, "OpenLakeForge stage promotion",
        "A developer merges a pull request into main. The build job pushes one project-code image to "
        "GHCR, or ECR on AWS, and freezes the code and that image digest into one project revision. The "
        "revision is activated in the DEV namespace and tested, then, after approval, activated unchanged "
        "in PROD. Each stage has its own buckets and catalog; both share the services in olf-system.",
    )


if __name__ == "__main__":
    for render in (local, aws, stages):
        for path in render():
            print(f"wrote {path.name} ({path.stat().st_size // 1024} KB)")
