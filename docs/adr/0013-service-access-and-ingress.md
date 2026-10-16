# ADR 0013: Service access — one ingress, configured by domain and issuer

## Status

Binding. The local root runs the ingress adapter and emits `access.ingress`
with routes for Dagster and Superset per stage, Trino, OpenMetadata when
governance is enabled, the portal landing page at the base domain, and `auth.<base_domain>` for the identity provider; the AWS and Azure roots still emit
`access.kubectl_port_forward`.

## Context

Users reach Dagster, Superset, OpenMetadata, and Trino through
`kubectl port-forward` to fixed localhost ports. That needs a cluster
credential on every workstation, breaks on pod restart, and gives no stable
URL or trusted TLS. v0.4 (#20) replaces it for local and on-premises installs.

## Decision

1. **One ingress everywhere.** A local evaluation install and a real install
   run the same ingress and differ only in configuration. The Deployment
   Profile carries that configuration as `spec.access`:

   | Field | Default | Real install |
   | --- | --- | --- |
   | `base_domain` | `olf.localhost` | e.g. `olf.example.com` |
   | `issuer` | `local-ca` | an ACME (Let's Encrypt) or organisational CA issuer |

   The defaults need neither a purchased domain nor a public server.

   **The block is the switch (#399).** A profile with no `spec.access` is a
   quickstart: no Traefik, cert-manager, local CA, CoreDNS rewrite, portal or
   route Ingresses, and `shared.access` is a port-forward contract
   (`access.port_forward`, no routes), which `olf` clients and `olf forward`
   serve over `http://localhost:<port>`. Declaring the block, even as
   `access: {}` for the defaults, turns the ingress on. Block presence was
   chosen over an `access.exposure` field because a second field would let the
   block and the field disagree (`access:` present with `exposure: none`), and
   the defaults above already make an empty block meaningful.
   `spec.identity` requires `spec.access`: sign-in redirects need the route
   hostnames, so the profile is rejected at validation otherwise.
2. **Routes sit one level under the base domain:** a stage service is
   `https://<service>-<stage>.<base_domain>` (`dagster-dev.olf.localhost`);
   a shared service (OpenMetadata, Trino) drops the stage suffix. One
   wildcard level is all that most client certificates and edge providers
   cover (#340), so a single `*.<base_domain>` certificate serves every
   route. Stage names are the fixed set `dev`, `uat`, `prod` and the stage
   services are `dagster` and `superset`, so no `<service>-<stage>` host can
   equal a shared or reserved host (`trino`, `openmetadata`, `auth`,
   `oauth2`). The one exception is the portal (decision 7), which is the apex,
   `https://<base_domain>`.
3. **The provider contract carries the routes.** `shared.access` gains
   `base_domain`, `issuer`, `tls_mode`, and `routes` — declared together or
   not at all, so a port-forward contract still parses. `routes` is keyed by
   the service's existing contract ref (`stage/dev/orchestration`,
   `shared/governance_service`, `shared/portal`, ...), so a route for a disabled capability
   or an unknown service does not resolve and is rejected. Consumers read
   URLs from the contract, never from fixed ports.
4. **Internal endpoints cannot be user-facing.** Only orchestration,
   reporting, governance, query, identity, and portal may be routed with
   `exposure: user-facing`. Databases, object-store administration, the
   catalog service, and the registry may only be `internal`; Dagster code
   servers have no ref and cannot be routed at all.
5. **Port-forward is an explicit development fallback**, not an access path.
   `olf`'s own clients (`olf.access.service_url`) use a stage's route when
   its contract has one, and port-forward when it has none (the AWS and
   Azure contracts today), when `OLF_PORT_FORWARD` is set, or when the route
   is in `shared.access.perimeter.protected_routes` of an enabled perimeter
   (ADR 0014 seam 3), because the ingress answers a request with no session
   by redirecting to login. Only a caller that tests the ingress itself passes
   `through_ingress=True`. They resolve
   `*.localhost` to `127.0.0.1` in-process and, for the `local-ca` issuer,
   verify against the CA read from the cluster — no `/etc/hosts` edit and no
   disabled verification. Users trust that CA once with `olf access trust`.
6. **Local adapter: Traefik terminates TLS, cert-manager issues.** On kind,
   Traefik binds host ports 80/443 on `127.0.0.1` through the control-plane
   node. cert-manager signs from a `local-ca` ClusterIssuer whose root is
   self-signed once and never rotated by a re-deploy; `olf deploy` waits for
   a probe certificate from it. `tls_mode` is therefore `ingress-terminated`,
   the only value the contract accepts. One wildcard
   certificate, `*.<base_domain>` plus the bare `<base_domain>` (a wildcard
   does not match the apex), is issued in the shared namespace by
   cert-manager's ingress-shim and is Traefik's default certificate
   (`tlsStore.default`). Traefik's TLS store is global and keyed by SNI, so
   per-namespace copies of the same wildcard would leave one of them serving
   every route; stage `Ingress` objects carry an empty `tls` block, which
   enables TLS on the router and names no Secret. Routes are standard `Ingress` objects, which need no
   CRD at plan time. Traefik attaches middleware per `Ingress`, so a
   namespace has one `Ingress` per access group (the role floor, ADR 0014
   seam 3); only one in the shared namespace carries the issuer annotation
   and names the TLS Secret. The `Ingress` without a group holds the routes
   behind no middleware: the identity route and the perimeter's login
   callback.
7. **The portal is a static page at the base domain.** It lists every route
   of `shared.access.routes` that is enabled and `user-facing`, grouped by
   stage. Terraform renders the HTML from the same route data that feeds
   the contract and stores it in a ConfigMap, so the page cannot list a
   service the contract does not route. A pinned BusyBox `httpd` serves it
   from the `olf-system` namespace in the platform phase: the route set is
   static infrastructure (ADR 0002). Links are not filtered by role;
   the perimeter (#176) denies what a role cannot open and sends an
   unauthenticated request to login, and the portal sits behind it like
   every other route.

8. **Pods reach the same URLs.** A service that validates a token calls the
   issuer's public URL from inside the cluster, and `*.localhost` must not
   resolve to the pod's own loopback there. The local root adds a CoreDNS
   `rewrite` sending `<base_domain>` and every name under it to the Traefik
   Service, and publishes the local CA root as ConfigMap `olf-local-ca`
   (key `ca.crt`) in `olf-system` and every stage namespace for pods to
   mount. The URL, certificate and token `iss` are then the same as in a
   browser. The rewrite replaces the cluster's `Corefile` key, so it is
   removed with the cluster.

The shape is provider-neutral. The AWS mapping (#274) is out of v0.4.

## Consequences

- `olf` rejects a contract whose `base_domain` or `issuer` differs from the
  resolved profile, so a contract and profile cannot silently diverge.
- Changing `base_domain` changes every user-facing URL; it is deployment
  configuration, not product intent, and stays out of `lakehouse.yaml`.

- The ingress and portal add five pods to the local footprint, all in `olf-system`:

  | Pod | CPU request | Memory request / limit |
  | --- | --- | --- |
  | Traefik | 50m | 64Mi / 256Mi |
  | cert-manager controller | 10m | 64Mi / 256Mi |
  | cert-manager cainjector | 10m | 64Mi / 256Mi |
  | cert-manager webhook | 10m | 32Mi / 128Mi |
  | Portal (BusyBox httpd) | 5m | 8Mi / 32Mi |

- Mapping 80/443 changed the kind cluster shape: an existing local cluster
  must be recreated (`olf destroy` then `olf deploy`), and those host ports
  must be free; `olf deploy` checks both ports and the issuer before it
  creates the cluster.

## History

New record (#264). No prior ADR covered service access. #265 added the local
Traefik/cert-manager adapter (decision 6) and fixed `tls_mode`'s values.
#266 added the local routes and wildcard certificates. #267 moved `olf`'s
clients onto the routes, added `olf access trust`, and lists the URLs in
`olf status`. #268 added the deploy preflight, the not-Ready Certificate
section in `olf status`, and the e2e Traefik restart and renewal drills.
#24 added the `shared/identity` route (`auth.<base_domain>`, Keycloak) to the
local routes; it is never behind the perimeter (ADR 0014). Decision 8 (in-cluster
resolution of the routes and the CA ConfigMap) was added with it.
#330 added the portal (decisions 2, 3, 4, 6 and 7): the apex route, its
certificate name, and the landing page.
#176 put every route except the identity route behind the perimeter (ADR 0014
seam 3): decision 6 now renders one `Ingress` per namespace and access group,
and `olf`'s clients port-forward to a protected route instead of taking its
ingress URL (decision 5). The login callback host `oauth2.<base_domain>` is
answered by the proxy and is not a contract route.
#389 flattened stage hostnames to `<service>-<stage>.<base_domain>` (decisions 2 and
6): every namespace's certificate is now `*.<base_domain>`.
The flat-hostname change also replaced the per-namespace certificates with one shared
wildcard served as Traefik's default certificate (decision 6).
