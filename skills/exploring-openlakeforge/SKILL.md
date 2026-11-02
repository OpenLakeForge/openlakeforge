---
name: exploring-openlakeforge
description: >-
  Explore an existing OpenLakeForge lakehouse through the olf CLI as its signed-in
  user. Use for discovering Trino tables and SQL data, OpenMetadata domains
  and data products, Dagster jobs and runs, archived logs, or investigating
  object-storage files. Read and investigate; do not deploy or mutate data.
license: Apache-2.0
---

# Exploring OpenLakeForge

Use `olf` from a lakehouse project with `openlakeforge.yaml`. If elsewhere,
pass `--profile-file /path/to/openlakeforge.yaml` to each platform command.
To operate inside an OpenLakeForge source checkout, prefix commands with
`uv run --project tools/olf --locked` from the repository root.

## Agent rules

- Run one `olf` invocation at a time. Do not use infrastructure credentials,
  Kubernetes access, service tokens, or admin commands to bypass a refusal.
- Use the person's session, never a workload identity. Check `olf whoami`.
  If sign-in is required, ask the person to complete `olf login`; it uses a
  browser. If GitHub identity is not enabled, personal SQL queries are
  unavailable; report that limitation rather than switching identities.
- **Read-only:** run only `SHOW`, `DESCRIBE`, `EXPLAIN`, or `SELECT` SQL.
  Do not launch runs, edit reports, change schemas or write data without a
  separate explicit request. Respect permission errors.
- Pick the requested stage (`dev`, `uat`, `prod`) explicitly. Domain and
  product lists are deployment-wide; SQL, jobs, runs and logs are stage-scoped.
- Prefer structured output. `--json` returns JSON for platform API reads;
  `olf query --format json` returns **JSON Lines**, one object per result row.

## Discover governance and data

```bash
olf whoami
olf domain list --json
olf product list --json
olf query "SHOW SCHEMAS" --stage dev --format json
olf query "SHOW TABLES FROM <schema>" --stage dev --format json
olf query "DESCRIBE <schema>.<table>" --stage dev --format json
```

Replace `<schema>` and `<table>` with names actually discovered. Trino uses
the stage's catalog automatically; refer to tables as `schema.table`.
Use domain/product associations to narrow the search, then inspect columns
before writing a query. The CLI lists OpenMetadata domains and products,
but does not yet expose full descriptions, owners, lineage or glossary.

To answer a data question, issue a small, projected, filtered `SELECT` with
a bounded `LIMIT` and `--format json`. Do not dump PII, `SELECT *`, or attempt
broad scans; `LIMIT` caps output rows, **not necessarily scanned bytes**.
Ask before an expensive query or one that would expose sensitive values.
For longer SQL, use the command's `--file` option.

## Inspect pipelines

```bash
olf jobs list --stage dev --json
olf runs list --stage dev --limit 20 --json
olf runs show RUN_ID --stage dev --json
olf logs --stage dev --limit 100 --json
```

Find the job first, then narrow runs with `--job JOB_NAME` and logs with
`--date YYYY-MM-DD`, `--hour`, `--pod` and `--container` if needed.
Logs are hourly archives: the current hour may not yet be available.
These commands inspect state; `olf runs launch` would start work and is
not part of this read-only skill.

## Object files and integration boundaries

The user CLI currently has **no bucket/object listing or download command**.
Do not invent one, expose storage credentials, or use an infra command to
bypass authorization. Query registered tables through Trino; explain that
raw bucket/object inventory is not available via the user CLI yet.

This skill is for shell-capable agents, including agents working alongside
Exmergo Dex. Dex's native Trino connector is a separate connection path:
do not assume it can reuse `olf`'s browser session or Polaris extra credential.

On errors, report the stage, requested operation, and actual refusal; never
silently fall back to elevated access.
