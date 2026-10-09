# Architecture Documentation

This directory contains the repo-local architecture source of truth for OpenLakeForge.

- `diagrams/` contains five technical charts drawn with the official Kubernetes
  iconography — the cluster pod census, namespace runtime topology, the ephemeral
  job lifecycle, the medallion/catalog data path, and the Terraform contract flow —
  plus reference tables for portability, deploy phases, identity, and observability.
  Start at `diagrams/README.md`.
- `overview.md` describes the initial v1 platform shape and ownership boundaries.
- `floe-validation.md` explains Floe's Bronze-to-Silver validation, reject
  handling, materialization, and Kubernetes runner boundary.
- `project-revisions.md` explains the build-once, content-addressed
  `ProjectRevision` promoted unchanged across stages.
- `azure-aks-poc.md` describes the first Azure deployment target and test flow.
- `aws-eks-poc.md` describes the AWS EKS managed-services POC and compatibility
  gate.
- `local-stack-contracts.md` describes the Terraform-managed local service interfaces.
- `provider-contracts.md` describes the provider-neutral contract boundary that
  keeps the local implementation cloud-ready.
- `identity-credentials.md`, `identity-sessions-and-recovery.md` and
  `identity-callers.md` are the identity policy gates (ADR 0014): credential
  inventory, session policy with administrator recovery, and the caller
  inventory.
- `../technical-debt.md` tracks known weaknesses, mitigations, and fix paths.
- `../testing/floe-openlineage-capture-test-plan.md` describes the capture-based
  validation path for Floe OpenLineage events.
- Architecture decision records live in `../adr/`.
