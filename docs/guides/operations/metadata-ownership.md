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

An entity without the marker was not created or adopted by OpenLakeForge. A table the crawl created gets the marker
on the first deploy that registers it.

## Re-deploying

A deploy reads each entity first and sends a JSON Patch only for the owned fields that differ, so deploying the same
revision twice makes no write request. Deploying a new revision rewrites only the marker's revision.

A human edit to an owned field, such as a data product's description, is overwritten by the next deploy. Make the
change in `lakehouse_code/governance.yml` instead.

## Repair by hand

1. An entity renamed or removed in Git remains in OpenMetadata. Open it in the UI and delete it there once nothing
   else depends on it.
2. A data product that should leave a domain: edit its domain in the UI. A deploy sets the domain only when it
   creates the product.
3. A table registered under the wrong schema: delete it in the UI, then rerun
   `olf infra openmetadata deploy-metadata` so it is registered under the right one.
