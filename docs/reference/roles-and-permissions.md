# Roles and permissions

The four OpenLakeForge roles and what each can do in every service. The roles
are fixed for v0.5-alpha and defined in `release/identity-roles.yaml`; this
page is derived from that file.

A person holds the highest role among the GitHub teams they belong to, and
GitHub teams are mapped to roles in `spec.identity`
([Map teams to roles](../guides/identity/map-teams-to-roles.md)). With
authentication off, everyone is an administrator everywhere.

## Roles

Highest first. A role holding a service's grant implies that every role above
it holds one too.

| Role | Intended for |
| --- | --- |
| `platform-admin` | Running the platform: administrator in every service |
| `data-engineer` | Building and running pipelines, models and metadata |
| `analyst` | Reading and building dashboards on published data |
| `viewer` | Reading dashboards and metadata |

## What each role opens

A role with no entry for a service is denied at the perimeter, before the
request reaches the service. The cell is the role the person gets inside the
service. A grant applies on every enabled stage.

| Service | `platform-admin` | `data-engineer` | `analyst` | `viewer` |
| --- | --- | --- | --- | --- |
| Portal | opens | opens | opens | opens |
| Dagster | full | full | denied | denied |
| Trino UI | opens | opens | denied | denied |
| Superset | `Admin` | `Alpha` | `Gamma` | read-only |
| OpenMetadata | `Admin` | `write` | `read` | `read` |

A person refused a service sees an "Access is role-restricted" page.

## Per service

### Portal

Every signed-in person with a role opens it. It lists the services and, with
GitHub sign-in, links to the organization's teams page where membership is
managed.

### Dagster

Dagster has no authorization of its own, so the perimeter is the only control.
`platform-admin` and `data-engineer` have full capability: launching jobs with
run configuration, terminating and deleting runs, reloading code locations and
toggling schedules. The pods that run those jobs hold the stage's storage and
catalog credentials, so assign `data-engineer` on that basis.

### Trino UI

`platform-admin` and `data-engineer` open the Trino web UI. `analyst` and
`viewer` do not. The roles do not change what a query may read: Trino's
catalog access is decided by the service principals, not by the person (see
[CLI and automation](#cli-and-automation)).

### Superset

| Action | `platform-admin` (`Admin`) | `data-engineer` (`Alpha`) | `analyst` (`Gamma`) | `viewer` (read-only) |
| --- | --- | --- | --- | --- |
| View published dashboards | all | all | granted schemas | granted schemas |
| Create and edit own charts and dashboards | yes | yes | yes, on granted datasets | no |
| Create and edit datasets | yes | yes | no | no |
| SQL Lab | yes | no | no | no |
| Add, edit, delete database connections | yes | no | no | no |
| Import and export dashboards, alerts and reports | yes | yes | no | no |
| User and role administration | yes | no | no | no |

`analyst` and `viewer` see the Gold schemas of the imported dashboards. SQL Lab
is for administrators only. The `viewer` role is a custom read-only role
(`olf_viewer`), because Superset has no read-only dashboard role of its own.

> [!NOTE]
> These rows are the design in the permission specification and the pinned
> Superset's role definitions. Schema limits shape what Superset offers; every
> Superset query reaches Trino as the stage's service principal, so they do not
> restrict what a connection could read.

### OpenMetadata

| Action | `platform-admin` | `data-engineer` | `analyst` | `viewer` |
| --- | --- | --- | --- | --- |
| Search, browse, view assets and lineage | yes | yes | yes | yes |
| Edit descriptions, tags, glossary terms, tier, certification | yes | yes | no | no |
| Edit display names and lineage; trigger ingestion pipelines | yes | yes | no | no |
| Change owners | yes | yes | no | no |
| Create or delete entities | yes | no | no | no |
| Roles, policies, teams, users, bots, settings | yes | no | no | no |

A CronJob, `openmetadata-role-sync`, applies these roles every 5 minutes from
the mapped GitHub teams: the administrator flag for `platform-admin`, the
`DataSteward` role for `data-engineer`, and neither for the others. A person in
no mapped team cannot sign in. An asset's owner keeps full control of it
whatever their role, so reassign owned assets when you demote someone.

OpenMetadata's local `admin@open-metadata.org` login does not work once sign-in
is on.

### CLI and automation

`olf`, CI and the pods of the platform are not people and hold no role. They
authenticate with Kubernetes RBAC (your kubeconfig) or with a service
credential from a Secret, and reach services through port-forwards or their
in-cluster addresses, never through the sign-in perimeter. Whoever holds the
kubeconfig can therefore reach every service; a role limits browsers, not
cluster access. `olf break-glass` is the recorded way to use that when the
issuer is down ([Break-glass access](../guides/identity/break-glass.md)).

OpenLakeForge roles are never cloud IAM permissions.
