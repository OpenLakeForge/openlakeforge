# ADR 0013: Service access — one ingress, configured by domain and TLS mode

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ · AWS ✘ (#274) · Azure ✘: both emit `access.kubectl_port_forward` and do not read `spec.access` |

## Context

Users once reached Dagster, Superset, OpenMetadata and Trino through
`kubectl port-forward` to fixed localhost ports. That needs a cluster
credential on every workstation, breaks on a pod restart, and gives no stable
URL and no trusted TLS. Local and on-premises installs need stable URLs that
work without a purchased domain, a public server or a certificate, and that
the same platform serves unchanged behind a customer's own HTTPS edge. Sign-in
(ADR 0014) adds two constraints: redirects need known hostnames, and pods
must reach the issuer whose public URL is the token's `iss`.

## Decision

**D1. One ingress everywhere, switched on by the profile's `spec.access`
block.** An evaluation install and a real install run the same ingress and
differ only in `spec.access`: `base_domain` (default `olf.localhost`), `tls`
and, with `tls: external`, `trusted_proxies`. `tls` is `none` (plain HTTP, D9),
`local-ca` (opt-in HTTPS from a CA the platform creates, which every browser
must trust) or `external` (the customer's proxy, tunnel or load balancer
terminates HTTPS in front of OpenLakeForge: Traefik serves HTTP while every
generated URL, callback and cookie is `https://`). `external` is refused on
`*.localhost`, where nothing upstream can terminate TLS. The AWS and Azure
default stays `local-ca`, because they do not read the block yet and `none` is
local-only. A profile without the block is a quickstart: no Traefik,
cert-manager, local CA, CoreDNS rewrite, portal or route Ingresses, and
`shared.access` is a port-forward contract (`access.port_forward`, no routes)
that `olf`'s clients and `olf infra forward` serve over
`http://localhost:<port>`. Declaring the block, even as `access: {}`, turns
the ingress on. On the local provider `spec.identity` requires `spec.access`,
because sign-in redirects need the route hostnames. Field reference:
[`spec.access`](../reference/deployment-profile.md#specaccess). Enforced by:
`tools/olf/olf/common/profile.py`.

**D2. A route sits one level under the base domain: `<service>-<stage>` for a
stage service, `<service>` for a shared one, and the portal at the apex.** The
stage services are `dagster`, `superset` and `trino` (one Trino per stage,
ADR 0019 section 9, refs `stage/<stage>/query` and `stage/<stage>/query_api`,
the `/v1` path); a shared service such as OpenMetadata drops the stage suffix;
the portal (D7) is `<base_domain>` itself. One wildcard level is what most
client certificates and edge providers cover, so one `*.<base_domain>`
certificate serves every route. Stage names are the fixed set `dev`, `uat`,
`prod`, so no `<service>-<stage>` host can equal a shared or reserved host
(`openmetadata`, `auth`, `oauth2`). Enforced by: the route checks in
`tools/olf/olf/common/provider_contracts/_v3.py` and the routes in
`infra/terraform/environments/local/contracts.tf`.

**D3. The provider contract carries the routes, and consumers read URLs only
from it.** `shared.access` gains `base_domain`, `tls`, `tls_mode` and
`routes`, declared together or not at all, so a port-forward contract still
parses. `tls_mode` follows `tls` one to one: `none` for `none`,
`ingress-terminated` for `local-ca`, `edge-terminated` for `external`.
`routes` is keyed by the service's existing contract ref
(`stage/dev/orchestration`, `shared/governance_service`, `shared/portal`, …),
so a route for a disabled capability or an unknown service does not resolve
and is rejected. `olf` rejects a contract whose `base_domain` or `tls` differs
from the resolved profile. Consumers never assume fixed ports. Field
reference: [`shared.access`](../architecture/provider-contracts.md#sharedaccess).
Enforced by: `tools/olf/olf/common/provider_contracts/_v3.py`.

**D4. Internal endpoints cannot be user-facing.** Only orchestration,
reporting, governance, query, identity and portal may be routed with
`exposure: user-facing`. Databases, object-store administration, the catalog
service and the registry may only be `internal`; Dagster code servers have no
ref and cannot be routed at all. Enforced by: `_USER_FACING_SERVICES` in
`tools/olf/olf/common/provider_contracts/_v3.py`.

**D5. Port-forward is an explicit development fallback, not an access path.**
`olf`'s own clients (`olf.infra.access.service_url`) use a service's route
when the contract has one, and port-forward when it has none (the AWS and
Azure contracts), when `OLF_PORT_FORWARD` is set, or when the route is in
`shared.access.perimeter.protected_routes` of an enabled perimeter (ADR 0014
D3), because the ingress answers a request with no session by redirecting
to login. Only a caller that tests the ingress itself passes
`through_ingress=True`. The clients resolve `*.localhost` to `127.0.0.1`
in-process and, with `local-ca`, verify against the CA read from the cluster:
no `/etc/hosts` edit and no disabled verification. Users trust that CA once
with `olf infra access trust`. Enforced by: `tools/olf/olf/infra/access.py`.

**D6. On the local provider Traefik serves the routes and cert-manager issues
their one certificate.** On kind, Traefik binds host ports 80 and 443 on
`127.0.0.1` through the control-plane node. cert-manager signs from a
`local-ca` ClusterIssuer whose root is self-signed once and never rotated by a
re-deploy; `olf infra deploy` waits for a probe certificate from it. With
`local-ca`, one certificate for `*.<base_domain>` plus the bare
`<base_domain>` (a wildcard does not match the apex) is issued in the shared
namespace and is Traefik's default certificate; stage `Ingress` objects carry
an empty `tls` block and name no Secret, because Traefik's TLS store is global
and keyed by SNI. With `external`, Traefik serves only `web`, with no TLS
store, certificate or host port 443, and believes `X-Forwarded-Proto` and
`-Host` only from `forwardedHeaders.trustedIPs`, set from
`spec.access.trusted_proxies` (required with `external`, refused otherwise,
never `0.0.0.0/0`); the perimeter's forward-auth middleware keeps those
headers (`trustForwardHeader`) only in this mode. Routes are standard
`Ingress` objects, which need no CRD at plan time. A namespace has one
`Ingress` per access group (the role floor, ADR 0014 D3), because Traefik
attaches middleware per `Ingress`; only one in the shared namespace carries
the issuer annotation and names the TLS Secret, and the `Ingress` without a
group holds the routes behind no middleware: the identity route and the
perimeter's login callback. Enforced by: `infra/terraform/modules/access/traefik`,
`infra/terraform/modules/access/cert-manager` and
`infra/helm/values/local/traefik.yaml`.

**D7. The portal is a page at the base domain listing every enabled
user-facing route.** Stage services sit under their stage and shared services
under a final "Shared" heading; the page omits itself and the identity route.
When the issuer is GitHub it adds an Admin link to
`https://<host>/orgs/<org>/teams` ("Manage your account and teams on
GitHub"), since Dex has no account page and GitHub owns accounts. Terraform
writes the links as `routes.json` in a ConfigMap from the same route data that
feeds the contract, so the page cannot list a service the contract does not
route. The pinned [openlakeforge-portal](https://github.com/OpenLakeForge/openlakeforge-portal)
image serves it, and the page Logout lands on, from `olf-system` in the
platform phase: the route set is static infrastructure (ADR 0002). Links are
not filtered by role; the portal sits behind the perimeter like every other
route, which denies what a role cannot open. Enforced by:
`infra/terraform/modules/access/portal`.

**D8. Pods reach the issuer at its Service, not at its public URL.** The
issuer's public URL is the token's `iss` and the browser's login page, and is
compared as a string. A service that validates a token fetches keys and
redeems codes at the `internal_url` of the `identity.oidc` contract (ADR
0014), Dex's Service (`http://dex.olf-system.svc.cluster.local:5556`), over
plain HTTP: the perimeter proxy, Polaris and the platform API do. An
`external` issuer has no `internal_url` and is reached at its public,
publicly trusted URL. OpenMetadata and Superset still call the public URL from
inside the cluster; for them the local root keeps a CoreDNS `rewrite` sending
`<base_domain>` and every name under it to the Traefik Service, and publishes
the local CA root as ConfigMap `olf-local-ca` (key `ca.crt`) in `olf-system`
and every stage namespace. The rewrite replaces the cluster's `Corefile` key,
so it is removed with the cluster. With `tls: external` the rewrite is not
applied: the public name belongs to the customer's edge, so those two callers
reach it through the customer's DNS and need a publicly trusted certificate.
Dex mounts `olf-local-ca` only to trust a GitHub Enterprise host, which is not
an issuer call. Enforced by: `infra/terraform/modules/access/in-cluster-resolution`.

**D9. Plain HTTP on `*.localhost` is the local default: `tls: none`.** The
local provider accepts `tls: none` only for a `base_domain` ending in
`.localhost`; any other provider or a public domain is rejected, and public
exposure (#340) must reject it too. Browsers resolve `*.localhost` to loopback
and treat it as a secure context, so the perimeter cookie and the OIDC
redirects work over HTTP, and Traefik's `127.0.0.1:80` mapping is the only
exposure. Routes and the contract use `http://`, `tls_mode` is `none`, and
Traefik serves `web` with no `tls` block and no host port 443. cert-manager
and the local CA stay deployed with `none` and with `external` (with no route
certificate), because the CA ConfigMap the pods mount is independent of the
routes. With `none` the CA is disposable: nothing outside the cluster trusts
it, so it is not backed up, and a restore or rebuild keeps the platform's own
CA. A profile that wants browser HTTPS says `tls: local-ca`; the conformance
profile does, so the nightly keeps exercising the TLS routes. Enforced by:
`tools/olf/olf/common/profile.py` and `tools/olf/olf/infra/backup/_plan.py`.

## Consequences

- Changing `base_domain` changes every user-facing URL. It is deployment
  configuration, not product intent, and stays out of `governance.yml`.
- The ingress, the portal and, with `spec.identity`, the platform API add pods
  to `olf-system`; their requests are in the
  [pod census](../architecture/diagrams.md#chart-1--cluster-pod-census).
- kind maps host ports 80 and 443 for every local profile, so both must be
  free; `olf infra deploy` checks them and the TLS mode before it creates the
  cluster. A cluster created before the mapping must be recreated.
- OpenMetadata 1.13.6 has no token-URL override, and Superset's move to
  `internal_url` waits for a republished image (#1092), so the CoreDNS rewrite
  and `olf-local-ca` stay until both move (D8).
- Whether cert-manager is still needed with `tls: none` is open (#391).
- AWS and Azure reach services by port-forward until their mapping (#274).

## Boundaries

| Not decided here | Where |
| --- | --- |
| Roles, the issuer and the perimeter in front of the routes | [ADR 0014](0014-identity-and-authorization.md) |
| Which pod may reach which port, including Traefik's flows | [ADR 0017](0017-local-network-policy-and-rbac.md); flow rows in `release/network-flows.yaml` |
| Trino's own authentication and its Web UI sign-in | [ADR 0016](0016-trino-workload-authentication.md) |
| The platform API served at `/api/` on the portal's host | [ADR 0021](0021-platform-api.md) |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| `kubectl port-forward` as the access path | Needs a cluster credential per workstation, breaks on pod restart, gives no stable URL or trusted TLS (D5 keeps it as a fallback) |
| An `access.exposure` field beside the block | Block and field could disagree (`access:` present with `exposure: none`); the defaults already make an empty block meaningful |
| Two-level hosts, `<service>.<stage>.<base_domain>` (#389) | Most client certificates and edge providers cover one wildcard level only |
| A wildcard certificate per namespace | Traefik's TLS store is global and keyed by SNI, so one copy would serve every route |
| A portal page served by BusyBox from Terraform | Replaced by the pinned openlakeforge-portal image |

## History

- 2026-10-03 (#264): New record: D1 to D5; no prior ADR covered service access.
- 2026-10-05 (#265 to #268): D6 added, fixing `tls_mode`'s values; D5's
  clients moved onto the routes, with `olf access trust` and the deploy preflight.
- 2026-10-14 (#24): D8 added with the `shared/identity` route
  (`auth.<base_domain>`, then Keycloak), never behind the perimeter.
- 2026-10-18 (#389): D2 and D6 changed: stage hosts flattened to
  `<service>-<stage>.<base_domain>`, one shared wildcard as Traefik's default certificate.
- 2026-10-18 (#399): D1 changed: the block is the switch; without it the
  contract is port-forward.
- 2026-10-18 (#403): D9 added: plain HTTP on `*.localhost` as the local default.
- 2026-10-21 (#384): D8 changed: `shared/identity` serves Dex; Keycloak removed.
- 2026-10-22 (#330): D7 added: the portal at the apex; D2 to D4 and D6 gain
  its route and certificate name.
- Undated: D7 changed: the Shared section split out, the identity link
  dropped, the openlakeforge-portal image with the GitHub teams Admin link.
- 2026-10-23 (#176): D5 and D6 changed: every route but identity behind the
  perimeter, one `Ingress` per namespace and access group, clients
  port-forward to protected routes.
- 2026-11-07 (#989): D1 changed: `spec.access.issuer` renamed `tls` and
  `external` added; contracts in state keep the `issuer` key.
- 2026-11-08 (#960, #976): D2 changed: Trino is a stage service,
  `trino-<stage>.<base_domain>`.
- 2026-11-09 (#983): D9 changed: Trino no longer has a certificate (ADR 0016).
- 2026-11-09 (#990): D1, D6 and D8 changed: the local adapter for `external`
  with `trusted_proxies`.
- 2026-11-11 (#991): D8 changed: the perimeter proxy, Polaris and the platform
  API reach Dex at `internal_url`; the rewrite stays for OpenMetadata and Superset.
- Undated: D9 changed: the local CA is backed up only with `local-ca`.
- 2026-11-22 (#1385): Status, D2 and D7 corrected: Trino is a stage service.
- 2026-11-23 (#1392): Rewritten in the standard form; no decision changed.
