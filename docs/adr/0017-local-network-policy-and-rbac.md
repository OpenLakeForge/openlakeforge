# ADR 0017: Local NetworkPolicy and RBAC — default deny, one flow file, named exceptions

| | |
| --- | --- |
| Status | Binding |
| Built | local ✔ (#46) · AWS ✘ · Azure ✘ (neither root renders a NetworkPolicy) |

## Context

Without policy every pod reaches every Service: PostgreSQL (per-service
passwords only), Polaris, SeaweedFS, Dex's whole HTTP surface, Dagster's
unauthenticated GraphQL API, and Trino, which has no authentication of its own
([ADR 0016](0016-trino-workload-authentication.md)). The perimeter
([ADR 0014](0014-identity-and-authorization.md)) guards ingress only, so an
untrusted pod bypasses it. Cluster administrators stay trusted;
`kubectl port-forward`, `exec` and `logs` are RBAC paths, not network paths.
Traefik and cert-manager share `olf-system` with the data plane, and a stage
namespace (`olf-<stage>`) holds pods whose labels user code controls. kind's
default CNI, kindnet, implements NetworkPolicy through kube-network-policies,
a reference implementation, so enforcement has to be proven on the pinned
cluster rather than assumed.

## Decision

**D1. Dataplane gate.** `olf infra deploy` fails before any policy is applied
unless the cluster enforces policy: the canary runs in the foundation phase of
every `olf infra deploy`, and the conformance e2e checks the flows themselves.
Policy objects without enforcement never count as done. The canary
connects a client and server pair with no policy, then requires an
ingress-only and an egress-only deny each to block it, and an allow rule to
admit it again. Enforced by: `tools/olf/olf/infra/deployment/local/network_canary.py`
(called from `foundation_up` in `tools/olf/olf/infra/deployment/local/provider.py`).

**D2. Default deny, one flow file.** Ingress and egress are denied in every
stage namespace and for each `olf-system` workload listed under `workloads` in
`release/network-flows.yaml`; the rest of `olf-system` stays unisolated. DNS
is allowed to every selected pod. Every other flow is a row of that file, read
by Terraform and by `olf infra e2e run`; where both ends are selected, a flow
needs the row on both sides. Rows that depend on the profile (an
`issuer: external` host, a stage's source egress ports) are rendered by the
local root from the profile. A flow id is never renumbered.
Enforced by: `infra/terraform/modules/security/network-policy`,
`olf check flows`.

**D3. A stage namespace is one trust domain; `olf-system` is not.** Inside a
stage namespace pods talk freely: run pods and Floe Jobs are launched by the
`dagster` ServiceAccount with labels user code controls, so a label is not a
boundary there. Between namespaces, and between `olf-system` workloads, every
flow is an allow row selecting on chart-native labels. Enforced by:
`infra/terraform/modules/security/network-policy`.

**D4. Withdrawn (2026-11-23):** the stage-first rollout is complete and
nothing enforces it.

**D5. RBAC is inventoried, narrowed where the roles are ours, and pinned.**
The e2e lists every Role and ClusterRole bound to a ServiceAccount in the OLF
namespaces that grants a Secret verb, or `create` on pods, Jobs, CronJobs or
workload controllers (which reads Secrets by mounting them), and fails on any
this record does not name. The named exceptions are:
E1, the create-if-missing credential Jobs (`create` on Secrets or ConfigMaps
cannot be limited by `resourceNames`; every other verb is pinned by name);
E2, OpenMetadata's chart Role; E3, the Dagster run launcher of each stage;
E4, Traefik's cluster-wide read of TLS Secrets; E5, cert-manager. Enforced by:
`tools/olf/olf/infra/e2e/_network.py` (`E1_PINS` and the E2 to E5 tuples).

**E6. Withdrawn (2026-11-23):** it named Calico's components, which were never
installed.

**D6. `kube-system` and the control-plane host network are not policy
targets**, nor is a CNI's own namespace: a default-deny there can take the
cluster down (CoreDNS, kube-proxy, the CNI itself). Rows allow only what the
OLF pods need from them. Enforced by:
`infra/terraform/modules/security/network-policy` (policies only in the stage
namespaces and `olf-system`).

**D7. kindnet is the CNI.** The canary passed on the pinned kind 0.32.0, so
Calico is not built; a cluster whose CNI was replaced, or a host where kindnet
does not enforce, is not supported. Because kube-network-policies is a
reference implementation, the conformance e2e tests the semantics the flow
rows rely on. Enforced by:
`tools/olf/olf/infra/deployment/local/network_canary.py`.

**D8. The over-broad Roles are narrowed; E2 is accepted.** The Polaris and
OpenMetadata bootstrap Roles keep only a namespace-wide `create` (E1) and pin
every other verb by `resourceNames`, and the SeaweedFS chart's ClusterRole is
not created. OpenMetadata's chart Role stays, because its pod and Job creation
cannot be confined without breaking the UI pipeline client; it is recorded
with its exposure and pinned by the e2e. Enforced by:
`infra/terraform/modules/catalog/polaris/rbac.tf`,
`infra/terraform/modules/governance/openmetadata/rbac.tf`,
`infra/helm/values/local/seaweedfs.yaml`, `tools/olf/olf/infra/e2e/_network.py`.

**D9. The e2e's own stub pods are reached through flow rows keyed on a label
the e2e sets**, not through temporary policies that open a default-deny
workload: a policy removed in a `finally` would be the only thing between a
crashed run and a broken stack. The stubs live in `olf-system`, and creating
pods there is already a cluster-administrator act. Enforced by: rows F29 and
F41 of `release/network-flows.yaml`.

**D10. Stage-wide trust is accepted for local qualification.** Inside a stage
namespace there are no per-pod rules. A stage row that names pods narrows a
grant to them and is rendered as its own policy, but it does not isolate
them. Enforced by: `UNNARROWABLE` in
`tools/olf/olf/common/commands/checks/_flows.py`, the stage policies of
`infra/terraform/modules/security/network-policy`.

The flow rows, the `olf-system` workloads, the namespace selectors and the
RBAC inventory with each exception's exposure are reference:
[Network policy and RBAC](../architecture/identity/identity-network-policy-and-rbac.md).
Flow ids (F1, F2, …) are the `id` of a row of `release/network-flows.yaml`.

## Consequences

- Anything running in a stage namespace can reach everything that namespace
  may reach (D10).
- Any pod allowed to reach Traefik (F4) can request `auth.<base_domain>`,
  which routes the whole of Dex; the network cannot name a path. Narrowing the
  route is ADR 0014's follow-up.
- kindnet programs a new or restarted pod's policy asynchronously: a fresh pod
  briefly has unrestricted egress, and a restarted destination is briefly
  unreachable after it reports Ready. The e2e retries around it; the window is
  not measured.
- NetworkPolicy has no hostname selector, so external egress (Dex and the
  role sync to GitHub, the alert evaluator to receivers, a stage's source
  egress) is a port on any address outside the pod and Service ranges, on the
  named pods only.
- E2 is the widest grant: a compromised OpenMetadata can read the PostgreSQL
  admin and Polaris root Secrets, because creating a workload counts as
  reading every Secret in its namespace.
- `default` ServiceAccounts hold no Role; turning off their token automount is
  deferred hardening, not an RBAC grant.
- Rows the flow file tags `T` are unproven on a running cluster until a run
  exercises them.

## Boundaries

| Not decided here | Where |
| --- | --- |
| Ingress, routes and TLS | [ADR 0013](0013-service-access-and-ingress.md) |
| The perimeter, the issuer and roles | [ADR 0014](0014-identity-and-authorization.md) |
| Trino authentication | [ADR 0016](0016-trino-workload-authentication.md) |
| Per-person data access | [ADR 0019](0019-data-plane-authorization-deferral.md) |
| Resource requests, limits and PodDisruptionBudgets | #351 |
| Pod Security Standards and `securityContext` | #352 |
| AWS and Azure network enforcement | [ADR 0010](0010-cloud-provider-implementations.md) |
| Hostname egress filtering, mTLS between pods, a service mesh | Not covered |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| Calico as the local CNI | kindnet enforces on the pinned kind (D7); Calico would force every local cluster to be recreated and add memory, start-up time and a second upgrade surface |
| A default-deny policy on `kube-system` | Can take down CoreDNS, kube-proxy or the CNI (D6) |
| Per-pod rules inside a stage namespace | User code sets the labels, so a rule would not be a boundary (D3, D10) |
| Temporary policies that open a workload for the e2e's stubs | A crashed run would leave the stack broken (D9) |
| Narrowing OpenMetadata's chart Role by `resourceNames` | Its pod and Job creation still reads any Secret in `olf-system`, and confining it breaks the UI pipeline client (D8) |

## History

- 2026-10-16 (#46, #386): New record. D1 to D6, and the maintainer's
  decisions now numbered D7 to D10; D7 made Calico a fallback behind the
  canary.
- 2026-10-16 (#46): D7 settled: the canary passed on kind 0.32.0 with kindnet,
  so Calico is not built.
- 2026-10-18 (#408): D9 changed: the e2e's stub is reached by a flow row keyed
  on a label it sets.
- 2026-10-22 (#463): D8 changed: E2 accepted as a named exception instead of
  narrowed; E1 to E6 are the named set.
- 2026-11-23 (#1393): Rewritten in the standard form; the second list of decisions
  (1 to 4) became D7 to D10; the flow table and RBAC inventory moved to
  `identity-network-policy-and-rbac.md`. No decision changed.
- 2026-11-23 (#1393): D1 corrected to match the code: the canary runs on every
  `olf infra deploy`, not on every e2e run; the conformance e2e checks the
  flows themselves.
- 2026-11-23 (#1393): D4 and E6 withdrawn; D4's rollout is complete and
  nothing enforces it, and E6 named Calico components that were never
  installed. Nothing was renumbered.
