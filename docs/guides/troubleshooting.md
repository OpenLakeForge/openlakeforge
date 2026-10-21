# Troubleshoot a local deployment

Fixes for the problems that most often stop `olf deploy --provider local`: a
Docker engine that is down, a missing tool, taken ports, an unreachable
cluster, pods that do not start, slow or blocked image pulls and a deployment
that has drifted. Find your symptom, run the commands under it, then deploy
again.

Every fix ends the same way. `olf deploy` reconciles what exists and is safe to
run again:

```bash
olf deploy --provider local
```

## Look at the cluster with kubectl

Several fixes below read the cluster directly. `olf` keeps the cluster's
credentials in its own kubeconfig, so your `~/.kube/config` is untouched. Set
it, and use the `kubectl` that `olf` manages, in each shell:

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
KUBECTL="$(olf toolchain path kubectl)"
```

If you set `OLF_HOME`, use `$OLF_HOME/state/local/kubeconfig.yaml`. The cluster
is `openlakeforge-local`, its context `kind-openlakeforge-local`, the shared
services run in the namespace `olf-system` and each stage in `olf-<stage>`.

## Docker is unavailable

`olf init` stops with `Docker engine is not reachable`, or `olf deploy` cannot
create the cluster or build images.

```bash
docker ps
```

If that fails, start your Docker engine and run `olf` from the same shell that
can run `docker ps`.

## A tool is missing

`olf` provisions Terraform, Helm, kubectl and kind itself, so a missing tool is
usually Docker. Ask `olf` for the actionable reason:

```bash
olf doctor --provider local --phase foundation
```

A line that reads `FAIL` names the tool and why. A managed tool that cannot be
downloaded needs network access to the release host; with
`OLF_TOOLCHAIN_MODE=host`, the tool must be on your `PATH`. See
[Install options](install-options.md).

## Ports 80 or 443 are in use

`olf deploy` stops before it creates the cluster, with a message that starts
`127.0.0.1:80 already in use` (or `:443`). The local ingress publishes both
ports. Find the process, stop it, and deploy again:

```bash
lsof -nP -iTCP:80 -sTCP:LISTEN
```

`olf forward` needs its own ports free: 3000, 8080, 8088, 8181, 8585, 8888,
9000 and 9333.

## The cluster is unreachable

```bash
"$KUBECTL" --context kind-openlakeforge-local cluster-info
```

If it fails, check that the cluster still exists in Docker:

```bash
docker ps --filter name=openlakeforge-local
```

If no node container is listed, the Docker engine was reset. Recreate the
cluster, then the platform and your code:

```bash
olf deploy --provider local --phase foundation
olf deploy --provider local --phase platform
olf deploy --provider local --phase artifacts
```

## A pod does not start

Find it, then read why:

```bash
olf status --provider local
POD="<name of the failing pod from the status output>"
"$KUBECTL" --context kind-openlakeforge-local describe pod "$POD" -n olf-system
"$KUBECTL" --context kind-openlakeforge-local logs "$POD" -n olf-system
```

Use the namespace the pod is listed under: `olf-system`, or `olf-dev` and the
other stage namespaces. For a pod with several containers, add
`-c "<container name>"` to `logs`. If the cause is not clear, collect the
evidence for a bug report with `olf diagnostics collect`; see
[Check status and collect diagnostics](status-and-diagnostics.md).

## Image pulls are slow or fail

`olf deploy` pre-fetches the large runtime images into the kind nodes before
the platform is installed. After a failed pull, run that step alone and deploy
again:

```bash
olf deploy --provider local --phase prefetch
```

## A corporate proxy breaks image pulls

A network that intercepts HTTPS makes the Docker engine or the kind nodes
reject registries with an error like this:

```text
x509: certificate signed by unknown authority
```

That is a container-runtime trust problem, not an OpenLakeForge one. Configure
your Docker engine to trust your organization's CA certificate, check that
`docker pull` works, then run the prefetch step above and deploy again.

## A browser or curl cannot open a service

With the HTTPS stack, the browser must trust the local CA, and command-line
tools may need `--resolve` for `*.localhost` names. Both are covered in
[Access the local stack](local-access.md).

## The deployment has drifted

If `olf deploy` fails on resources that exist in a half-finished state, run it
again first: Terraform reconciles what it can. To rebuild only the platform and
keep the cluster:

```bash
olf destroy --provider local --phase platform
olf deploy --provider local --phase platform
olf deploy --provider local --phase artifacts
```

For a clean start, tear everything down as described in
[Tear down a local deployment](tear-down.md).

## Check it worked

```bash
olf status --provider local
```

Every pod is `Running` or `Completed`, and `olf e2e run --env local --suite
full` ends without an error.
