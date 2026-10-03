# ADR 0013: Service access — one ingress, configured by domain and issuer

## Status

Binding for the contract. The ingress adapter is not built yet (#265); every
root still emits the `access.kubectl_port_forward` contract.

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

The shape is provider-neutral. The AWS mapping (#274) is out of v0.4.

## Consequences

- `olf` rejects a contract whose `base_domain` or `issuer` differs from the
  resolved profile, so a contract and profile cannot silently diverge.
- Changing `base_domain` changes every user-facing URL; it is deployment
  configuration, not product intent, and stays out of `lakehouse.yaml`.

## History

New record (#264). No prior ADR covered service access.
