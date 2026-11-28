# Metadata ownership

How OpenLakeForge decides which OpenMetadata entities and fields it writes, how it marks them, and what to do when
OpenMetadata and Git disagree. Read it before editing an OpenLakeForge-managed entity in the OpenMetadata UI.

## Who writes what

OpenMetadata holds metadata from three writers: Git, the Trino crawl and the pipelines at run time. A deploy changes
only the fields in the middle column and leaves the rest alone.

| Entity | Written by `olf infra openmetadata deploy-metadata` (Git) | Never touched by a deploy |
| --- | --- | --- |
| Domain | `displayName`, `description`, `domainType` | owners, tags, conversations, followers |
| Data product | `displayName`, `description`, its assets (the tables under its Gold schema), the domain it is created in | tags, owners, manually attached assets |
| Database schema (Silver, Gold) | the domain it belongs to | tags, owners, description |
| Table | created empty if absent | columns and column descriptions (Trino crawl), lineage (OpenLineage), quality results, tags |
| Storage service and containers | display name, S3 region and endpoint; container `description` | everything else |

Columns and their descriptions come from the Trino crawl, lineage from the pipelines' OpenLineage events, and quality
results from the quality writer (ADR 0007). A deploy never deletes: an entity removed from Git stays in
OpenMetadata until someone removes it by hand.

## The ownership marker

Every domain, data product, database schema, table and container a deploy creates or manages carries the custom
property `olfManaged`. Its value is `git:<revision>`, the commit of the project checkout that last deployed it, or
`git:unknown` outside a Git checkout. A deploy defines the property on each entity type when it is missing.
Domains and data products are shared by every stage, so they carry the revision of whichever stage deployed last;
schemas, tables and containers carry their own stage's.

An entity without the marker was not created or adopted by OpenLakeForge. A table the crawl created gets the marker
on the first deploy that registers it.

## Re-deploying

A deploy reads each entity first and sends a JSON Patch only for the owned fields that differ, so deploying the same
revision twice makes no write request. Deploying a new revision rewrites only the marker's revision.

A human edit to an owned field, such as a data product's description, is overwritten by the next deploy. Make the
change in `lakehouse_code/governance.yml` instead.

## Check for drift

`olf infra openmetadata drift` compares Git with a stage's OpenMetadata and lists each difference. It sends only
read requests, so it is safe against any stage, and it exits non-zero when it finds something. It needs a deployed
platform and the stage's contract environment, like `deploy-metadata`; pass `--stage` to pick the stage. The Full
e2e (`olf infra e2e run --env local`) runs it after a deploy and fails if the report is not empty.

| Kind | Meaning |
| --- | --- |
| `missing` | Declared in Git, absent in OpenMetadata, for example a table the crawl has not found yet |
| `stale` | Carries `olfManaged`, no longer declared in Git (renamed or removed) |
| `changed` | An owned field differs from Git, including a human edit (the next deploy overwrites it), a data product that does not hold every table of its Gold schema (the next deploy attaches them), and a data product moved to another domain (a deploy never moves it; see the repair steps) |
| `unresolved` | A reference cannot be resolved, for example a data product whose domain does not exist |

Domains and data products are shared by every stage, so they report under stage `shared`. Today's report covers
domains, data products, schemas, tables and containers.

## Repair by hand

1. `stale`: an entity renamed or removed in Git stays in OpenMetadata, because a deploy never deletes. Open it in the
   UI and delete it once nothing else depends on it.
2. `changed`: to keep the edit, change `lakehouse_code/governance.yml` to match it. To discard it, run
   `olf infra openmetadata deploy-metadata`, which restores Git's value. A data product's domain is set only when the
   deploy creates the product; to move one, edit its domain in the UI.
3. `missing`: run the product's pipeline, wait for the Trino crawl, then rerun `deploy-metadata`.
4. A table registered under the wrong schema: delete it in the UI, then rerun `deploy-metadata`.
