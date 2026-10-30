# Floe OpenLineage Capture Test Plan

This test plan verifies the configured Floe OpenLineage behavior after direct,
authenticated emission to OpenMetadata was enabled with Floe 0.6.10.

The test deliberately captures Floe events first, instead of sending them
directly to OpenMetadata. That separates event-shape validation from
OpenMetadata entity matching and makes regressions easier to diagnose.

## Goal

Prove that Floe emits valid Bronze-to-Silver OpenLineage events for the current
OpenLakeForge runtime:

- `run.runId` is UUID-shaped.
- `inputs` and `outputs` are present on completion events.
- S3 Bronze dataset names are bucket-relative and do not start with `/`.
- Iceberg Silver output datasets use the expected catalog namespace.
- OpenLineage job namespace and job name are separated correctly.
- OpenMetadata can later match the same event stream without the removed proxy.

This test does not validate dbt Silver-to-Gold lineage. dbt-trino is tested
separately in ADR 0001.

## Preconditions

| Requirement | Expected State |
| --- | --- |
| Local stack | `uv run --project tools/olf --locked olf infra deploy --provider local --phase foundation` and `uv run --project tools/olf --locked olf infra deploy --provider local` have completed. |
| Floe | The project-code image carries the pinned `floe-python`; Floe runs in one Kubernetes Job per domain on that image; the run pod forwards its `OPENLINEAGE_URL` and `OPENLINEAGE_ENDPOINT` to the Job, which renders its lineage endpoint from them. |
| OpenLineage proxy | Not deployed. The test uses a temporary capture endpoint. |
| dbt OpenLineage | Disabled. The test isolates Floe events only. |
| Target product | Start with one small path, preferably `order_revenue_pipeline`. |

## Capture Endpoint

Create a temporary in-cluster HTTP endpoint that records every POST body and
returns a 200/202 response. The endpoint must expose one route compatible with
Floe's OpenLineage client configuration, for example:

```text
http://openlineage-capture.olf-dev:5000/api/v1/lineage
```

The endpoint should persist captured JSON events long enough to copy them out of
the pod after the test. A minimal Python/Flask or Python `http.server` based
container is enough. It does not need authentication for the first capture pass.

## Test Flow

| Step | Action | Expected Result |
| --- | --- | --- |
| 1 | Deploy the capture service in the stage namespace (`olf-dev`). | `openlineage-capture` is reachable from Dagster run pods and Floe Jobs. |
| 2 | Point the code location at it: set `OPENLINEAGE_URL=http://openlineage-capture.olf-dev:5000` and `OPENLINEAGE_ENDPOINT=api/v1/lineage` on the user deployment. | The code location restarts; the profile it renders differs only by lineage settings. |
| 3 | Confirm the run pods inherit the change. | A run pod launched next carries the same two variables, and so does each Floe Job it creates. |
| 4 | Launch a narrow Dagster run, preferably `order_revenue_pipeline`. | Bronze and Silver assets succeed, and the capture endpoint receives OpenLineage events. |
| 5 | Copy captured event JSON from the capture pod. | The event payloads are available locally for validation. |
| 6 | Validate `run.runId` on every event with a UUID parser. | No event uses an old `mfv1-...` style run ID. |
| 7 | Validate COMPLETE events have non-empty `inputs` and `outputs`. | Bronze source datasets and Silver Iceberg output datasets are present. |
| 8 | Validate S3 input dataset names. | Names are bucket-relative paths such as `bronze/crm/...`, never `/bronze/crm/...`. |
| 9 | Validate job naming. | `job.namespace` carries the namespace and `job.name` is not prefixed by the Polaris REST URI. |
| 10 | Validate Iceberg output dataset naming. | Output datasets can be mapped to OpenMetadata's `polaris.<catalog>.<domain>_silver.<table>` entities. |
| 11 | Reapply the stage's active revision with `olf infra project deploy`. | The user deployment's lineage variables return to the stage contract's values. |

## Optional OpenMetadata Acceptance Pass

Only run this after the capture pass succeeds.

| Step | Action | Expected Result |
| --- | --- | --- |
| 1 | Point Floe lineage at OpenMetadata's native OpenLineage endpoint. | Floe can submit events without the deleted OpenLineage proxy. |
| 2 | Run the same narrow product path. | OpenMetadata accepts the events. |
| 3 | Inspect the lineage graph for the tested entity. | Bronze bucket container lineage connects to the Silver Iceberg table. |
| 4 | Keep dbt lineage disabled. | The test does not mix in the still-blocked Silver-to-Gold dbt lineage path. |

## Pass Criteria

The Floe debt can move from `Resolved Upstream, Pending Verification` to
resolved only when all of these are true:

- Each domain's Floe Job runs the run pod's project-code image.
- Captured events satisfy the event-shape checks above.
- OpenMetadata can accept the Floe events directly.
- OpenMetadata resolves at least one Bronze-to-Silver lineage edge without a
  normalizing proxy.

## Failure Triage

| Failure | Likely Cause | Next Step |
| --- | --- | --- |
| No events captured | Lineage block not rendered into the profile; the Floe Job pod cannot reach the capture service. | Inspect the Floe Job pod env and the capture service DNS. |
| Non-UUID run IDs | The domain's Floe Job is not running the expected project-code image, or there is a Floe regression. | Check the Floe Job pod's image digest against the run pod's, and the captured event payload. |
| Empty inputs/outputs | Floe emission regression or unsupported entity path. | Reproduce with one CSV-to-Iceberg entity and compare with Floe issue #382 expectations. |
| S3 names start with `/` | Floe S3 dataset-name fix is not active. | Confirm the `floe-python` pin in the project-code image and file upstream regression if needed. |
| OM accepts events but shows no edge | Dataset namespace/name no longer matches OM storage or Polaris entities. | Compare captured dataset names with OM container/table FQNs. |

## References

- [ADR 0007: OpenMetadata Lineage Integration Deferred](../adr/0007-governance-and-lineage.md)
- [Technical Debt: Upstream-Related Debt](../technical-debt.md#upstream-related-debt)
- [Floe issue #382](https://github.com/malon64/floe/issues/382)
- [Floe releases](https://github.com/malon64/floe/releases)
