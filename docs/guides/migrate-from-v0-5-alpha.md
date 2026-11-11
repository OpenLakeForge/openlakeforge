# Migrate from v0.5 alpha to the v0.6 beta

Everything you change in a project, and in a running local deployment, to move
from a v0.5 alpha release (`0.5.0-alpha.1` to `0.5.1-alpha.3`) to the v0.6 beta
contract. Start with the command; the sections after it say what each change
means and what, if anything, cannot be automated.

The beta promises best-effort forward migration inside the beta line, for the
`local` provider only (see [Lifecycle stages](../release/releasing.md#lifecycle-stages)).
This page is the migration for the step into it. Every other beta release gets
its own section here.

## Rewrite the project files

From the project root, review and then apply:

```bash
olf project migrate --dry-run
olf project migrate
```

In one run the command rewrites:

| File | From | To | What changes |
| --- | --- | --- | --- |
| `openlakeforge.yaml` | `openlakeforge.io/v1alpha1` | `openlakeforge.io/v1beta1` | The version line, and `spec.access.issuer` becomes `tls` |
| `lakehouse_code/governance.yml` | `openlakeforge.io/v1beta1` | `openlakeforge.io/v1beta2` | Each product moves under its domain |
| `lakehouse_code/bronze/*/source.yaml` | `openlakeforge.io/v1alpha3` | `openlakeforge.io/v1beta1` | The version line only |

Comments and layout survive. If any one file cannot be migrated safely, the
command names it and changes none of them; fix that file and run it again. A
project already on the new versions prints `nothing to migrate`.

Until you migrate, `olf` refuses the old versions and names this command. Then
check the result:

```bash
olf profile validate --project .
olf project validate --project .
```

## The profile

### `apiVersion` and `spec.access.issuer`

The profile is `openlakeforge.io/v1beta1`. The only field that changed is the
TLS mode of `spec.access`, which v1alpha1 called `issuer`:

```yaml
# before
apiVersion: openlakeforge.io/v1alpha1
spec:
  access:
    issuer: local-ca
# after
apiVersion: openlakeforge.io/v1beta1
spec:
  access:
    tls: local-ca
```

v1beta1 accepts `tls: none | local-ca | external` and rejects `issuer` under
`spec.access`. `spec.identity.issuer` (`github` or `external`) is a different
field and is unchanged. The values are described in the
[Deployment Profile reference](../reference/deployment-profile.md#specaccess);
`external` is covered in [Run behind your own HTTPS](run-behind-your-own-https.md).

The local default also moved, in `0.5.1-alpha.3`: a profile that declares
`spec.access: {}`, or leaves the mode out, now serves plain HTTP on
`*.olf.localhost`. If you relied on HTTPS, write `tls: local-ca` and run
`olf infra access trust`. Re-register the GitHub App afterwards
(`olf infra secrets rotate github-app`), because its callback URL names the
scheme.

### Fields that are not in v1beta1

`spec.expose` (public gateway, DNS) does not exist and is rejected. A
`query.engine` field does not exist either; Trino is the query engine, and a
DuckDB engine is not part of this beta. Both fail validation as unknown fields.

### Query size per stage

`spec.query.size` is the default Trino size and `spec.stages.<stage>.query.size`
overrides it for one stage. On the local provider each enabled stage now runs
its own Trino coordinator at its own size, in the stage's namespace, instead of
one shared Trino in `olf-system`. Only `small` is accepted. A v0.5 profile with
no `query` block deploys as before, with one coordinator per stage. Plan for the
extra memory: a DEV+PROD deployment runs two coordinators.

## The Trino route and authentication

Trino is a stage service. Its host is `trino-<stage>.<base_domain>`
(`trino-dev.olf.localhost`), and the shared `trino.<base_domain>` host is gone.
Replace the host in any bookmark, script or Superset connection you wrote by
hand.

Trino also has no password and no HTTPS listener of its own any more: every
caller presents its own token and Polaris decides what it may read. Use
`olf query` for people. A client configured with a Trino username and password
stops working; give a tool a Polaris principal instead of a Trino login
([ADR 0019](../adr/0019-data-plane-authorization-deferral.md)). Superset
connects to Trino as the signed-in person, so re-import your reports after the
upgrade (`olf infra report import --stage <stage>`).

## Bucket names

Local and Azure buckets follow one rule, `<metadata.name>-<stage>-<layer>`, plus
`<metadata.name>-ops` for the ops bucket. They were `lakehouse-<stage>-<layer>`
and `openlakeforge-ops`. `metadata.name` is limited to 51 characters. AWS bucket
names are unchanged.

A rename is not a copy, and no step carries the data across. Nothing copies
objects to the new names and Polaris keeps pointing at the old ones, so
`olf infra deploy` refuses to rename deployed buckets and lists them. A backup
taken under the old names cannot be restored under the new ones either: restore
refuses a different release or profile
([Limits](backup-and-restore.md#limits)).

A deployment on the old names keeps working as long as you do not apply the
rename. When you choose to move, the supported path is
`olf infra destroy` followed by `olf infra deploy`, then running your pipelines
again so Bronze, Silver and Gold are rebuilt from the sources. Passing
`--allow-stage-removal` applies the rename over the existing platform with the
same result: empty buckets. If the tables cannot be rebuilt from their sources,
stay on the old names. Details are in
[Renaming buckets](backup-and-restore.md#renaming-buckets). The layout itself is
in [ADR 0004](../adr/0004-medallion-layout-and-catalog-namespaces.md).

## Descriptors

`source.yaml` is `openlakeforge.io/v1beta1` and no field changed.
`governance.yml` is `openlakeforge.io/v1beta2`: a data product is declared under
its domain in `domains[].products` and no longer names a `domain`, and a root
`products:` list is rejected. Derived names (`<domain>_silver`, `<product>_gold`,
jobs, buckets) are the same. `olf project migrate` does both rewrites; it
refuses an unknown domain, a duplicate product id and a layout it cannot move
safely, and tells you what to fix by hand. The field reference is
[Descriptors](../reference/descriptors.md).

## The people catalog

> [!NOTE]
> This section lands with #1052 and is not true of a build without it.

Each stage's Trino served the same catalog under two names: `lakehouse_<stage>`
and, for people, `<stage>`. #1052 folds the second into the first. Fully
qualified SQL that said `prod.order_revenue_gold.mart_order_revenue_by_day`
says `lakehouse_prod.order_revenue_gold.mart_order_revenue_by_day`. `olf query`
and the Superset databases already use the new name, so only SQL you wrote with
a literal `<stage>.` prefix changes. Reports imported before the change carry
the old catalog in their dataset permissions; the redeploy re-imports them.

## Python imports and packages

If you are coming from before `0.5.1-alpha.3`, the project code moved too:
`openlakeforge_domain` is `openlakeforge_core`, `libs` is `openlakeforge_runtime`
and a requirement on `openlakeforge-domain-model` becomes `openlakeforge-core`.
The exact replacements are in the Migration notes of the
[changelog](../../CHANGELOG.md).

## Apply to a running deployment

1. Run `olf project migrate`, then the two validate commands above.
2. If bucket names would change, `olf infra deploy` stops and lists them (see
   above); decide whether to rebuild the data before going on.
3. `olf infra deploy`. It rolls out a Trino coordinator per stage and builds
   and activates the project's revision again. Whether a revision is compatible
   is decided by runtime contract version, not by distribution version
   ([ADR 0012](../adr/0012-project-revisions-and-promotion.md)).
4. `olf infra e2e run --env local` to check the stack end to end.

## Related

* [Deployment Profile reference](../reference/deployment-profile.md)
* [Back up and restore](backup-and-restore.md)
* [Releasing](../release/releasing.md)
