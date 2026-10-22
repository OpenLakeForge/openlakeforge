# Architecture: maintainer documentation

Implementation detail for people changing OpenLakeForge. These pages are kept
in the repository and are not published on openlakeforge.dev; the user-facing
summary is [overview.md](overview.md).

| Page | Covers |
| --- | --- |
| [diagrams/README.md](diagrams/README.md) | Pod census, namespace topology, job lifecycle, medallion data path and provider-contract charts, with reference tables |
| [provider-contracts.md](provider-contracts.md) | The provider-neutral contract boundary, its v3 shape and validation |
| [local-stack-contracts.md](local-stack-contracts.md) | How the local Terraform root implements each contract |
| [aws-eks-poc.md](aws-eks-poc.md) | The AWS Terraform roots, contract implementations and compatibility gate |
| [azure-aks-poc.md](azure-aks-poc.md) | The Azure Terraform roots, image flow and e2e checks |
| [project-revisions.md](project-revisions.md) | The content-addressed `ProjectRevision`, stage activation and the report bundle contract |
| [floe-validation.md](floe-validation.md) | Floe's contract, runner boundary and alternatives at the Bronze-to-Silver step |
| [identity-credentials.md](identity-credentials.md), [identity-sessions-and-recovery.md](identity-sessions-and-recovery.md), [identity-callers.md](identity-callers.md) | Identity policy gates (ADR 0014): credential inventory, session policy, caller inventory |
| [identity-service-permissions.md](identity-service-permissions.md) | What each canonical role may do in Superset and OpenMetadata |

Related maintainer material:

- [../adr/README.md](../adr/README.md): the decision log.
- [../technical-debt.md](../technical-debt.md): known weaknesses and their fix
  paths.
- [../release/releasing.md](../release/releasing.md) and
  [../reference/component-catalog.md](../reference/component-catalog.md): the
  release pipeline and its pinned inputs.
- [../testing/floe-openlineage-capture-test-plan.md](../testing/floe-openlineage-capture-test-plan.md):
  the Floe OpenLineage capture test plan.
