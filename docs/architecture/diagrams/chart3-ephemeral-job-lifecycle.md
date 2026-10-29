# Chart 3 — Ephemeral Kubernetes Job Lifecycle

**Orchestration and ingestion run in ephemeral pods, not long-lived ones.** Clicking
*Launch* on a Dagster job creates one ephemeral Kubernetes Job for the run pod, from the
code location's own project-code image. The run pod lands Bronze with dlt itself, then
launches **one Floe Kubernetes Job per domain** on the same image; that Job validates the
domain's selected entities one after another. A product with six Silver inputs in one
domain (e.g. `order_revenue`) creates one Floe Job that validates those six entities,
never concurrently. Every Job is garbage-collected on TTL, so between runs no run or Floe
pod remains.

Gold is the deliberate exception: dbt does **not** do its work in the pod — `dbt-trino`
pushes the SQL into the **long-lived Trino service**, where the transformation compute
and its resource contention live (chart 2). So Bronze (dlt) is sized by the run pod,
Silver (Floe) by each domain's Floe Job, and Gold at the shared Trino coordinator. The
per-run footprint — everything except the standing platform services — returns to zero
between runs.

## The run

```mermaid
sequenceDiagram
    autonumber
    actor User
    box rgb(228,235,248) Control plane
        participant UI as Dagster webserver
        participant Daemon as Dagster daemon
        participant API as Kubernetes API
    end
    box rgb(240,232,245) Ephemeral · per run
        participant Run as Run pod (Job)
        participant Floe as Floe Job (per domain)
    end
    box rgb(233,233,237) Data plane
        participant Trino
        participant Polaris as Iceberg catalog
        participant S3 as Object storage
    end

    User->>UI: Launch order_revenue_pipeline
    UI->>Daemon: enqueue run
    Daemon->>API: create the run pod Job
    Note over Run: image project-code · SA dagster · TTL 1h

    Note over Run: ① Bronze — dlt
    Run->>S3: land raw entities in lakehouse-dev-bronze

    Note over Run: ② Silver — one Floe Job per domain
    Run->>API: create the sales Floe Job (--entities, 6 for order_revenue)
    Note over Floe: image project-code · SA dagster · TTL 1h · Secrets by reference
    loop once per entity, sequential
        Floe->>S3: read Bronze, validate against the contract
        Floe->>Polaris: commit validated Iceberg tables (OAuth)
        Note over Floe: rejects → quarantined CSV
        Floe->>S3: write the entity's run report
    end
    Run->>API: poll the Job, read its pod log (Floe JSON events)

    Note over Run: ③ Gold — dbt, SQL runs in Trino
    Run->>Trino: dbt build (dbt-trino)
    Trino->>Polaris: resolve Silver, commit Gold marts
    Trino->>S3: write Gold Iceberg data

    Run-->>Daemon: run succeeded · logs + artifacts already in S3
    Note over Run,Floe: Jobs TTL-collected — no run or Floe pods remain (Trino stays up)
```

The exact values live in the Terraform module and the stage's rendered Floe profile: the
run pod uses `ttlSecondsAfterFinished: 3600` and ServiceAccount `dagster`, carries the
contract-derived environment variables plus `envFrom` secrets, and runs each product job
with `max_concurrent: 1` because the profile's Floe orchestration is `sequential`. Each
Floe Job uses the same TTL and ServiceAccount, the run pod's image digest, the run pod's
rendered profile as text in `FLOE_PROFILE_CONTENT`, and only the Secrets the runner
references: the Polaris Floe client, the storage keys, and the
ingestion-bot JWT when governance is on.

## Where each engine does its work

| | Floe (Silver) | dbt (Gold) |
| --- | --- | --- |
| Orchestrated from | The run pod | The run pod |
| Executes in | **One Floe Job per domain** | **Trino** — dbt-trino pushes the SQL down |
| Version | `floe-python`, pinned in `project-code` | `dbt-trino`, pinned in `project-code`; Trino via Helm |
| Upgrade path | Bump `floe-python` and `dagster-floe`; rebuild `project-code` | Bump dbt in `project-code`; Trino via Helm |
| Storage access | S3 directly + catalog commits | Trino reads/writes S3; the run pod touches no data |
| Credentials | `polaris-floe-creds`, a `secretKeyRef` on the Floe Job | none — dbt reaches the catalog through Trino, which holds `polaris-trino-creds` (the bootstrap deletes the old `polaris-dbt-creds`) |

Floe holds an entity's frames in its domain Job's memory while it validates them, so that
Job, not the run pod, is what bounds Silver; dbt is a thin client whose transformations execute inside
Trino — the run pod never holds Gold data in memory (PR #62 replaced the earlier DuckDB
materialization).

## Job lifecycle

`ttlSecondsAfterFinished` keeps completed Jobs from accumulating: Kubernetes deletes the
Job and its pod an hour after it reaches a terminal state, leaving the logs and reports
already written to object storage as the durable record.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Pending: Job created
    Pending --> Running: pod scheduled
    Running --> Succeeded: run succeeded
    Running --> Failed: run failed
    Succeeded --> TTLExpired: + 3600s
    Failed --> TTLExpired: + 3600s
    TTLExpired --> [*]: Job and pod deleted

    note right of Running
        Logs stream to object storage
        while the pod is alive
    end note
    note right of TTLExpired
        Durable record survives in
        s3://openlakeforge-ops/
    end note
```

## Observing it live

```sh
kubectl --context kind-openlakeforge-local -n olf-dev get jobs -w
```

A `dagster-run-<id>` Job appears for the run, then one `floe-<domain>-<suffix>` Job
(label `app=floe-runner`) per domain it validates; each disappears an hour after
completion.

## Source of truth

- [libs/product_dagster.py](../../../libs/product_dagster.py) — asset graph assembly from the Floe project
- [libs/floe_profile.py](../../../libs/floe_profile.py) — the stage's Floe profile: storages, catalog, lineage, the per-domain `kubernetes_job` runner, sequential orchestration
- [lakehouse_code/silver/floe.yml](../../../lakehouse_code/silver/floe.yml) — the Floe project
- [infra/terraform/modules/orchestration/dagster/main.tf](../../../infra/terraform/modules/orchestration/dagster/main.tf) — `K8sRunLauncher`, `ttlSecondsAfterFinished`, runtime env
- [libs/dbt/profiles/](../../../libs/dbt/profiles/) — the dbt profile template, one per query engine (`trino.yml`); every stage value is an `env_var()` reference
- [ADR 0006](../../adr/0006-dagster-runtime-and-code-locations.md), [ADR 0001](../../adr/0001-platform-baseline-and-component-stack.md)
