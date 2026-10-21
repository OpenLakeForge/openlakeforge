# Check status and collect diagnostics

How to see what a local OpenLakeForge deployment is running and where to open
it, check the toolchain before a deploy, and gather the evidence for a bug
report with `olf diagnostics collect`.

## Check the deployment

```bash
olf status --provider local
```

The report has these parts, in this order:

| Part | Shows |
| --- | --- |
| A note, when sign-in is off | `Authentication is off.`, plus the OpenMetadata login on the Full preset |
| `URLs` | One line per service route, `<contract ref>: <url>` |
| `Pods`, `Services`, `PVCs` | One block per resource kind for the shared namespace `olf-system`, then for each enabled stage namespace (`olf-dev`, `olf-uat`, `olf-prod`) |
| `Certificates not Ready` | Every cert-manager Certificate whose `Ready` condition is not `True`, with the reason and message; `none` means every route certificate is issued |

A pod that is not `Running` or `Completed`, or a PVC that is not `Bound`, is
the place to start. `Certificates not Ready` matters for the HTTPS stack only:
a failed renewal keeps serving the old certificate until it expires, so
nothing else in the report shows it.

Select another stage, or get the report as JSON:

```bash
olf status --provider local --stage prod
olf status --provider local --json
```

The JSON document has a `urls` object, a `sections` object keyed by the block
titles above, and a `notes` list when there are notes.

## Open a service

Every URL in the `URLs` section works in a browser. The portal at
`http://olf.localhost` links to each service the profile enables. See
[Access the local stack](local-access.md) for HTTPS, name resolution and the
port-forward fallback.

## Check the host before a deploy

```bash
olf doctor --provider local --phase foundation
```

It changes nothing. Each line reads `PASS` or `FAIL`, and the command exits
`1` if any fails. It reports the project (and why it is not runnable, if it
is not), the platform payload, the Terraform roots, the toolchain mode, each
tool it needs with where it resolves, and whether the Docker engine answers.

`--phase foundation` checks what a first deploy needs. The default, `all`,
also checks the platform's provider contracts, which exist only after a
deploy, so on a machine with no deployment yet it reports a `FAIL` for them.

## Collect diagnostics

`olf diagnostics collect` writes one directory of text files and tolerates
partial failure: a command that fails is recorded in its own file, and the
rest still run. It takes the directory to write, the namespace to inspect and
the kube context:

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
olf diagnostics collect olf-diagnostics-dev \
  --namespace olf-dev --kube-context kind-openlakeforge-local
```

`olf` keeps the cluster's credentials in its own kubeconfig, apart from
`~/.kube/config`, so point `KUBECONFIG` at it as above. If you set `OLF_HOME`,
use `$OLF_HOME/state/local/kubeconfig.yaml`. Repeat the command with
`--namespace olf-system` for the shared services, and with each other stage
namespace you run. Without `--namespace`, it inspects `olf-dev`. Without
`--kube-context`, it uses the current context of `KUBECONFIG`.

The directory then holds:

| File | Content |
| --- | --- |
| `host-disk.txt`, `host-memory.txt` | Disk and memory of the host |
| `docker-disk.txt` | `docker system df` |
| `nodes.txt`, `all-pods.txt`, `all-events.txt` | Cluster-wide nodes, pods and events |
| `workloads.txt`, `events.txt`, `pod-descriptions.txt` | Everything in the chosen namespace |
| `pod-names.txt`, `<pod>.log` | The pod list, and the last 200 lines of every container of each pod |

> [!WARNING]
> Pod logs and descriptions can contain hostnames and project names. Read the
> files before you attach them to a public issue.

## Check it worked

```bash
ls olf-diagnostics-dev
```

It lists the files above. `olf diagnostics collect` itself ends with
`Diagnostics written to olf-diagnostics-dev`.
