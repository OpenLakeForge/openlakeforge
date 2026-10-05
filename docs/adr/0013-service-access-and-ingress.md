# ADR 0013: Service access — one ingress, configured by domain and issuer

## Status

Binding. The local root runs the ingress adapter and emits `access.ingress`
with routes for Dagster and Superset per stage, Trino, and OpenMetadata when
governance is enabled; the AWS and Azure roots still emit
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
2. **Routes are stage-derived:** `https://<service>.<stage>.<base_domain>`;
   a shared service (OpenMetadata, Trino) drops the stage label.
3. **The provider contract carries the routes.** `shared.access` gains
   `base_domain`, `issuer`, `tls_mode`, and `routes` — declared together or
   not at all, so a port-forward contract still parses. `routes` is keyed by
   the service's existing contract ref (`stage/dev/orchestration`,
   `shared/governance_service`, ...), so a route for a disabled capability
   or an unknown service does not resolve and is rejected. Consumers read
   URLs from the contract, never from fixed ports.
4. **Internal endpoints cannot be user-facing.** Only orchestration,
   reporting, governance, query, and identity may be routed with
   `exposure: user-facing`. Databases, object-store administration, the
   catalog service, and the registry may only be `internal`; Dagster code
   servers have no ref and cannot be routed at all.
5. **Port-forward is an explicit development fallback**, not an access path.
   `olf`'s own clients (`olf.access.service_url`) use a stage's route when
   its contract has one, and port-forward only when it has none (the AWS and
   Azure contracts today) or `OLF_PORT_FORWARD` is set. They resolve
   `*.localhost` to `127.0.0.1` in-process and, for the `local-ca` issuer,
   verify against the CA read from the cluster — no `/etc/hosts` edit and no
   disabled verification. Users trust that CA once with `olf access trust`.
6. **Local adapter: Traefik terminates TLS, cert-manager issues.** On kind,
   Traefik binds host ports 80/443 on `127.0.0.1` through the control-plane
   node. cert-manager signs from a `local-ca` ClusterIssuer whose root is
   self-signed once and never rotated by a re-deploy; `olf deploy` waits for
   a probe certificate from it. `tls_mode` is therefore `ingress-terminated`,
   the only value the contract accepts. Each namespace gets one standard
   `Ingress` holding its routes and one wildcard certificate
   (`*.<stage>.<base_domain>`, or `*.<base_domain>` for shared services),
   issued by cert-manager's ingress-shim. A standard `Ingress` needs no CRD
   at plan time, and #176 attaches auth middleware to it by annotation
   without changing hosts.

The shape is provider-neutral. The AWS mapping (#274) is out of v0.4.

## Consequences

- `olf` rejects a contract whose `base_domain` or `issuer` differs from the
  resolved profile, so a contract and profile cannot silently diverge.
- Changing `base_domain` changes every user-facing URL; it is deployment
  configuration, not product intent, and stays out of `lakehouse.yaml`.

- The ingress adds four pods to the local footprint, all in `olf-system`:

  | Pod | CPU request | Memory request / limit |
  | --- | --- | --- |
  | Traefik | 50m | 64Mi / 256Mi |
  | cert-manager controller | 10m | 64Mi / 256Mi |
  | cert-manager cainjector | 10m | 64Mi / 256Mi |
  | cert-manager webhook | 10m | 32Mi / 128Mi |

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
