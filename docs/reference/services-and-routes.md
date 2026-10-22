# Services and routes

The user-facing services of a local OpenLakeForge deployment, the URL each one
is published at, and which roles may open it. Use it to find a service's
address, or to see why a signed-in user was refused.

Routes exist only when the Deployment Profile declares `spec.access`. Without it
there is no ingress.
They are defined in `infra/terraform/environments/local/contracts.tf`, and the
role grants in `release/identity-roles.yaml`.

---

## URLs

`<base>` is `spec.access.base_domain`, `olf.localhost` by default. The scheme is
`http` when `spec.access.issuer` is `none` and `https` otherwise. `<stage>` is
`dev`, `uat` or `prod`.

| Service | Role in the platform | URL | Present when |
| --- | --- | --- | --- |
| Portal | Landing page that links every route below | `<scheme>://<base>` | `spec.access` is declared |
| Dagster | Pipelines and asset graph for one stage | `<scheme>://dagster-<stage>.<base>` | One per enabled stage |
| Superset | Dashboards for one stage | `<scheme>://superset-<stage>.<base>` | One per stage with `analytics` on |
| Trino | Query UI, shared by all stages | `<scheme>://trino.<base>` | `spec.access` is declared |
| OpenMetadata | Governance and lineage, shared by all stages | `<scheme>://openmetadata.<base>` | At least one enabled stage has `governance` on |
| Login (Dex) | GitHub sign-in | `<scheme>://auth.<base>` | `spec.identity.issuer` is `github` |

With the defaults, the DEV Dagster is `https://dagster-dev.olf.localhost`.
`*.localhost` names resolve to the loopback address in browsers, and the
ingress listens on host ports 80 and 443.

Storage admin, databases and code servers are never published. The sign-in
callback at `oauth2.<base>` belongs to the perimeter proxy and is not a page
to open.

---

## Who may open what

With `spec.identity`, every route except the login page sits behind the
perimeter, which admits a user only when their role holds a grant for the
service. The portal needs no grant: any recognised role may open it. The login
page needs no role.

A grant applies on every enabled stage. Roles are listed highest first.

| Service | `platform-admin` | `data-engineer` | `analyst` | `viewer` |
| --- | --- | --- | --- | --- |
| Portal | opens | opens | opens | opens |
| Dagster | full | full | denied | denied |
| Superset | `Admin` | `Alpha` | `Gamma` | read-only |
| OpenMetadata | `Admin` | `write` | `read` | `read` |
| Trino | opens | opens | denied | denied |

For Dagster and Trino the perimeter is the whole control: a role either opens
the service or it does not. For Superset and OpenMetadata the grant is also the
role the user receives inside the service.

A user whose role has no grant sees an "Access is role-restricted" page. Grants
are monotonic: a role that holds a service's grant implies every higher role
holds one too.

Without `spec.identity` there is no sign-in and no perimeter: every service
opens with administrator rights, which the profile allows only on `*.localhost`.

---

## Related

* [ADR 0013: service access](../adr/0013-service-access-and-ingress.md)
* [ADR 0014: identity and authorization](../adr/0014-identity-and-authorization.md)
* [Provider contracts](../architecture/provider-contracts.md)
