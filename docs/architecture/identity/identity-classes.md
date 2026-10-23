# Identity classes

Every caller on the platform belongs to exactly one of three identity classes,
and the class names the mechanism that governs it. This page is the contract
documentation for those classes (#178); the path-by-path inventory is in
[identity-callers.md](identity-callers.md).

| Class | Who | Governed by | Never |
| --- | --- | --- | --- |
| 1. Human | A person reaching a service UI through the issuer | Canonical roles from the GitHub team mapping, enforced at the perimeter ([ADR 0014](../../adr/0014-identity-and-authorization.md), [ADR 0018](../../adr/0018-github-is-the-forge.md)) | Used by an automated path |
| 2. In-cluster workload and automation | A pod calling another component, or `olf` and CI calling the cluster | A service credential from a Kubernetes Secret, or Kubernetes RBAC | A human account; routed through the perimeter; a member of a canonical role |
| 3. Cloud workload | A pod calling a cloud API (object storage, catalog, secrets) | Cloud IAM through EKS Pod Identity or Azure workload identity | A canonical role |

## The classification rule

A caller that reaches a service **through the issuer** is class 1. A caller
that authenticates with a service credential or with Kubernetes RBAC is class
2, whether it runs in a pod or on a workstation, and even when a person
started it. `olf` and CI are therefore class 2, with Kubernetes RBAC as their
identity.

The consequence is that the four canonical roles apply to class 1 only. A
role never grants a pod anything, and a service credential never signs a person
in.

## Class 1: human

| Item | Detail |
| --- | --- |
| Identity | A GitHub account, brokered by Dex into an OIDC token |
| Authorization | The highest canonical role among the person's mapped teams ([Roles and permissions](../../reference/roles-and-permissions.md)) |
| Enforcement | The perimeter (oauth2-proxy behind Traefik forwardAuth) refuses a route the role does not hold; Superset and OpenMetadata also map the role to their native roles |
| Lifetime | Perimeter cookie 30 min idle, 10 h absolute; access token 5 min; a revocation reaches the perimeter within about 5 min ([identity-sessions-and-recovery.md](identity-sessions-and-recovery.md)) |
| Operations | [Add, change or remove a member](../../guides/identity/manage-members.md) |

## Class 2: in-cluster workload and automation

| Item | Detail |
| --- | --- |
| Identity | A ServiceAccount or kubeconfig user, or a service credential created in the cluster |
| Examples | `olf deploy`, `olf e2e` and CI (RBAC, port-forward or in-cluster exec); Superset, dbt and OpenMetadata ingestion to Trino; Trino and Floe to Polaris; pods to PostgreSQL and object storage |
| Authorization | Kubernetes RBAC for operators and CI. For pods, a per-service credential from a Secret that a bootstrap Job generates once and reuses |
| Network | Reachability between pods is decided by default-deny NetworkPolicy ([identity-network-policy-and-rbac.md](identity-network-policy-and-rbac.md)) |
| Trino | Callers authenticate to Trino with a password per principal over HTTPS when `trino_workload_authentication` is `password` ([ADR 0016](../../adr/0016-trino-workload-authentication.md)) |
| Rotation | `olf secrets rotate` for the Dex clients ([Rotate secrets](../../guides/identity/rotate-secrets.md)); other credentials as the inventory states ([identity-credentials.md](identity-credentials.md)) |

Workstation and CI automation reaches services by an RBAC-authorized
`kubectl port-forward` or by in-cluster execution, never through the public
hostnames and never as a human or administrator account. Who may automate is
who holds `pods/portforward` and the Secret reads in RBAC.

Known limitation: a service credential is shared by every user of the
component it fronts. Every Superset query reaches Trino as the stage's runtime
principal, whatever the person's role. Per-user data access is deferred in
[ADR 0019](../../adr/0019-data-plane-authorization-deferral.md).

## Class 3: cloud workload

| Item | Detail |
| --- | --- |
| Identity | The cloud's workload identity of a pod |
| Mechanism | `identity.aws_pod_identity` on AWS, `identity.azure_workload_identity_ready` on Azure, both fields of the provider contract |
| Authorization | Cloud IAM policy on that identity |
| Status | Contract only. Class 3 is inventoried so the boundary is clear; exercising cloud credentials is outside the v0.5 scope |

OpenLakeForge roles are never cloud IAM permissions, and a cloud identity never
holds a canonical role.

## Break-glass

When the issuer is down, a cluster administrator is a class 2 caller: their
kubeconfig, not the issuer, is the authority. `olf break-glass` uses that to
reach a service for a bounded time and records the use
([Break-glass access](../../guides/identity/break-glass.md)). It does not turn
the perimeter off.
