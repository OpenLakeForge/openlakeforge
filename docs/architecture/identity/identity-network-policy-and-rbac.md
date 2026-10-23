# Network policy and RBAC

How the local stack limits which pods can reach which services and what each
ServiceAccount may do in the Kubernetes API. This is the control that keeps
class 2 paths from being used by anyone else (#46, #178). The design and every
flow are in [ADR 0017](../../adr/0017-local-network-policy-and-rbac.md); the
flows as data are in `release/network-flows.yaml`.

Scope: the local provider. AWS and Azure enforcement is outside it.

## The model

| Rule | Detail |
| --- | --- |
| Default deny | Ingress and egress are denied in `olf-system` and in every stage namespace (`olf-<stage>`). DNS is allowed. |
| One flow, one row | Every other flow is a row of `release/network-flows.yaml`, read by the policy module (`modules/security/network-policy`) and by `olf e2e`. `olf check flows` validates the file. A flow that is not listed is denied. |
| Both sides | A pod reaches a destination only through a row on both sides: egress on the source, ingress on the destination. |
| Stage namespace = one trust domain | Inside a stage namespace pods talk freely, because run pods and Floe Jobs are created with labels that user code controls. Between namespaces, and between `olf-system` workloads, every flow is an allow row. |
| Exceptions | `kube-system` is not a policy target: a default-deny there could take down DNS, kube-proxy or the CNI. |

Examples of what the rows deny: one stage reaching another stage's Dagster or
Superset, a stage namespace reaching Dex, PostgreSQL from outside its listed
sources, and any pod reaching Dagster except Traefik and the stage's own
namespace.

## Enforcement is checked, not assumed

`olf deploy --provider local` runs a canary after the foundation. It proves a
client and server pair connect with no policy, then requires an ingress-only
and an egress-only deny to block it. The deploy fails if the cluster does not
enforce NetworkPolicy, and `olf e2e` runs the canary again as its first step.
The local kind cluster's default CNI (kindnet) passed on the pinned kind
version, so no other CNI is installed.

## Dex and GitHub egress

Dex calls `github.com` and `api.github.com` at every sign-in and refresh, and
the role-sync CronJob calls the GitHub API every 5 minutes. NetworkPolicy has
no hostname selector, so the rules allow port 443 to any address outside the
pod and Service ranges, on those pods only.

| Flow | From | To | Port |
| --- | --- | --- | --- |
| F28 | Dex pods | Any address outside the cluster's pod and Service ranges | 443 |
| F30 | The `openmetadata-role-sync` CronJob | Any address outside the cluster's pod and Service ranges | 443 |
| F29 | Dex pods | The stub GitHub pod of the identity e2e (label `openlakeforge.io/e2e=github-stub`) | 8443 |
| F23 | oauth2-proxy | An `issuer: external` host | 443 |

F29 exists only so `olf e2e` can sign in against a stub instead of real
GitHub; F28 excludes the pod and Service ranges, so the stub needs its own
row. F23 is rendered from the profile when `issuer: external` is set. The
cluster-side behaviour of F23, F28, F29 and F30 is marked as to-verify in the
flow file.

With `spec.identity.github.host` set to a GitHub Enterprise Server, Dex and the
role sync reach that host over the same port-443 rule.

## RBAC

The kind kubeconfig is cluster-admin, and the operator and CI are trusted.
Inside the cluster the Roles are narrowed where the platform owns them and the
rest are recorded as named exceptions:

| Exception | Subject | Why it stays |
| --- | --- | --- |
| E1 | Bootstrap Jobs (Dex credentials, perimeter cookie, local CA, Polaris, OpenMetadata) | `create` on Secrets or ConfigMaps cannot be limited by `resourceNames`; `get`, `update`, `patch` and `delete` are pinned to named objects |
| E2 | OpenMetadata server and ingestion (chart Role) | Pod, Job and Secret verbs in `olf-system`, which its UI pipeline client needs; the widest grant |
| E3 | Dagster run launcher, per stage | Creates run Jobs and pods; no Secret verbs, but a stage's user code already holds the stage's Secrets |
| E4 | Traefik | Reads TLS Secrets in all namespaces |
| E5 | cert-manager | Issues certificates, with Secret verbs cluster-wide |

The `default` ServiceAccount of every namespace has no Role. `olf e2e` asserts
the denials with `kubectl auth can-i` as each ServiceAccount, and lists every
Role bound in the OpenLakeForge namespaces that grants a Secret verb or pod
creation, failing on any not named above. The break-glass Role for a
non-admin operator grants no Secret read and no `exec`
([identity-sessions-and-recovery.md](identity-sessions-and-recovery.md)).

## Limits

- Anything running in a stage namespace can reach everything that namespace may
  reach.
- Any pod allowed to reach Traefik can request the Dex route, `auth.<base_domain>`,
  because the network cannot name a path. Dagster and run pods are not given
  that flow.
- `kubectl port-forward`, `exec` and `logs` are RBAC paths, not network paths,
  and policy does not apply to them. A cluster administrator stays trusted.
- Resource limits, Pod Security Standards, hostname-based egress filtering,
  service meshes and AWS or Azure enforcement are not covered.

Callers and credentials these rules protect:
[identity-callers.md](identity-callers.md),
[identity-classes.md](identity-classes.md).
