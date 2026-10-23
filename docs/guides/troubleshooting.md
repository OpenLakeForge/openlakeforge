# Troubleshooting

How to see what a local deployment is running, fix the problems that most
often stop `olf deploy --provider local`, and collect evidence for a bug
report. Cloud-specific problems are at the end of
[Deploy to AWS](cloud/deploy-to-aws.md#troubleshooting) and
[Deploy to Azure](cloud/deploy-to-azure.md#troubleshooting).

Most fixes end the same way: `olf deploy` reconciles what exists, so running it
again is safe.

```bash
olf deploy --provider local
```

## Check the deployment

```bash
olf status --provider local
```

| Part | Shows |
| --- | --- |
| A note, when sign-in is off | `Authentication is off.`, plus the OpenMetadata login on Full |
| `URLs` | One line per service route |
| `Pods`, `Services`, `PVCs` | One block for `olf-system`, then one per stage namespace (`olf-dev`, ...) |
| `Certificates not Ready` | With HTTPS, every route certificate that is not issued; `none` when all are |

Start from a pod that is not `Running` or `Completed`, or a PVC that is not
`Bound`. `--stage prod` selects another stage and `--json` prints the same
report as JSON.

Before a first deploy, `olf doctor` checks the host without changing anything.
Each line reads `PASS` or `FAIL`:

```bash
olf doctor --provider local --phase foundation
```

## Use kubectl

`olf` keeps the cluster's credentials in its own kubeconfig, so your
`~/.kube/config` is untouched. Set it, with the kubectl `olf` manages, in each
shell:

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
KUBECTL="$(olf toolchain path kubectl)"
```

Use `$OLF_HOME/state/local/kubeconfig.yaml` if you set `OLF_HOME`. The context
is `kind-openlakeforge-local`.

## Docker is unavailable

`olf init` stops with `Docker engine is not reachable`, or `olf deploy` cannot
create the cluster or build images. Start your Docker engine, check that
`docker ps` works, and run `olf` from that same shell.

## A tool is missing

`olf` provisions Terraform, Helm, kubectl and kind itself, so a missing tool is
usually Docker. `olf doctor --provider local --phase foundation` names the
tool and the reason. A managed tool that cannot be downloaded needs network
access to its release host; with `OLF_TOOLCHAIN_MODE=host`, the tool must be on
your `PATH`.

## Ports 80 or 443 are in use

`olf deploy` stops before it creates the cluster with
`127.0.0.1:80 already in use` (or `:443`). Find the process, stop it and deploy
again:

```bash
lsof -nP -iTCP:80 -sTCP:LISTEN
```

`olf forward` needs its own ports free: 3000, 8080, 8088, 8181, 8585, 8888,
9000 and 9333.

## The cluster is unreachable

```bash
"$KUBECTL" --context kind-openlakeforge-local cluster-info
```

If it fails and `docker ps --filter name=openlakeforge-local` lists no node
containers, the Docker engine was reset. Recreate the cluster, the platform
and your code:

```bash
olf deploy --provider local --phase foundation
olf deploy --provider local --phase platform
olf deploy --provider local --phase artifacts
```

## A pod does not start

Find it in `olf status`, then read why. Use the namespace it is listed under:

```bash
POD="<name of the failing pod>"
"$KUBECTL" --context kind-openlakeforge-local describe pod "$POD" -n olf-system
"$KUBECTL" --context kind-openlakeforge-local logs "$POD" -n olf-system
```

For a pod with several containers, add `-c "<container name>"` to `logs`.

## Image pulls are slow or fail

`olf deploy` pre-fetches the large images into the kind nodes. After a failed
pull, run that step alone, then deploy again:

```bash
olf deploy --provider local --phase prefetch
```

A network that intercepts HTTPS, such as a corporate proxy, makes the pulls
fail with `x509: certificate signed by unknown authority`. Configure your
Docker engine to trust your organization's CA, check that `docker pull` works,
then run the prefetch step again.

## A browser or curl cannot open a service

With HTTPS, the browser must trust the local CA (`olf access trust`), and
command-line tools may need `--resolve` for `*.localhost` names. See
[Access the services](local-installation.md#access-the-services). With sign-in
on, see [Troubleshoot sign-in](identity/connect-github.md#troubleshooting).

## The deployment has drifted

If `olf deploy` fails on half-created resources, run it again first. To rebuild
only the platform and keep the cluster:

```bash
olf destroy --provider local --phase platform
olf deploy --provider local --phase platform
olf deploy --provider local --phase artifacts
```

For a clean start, run `olf destroy --provider local`, which deletes all data.

## Collect diagnostics

`olf diagnostics collect` writes a directory of text files for a bug report. A
command that fails is recorded in its own file, and the rest still run:

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
olf diagnostics collect olf-diagnostics-dev \
  --namespace olf-dev --kube-context kind-openlakeforge-local
```

Repeat it with `--namespace olf-system` for the shared services. The directory
holds host disk and memory, `docker system df`, cluster nodes, pods and events,
and for the namespace its workloads, events, pod descriptions and the last 200
log lines of every container.

> [!WARNING]
> Logs and descriptions can contain hostnames and project names. Read the files
> before you attach them to a public issue.

## Check it worked

```bash
olf status --provider local
```

Every pod is `Running` or `Completed`, and
`olf e2e run --env local --suite full` ends without an error.
