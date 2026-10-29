# Benchmarks

`olf bench` measures what a local OpenLakeForge deployment costs and how its
data path behaves as volume grows (#328). It reports measurements, failures and
ceilings, not targets; #329 draws the conclusions.

## Scenarios

| Command | Scenario | Measures |
| --- | --- | --- |
| `olf bench footprint [--clean\|--existing]` | A: install and idle | Each `olf deploy` phase (skipped with `--existing`, which measures the running deployment), time to the first Trino query, the first product pipeline with its Gold rows, pods per namespace, node storage, and CPU/RAM installed versus after `--settle-seconds` and over `--idle-seconds` |
| `olf bench etl --scale N [--delta D]` | B: end-to-end ETL | The CSV fixtures replicated N times through every product job, a no-change rerun, then a reload at N+D replicas: wall-clock per run and per layer, cluster CPU/RAM per layer, rows, checksums, Iceberg data files, bytes and snapshots |

Each run writes `result.json` and one log per `olf` subprocess under
`--output` (default `<project-root>/.tmp/bench/<scenario>-<UTC time>`). A
step that fails is recorded with its elapsed time and error, the result's
`status` is `failed`, and the command exits 1: a stopped run never reads as a
fast one.

## How the numbers are taken

| Quantity | Source |
| --- | --- |
| Phase times | `olf deploy --phase foundation\|prefetch\|platform\|artifacts`, each a timed subprocess |
| CPU and memory | `docker stats` of the kind node containers, summed and sampled every `--sample-interval` seconds: kubelet, containerd and the control plane included, as the host pays them |
| Storage | `du` of `/var/lib/containerd` and `/var/local-path-provisioner` inside every node. kind keeps a copy of each image per node |
| Layer times | Dagster step stats: `<source>_bronze_resources` is Bronze, `<product>_dbt_gold_assets` is Gold, the Floe domain steps are Silver. A layer's time is its wall-clock span |
| Validity | Silver rows must equal the generated rows per entity, every Gold table must have rows, and a no-change rerun must reproduce every table's checksum |

The ETL dataset is deterministic. Replica 0 is the fixture itself; replica
`r` suffixes every primary-key value, and every column named after a primary
key (keys come from the Floe contracts), with `-R<r>`. The same scale always
produces byte-identical files, whose SHA-256 is recorded.

## Reproduce

From a source checkout on a host that meets the
[local installation](../guides/local-installation.md#hardware) sizing:

```bash
uv run --project tools/olf --locked olf bench footprint --clean
uv run --project tools/olf --locked olf bench etl --scale 100 --delta 10
uv run --project tools/olf --locked olf bench etl --scale 10000 --delta 1000
```

`result.json` records the commit, whether the tree was modified and which
files, the component-catalog SHA-256, host CPUs, memory, kernel and cgroup
version, the Docker version, and any kind-config or image override in effect.

## Known limits of the method

- Every Silver entity uses `incremental_mode: none` with `overwrite`, so the
  delta step is a full reload at a larger size, not an incremental merge.
- CPU and memory are cluster totals per window, not per pod: a layer's peak
  includes everything else the cluster did meanwhile.
- Restart, failure and retry cases (#343, #533, #53) and the ~100-table
  scenario C are not covered yet.
