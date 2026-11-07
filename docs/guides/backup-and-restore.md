# Back up and restore a local deployment

How to back up a local OpenLakeForge deployment, and how to restore the backup
onto the same cluster or onto a cluster you destroyed and created again from
the same release. The commands cover the local provider only.

## What a backup holds

`olf infra backup` writes one directory:

| Path | Holds |
| --- | --- |
| `postgres/<database>.dump` | A `pg_dump -Fc` archive of each database: `polaris` first, then each stage's `dagster_<stage>`, `superset_<stage>` (analytics on), and `openmetadata_db` (governance on) and `dex` (Dex on). |
| `objects/<bucket>/<key>` | Every object of each stage's Bronze, Silver and Gold buckets and of the ops bucket, which holds the project revisions and their `ACTIVE.json` pointers. |
| `objects/<bucket>.inventory.json` | The key and size of each object in the bucket. |
| `secrets.enc` | The Secrets the databases depend on (Polaris principals, Superset, OpenMetadata, Dex and the platform API's service users, the GitHub sign-in Secret when `spec.identity.github` is set) and, with `spec.access.tls: local-ca`, `local-ca`, encrypted under your passphrase. |
| `manifest.json`, `SHA256SUMS` | The release, the SHA-256 of `openlakeforge.yaml`, the stages, and the name of everything above. No Secret value. |

It does not hold `openlakeforge.yaml` and `lakehouse_code/` (keep them in git),
Terraform state, the kubeconfig, or the OpenMetadata search index, which
restore rebuilds. The images a project revision names are not in it either:
they stay in your registry.

> [!WARNING]
> The directory holds all your data in the clear. Only `secrets.enc` is
> encrypted. Store it as you would the cluster itself.

## Back up

Run from the project root, with no pipeline run in flight:

```bash
olf infra backup --output ~/olf-backups/2026-11-07 -f openlakeforge.yaml
```

`--output` must be a new or empty directory; `olf` creates it readable by you
only. The command asks for a passphrase twice. To run it unattended, set
`OLF_BACKUP_PASSPHRASE` instead; it is never read from an argument.

While it runs, `olf` stops every stage's Dagster daemon so that no scheduled
run writes between one copy and the next, and it starts them again whatever
the outcome. It refuses to start while a run is queued or running: wait, then
retry. The databases are dumped before the buckets, so each Polaris pointer in
the dump names files that the copy holds.

Keep the passphrase apart from the backup, in a password manager or secrets
vault. Without it restore refuses the backup, because the Secrets cannot be
decrypted, and the databases name Polaris principals whose credentials only
`secrets.enc` holds.

Copy the directory off the machine and off the cluster's disk. A backup
that sits beside the cluster is lost with it. Check a copy with
`sha256sum --check SHA256SUMS` in the directory; restore runs the same check
first.

## Restore onto the running cluster

Use this to roll a deployment back to a backup. The platform stays deployed.

```bash
olf infra restore --from ~/olf-backups/2026-11-07
```

Run it from the project root, with the same release of `olf` and the same
`openlakeforge.yaml` that took the backup. Restore refuses a backup whose
release, profile hash, stages or checksums differ. It asks for the passphrase,
or reads `OLF_BACKUP_PASSPHRASE`.

## Destroy the cluster and restore onto a new one

1. Destroy the cluster. This deletes every object and run:

   ```bash
   olf infra destroy
   ```

2. Create the cluster and the platform again, without your code, from the same
   release and `openlakeforge.yaml`:

   ```bash
   olf infra platform apply -f openlakeforge.yaml
   ```

   This runs the foundation (the kind cluster), the image prefetch and the
   platform phase. The platform phase alone, `olf infra deploy --phase
   platform`, is what restore checks for, but it needs the cluster to exist.
   Do not run the artifacts phase: restore brings back the revision each stage
   had.

3. Restore:

   ```bash
   olf infra restore --from ~/olf-backups/2026-11-07
   ```

   Restore refuses a cluster that has no PostgreSQL and no Polaris, with the
   platform deploy as the fix. The Secrets are never put in place before that
   deploy, because a Polaris principal that is created while its Secret is
   already there deletes the Secret.

4. Check what survived (see [Check the result](#check-the-result)).

With `spec.identity.github`, the platform apply stops at the missing
`github-oauth-app` Secret
([Connect GitHub](identity/connect-github.md#use-an-oauth-app-instead)).
Create any placeholder Secret of that name in `olf-system` and apply again;
restore replaces it with the backed-up one.

### What restore does

In order:

1. Checks the checksums, release, profile, stages, passphrase and that the
   platform is deployed. Nothing changes if a check fails.
2. Stops the Dagster daemons and refuses while a run is in flight. Suspends
   each stage's CronJobs, because the log archiver writes to the ops bucket
   every 15 minutes.
3. Scales the workloads that hold a database open or read the Secrets to 0:
   Dagster, Superset, OpenMetadata, Polaris, Trino, Dex and the platform API.
4. Replaces the backed-up Secrets, drops and recreates each database with its
   owner and grants, and loads the dump.
5. Makes each bucket equal to the backup's. Objects written after the backup
   are deleted, because the pipelines read whatever a prefix holds and would
   ingest a stray file as data. The listing is compared with the inventory by
   key and size.
6. Scales back up: the shared services, then the stages, the daemons last.
   Resumes the CronJobs.
7. Reconciles the platform (the platform phase) and reactivates, in each stage,
   the project revision its restored `ACTIVE.json` names. It builds nothing, so
   a checkout that is newer than the backup does not replace the restored
   pipelines and dashboards. A stage with no pointer is left alone. If a
   revision's image no longer exists, as for one built with
   `olf infra project image --no-push`, the restore fails and names the stage,
   the revision and the image.
8. If the backup held `local-ca`, republishes the `olf-local-ca` ConfigMaps,
   has cert-manager sign again every Certificate the CA issued, and restarts
   the workloads that mount them.

If a step fails, the workloads stay at 0 replicas so that nothing serves
half-restored data. Fix the cause and run the same `olf infra restore` again; it
replaces everything and remembers each replica count.

### The local CA

A backup holds `local-ca` only when the routes serve HTTPS with it
(`spec.access.tls: local-ca`), because browsers trust that CA. A new cluster
mints a CA of its own, and restore puts the backed-up one back, so a machine
that trusted the old cluster's CA trusts the restored one again. If you trusted
the new cluster's CA before the restore, trust the current one:

```bash
olf infra access trust
```

With the default plain-HTTP routes (`tls: none`) the CA signs only
in-cluster certificates, such as Trino's (ADR 0016), and nothing outside the
cluster trusts it. The pods mount it by path, and no database holds it, so it
is disposable: the backup leaves it out, and a restore keeps the CA the
platform already runs with, including a rebuilt cluster's new one.

### Search

OpenSearch has no volume, so the OpenMetadata search index is empty after
the platform is deployed again. The entities are in the restored database. The
OpenMetadata bootstrap of the reconcile (step 7) finds the empty index and runs
the reindex, so search returns hits once it finishes (#909).

## Renaming buckets

Bucket names are `<metadata.name>-<stage>-<layer>` and `<metadata.name>-ops` (ADR 0004).
A deployment made before that rule has `lakehouse-<stage>-<layer>` stage buckets and an
`openlakeforge-ops` bucket, and changing `metadata.name` renames them too.

No step copies objects from an old bucket to a new one, and Polaris keeps pointing at the old names,
so `olf infra deploy` on the local or Azure provider refuses and lists each bucket it would rename:

```text
applying would rename already-deployed bucket(s): lakehouse-dev-bronze -> openlakeforge-dev-bronze, ...
```

A backup taken under the old names cannot be restored under the new ones: restore refuses a different
release or profile. To move to the new names without the data, run `olf infra destroy`, then
`olf infra deploy`, and run the pipelines again. To apply the rename over the existing platform and start
with empty buckets, pass `--allow-stage-removal`.

On AWS the guard does not apply. Set `legacy_dev_bucket_names = true` in the platform var-file to keep
the DEV buckets of the v0.2 POC. Without it, and for UAT and PROD, Terraform replaces each bucket whose name
changed, and the root creates them with `force_destroy`, so the objects in the old ones are deleted: copy them
first (`aws s3 sync`).

## Check the result

Record a stage before the backup and compare it after the restore. Neither
command launches a pipeline.

```bash
olf infra e2e durability capture --output dev-baseline.json -f openlakeforge.yaml --stage dev
# back up, destroy, deploy the platform, restore
olf infra e2e durability verify --baseline dev-baseline.json -f openlakeforge.yaml --stage dev
```

`verify` lists every table, run, dashboard, entity or object that is missing
or changed and exits non-zero if there is one. Capture and verify each enabled
stage.

## Limits

| Limit | Detail |
| --- | --- |
| Local provider only | `olf infra backup` and `restore` refuse AWS and Azure deployments. Their managed services back up their own state (#47). |
| Same release | Restore refuses a backup from another release. Restore on the release that took it, then upgrade. |
| Same profile | The backup records the SHA-256 of `openlakeforge.yaml`. Any edit, including a different stage set, is refused. |
| Platform deployed first | Restore does not create the cluster or the platform. |
| One operator at a time | Run `olf` commands one at a time, and stop `olf infra forward` first. |
| WSL2 clock | Restoring onto a new cluster runs the foundation again. With `systemd-timesyncd` active on WSL2 the clock steps back and Polaris rejects tokens, so restore or the checks after it fail at random. See [On WSL2](local-installation.md#on-wsl2). |

## Manual fallbacks

When `olf` cannot run, the same data can be moved by hand. These steps restore
data only. They do not restore the Secrets, and a Polaris dump names principals
whose credentials only `secrets.enc` holds, so a full restore onto a new
cluster needs `olf infra restore`.

Point `kubectl` at the cluster. The shared namespace is `olf-system`:

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
KUBECTL="$(olf toolchain path kubectl)"
```

### PostgreSQL

One StatefulSet pod, `postgresql-0` with the container `postgresql`, runs all
databases as the `postgres` superuser, which needs no password inside the pod.
Dump a database in the format `olf` uses:

```bash
"$KUBECTL" exec -n olf-system postgresql-0 -c postgresql -- \
  pg_dump -U postgres -Fc --no-owner --no-acl --dbname polaris > polaris.dump
```

To load it, scale the workloads that use the database to 0 first (for `polaris`:
`"$KUBECTL" scale deployment/polaris -n olf-system --replicas=0`), then recreate
the database with its owner and load the dump. The owner is the database name,
except `openmetadata_db`, whose owner is `openmetadata_user`:

```bash
DB=polaris OWNER=polaris
"$KUBECTL" exec -n olf-system postgresql-0 -c postgresql -- psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
  -c "DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE)" \
  -c "CREATE DATABASE \"$DB\" OWNER \"$OWNER\"" \
  -c "GRANT ALL PRIVILEGES ON DATABASE \"$DB\" TO \"$OWNER\"" \
  -c "REVOKE CONNECT ON DATABASE \"$DB\" FROM PUBLIC" \
  -c "GRANT CONNECT ON DATABASE \"$DB\" TO \"$OWNER\""
"$KUBECTL" exec -n olf-system postgresql-0 -c postgresql -- psql -v ON_ERROR_STOP=1 -U postgres -d "$DB" \
  -c "GRANT ALL ON SCHEMA public TO \"$OWNER\"" -c "ALTER SCHEMA public OWNER TO \"$OWNER\""
"$KUBECTL" exec -i -n olf-system postgresql-0 -c postgresql -- \
  pg_restore -U postgres --dbname "$DB" --no-owner --no-acl --role="$OWNER" < polaris.dump
```

Scale the workloads back up afterwards.

### Object storage

SeaweedFS serves S3 from the service `seaweedfs-s3` in `olf-system`. Run
`olf infra forward` in one terminal; it publishes S3 on
`http://localhost:9000`. The credentials are in the Secret `seaweedfs-s3-creds`
and the bucket names in `manifest.json`. In another terminal:

```bash
export AWS_ACCESS_KEY_ID="$("$KUBECTL" get secret seaweedfs-s3-creds -n olf-system -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' | base64 -d)"
export AWS_SECRET_ACCESS_KEY="$("$KUBECTL" get secret seaweedfs-s3-creds -n olf-system -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' | base64 -d)"
export AWS_DEFAULT_REGION=us-east-1
aws --endpoint-url http://localhost:9000 s3 sync "s3://<bucket>" "./<bucket>"
```

Swap the two `sync` arguments to copy back. Any S3 client works; `olf` uses
path-style addressing.
