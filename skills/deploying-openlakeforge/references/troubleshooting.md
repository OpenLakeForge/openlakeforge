# Troubleshooting a local deployment

Match the error, apply the fix, then run `olf deploy --provider local` again.
Every command here assumes the local provider and the default cluster name.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Docker engine is not reachable` | Docker is stopped, or `olf` runs in another shell | Start Docker, check `docker ps`, rerun `olf` from that shell |
| `127.0.0.1:80 already in use` (or `:443`) | Another process holds the port | Find it with `lsof -nP -iTCP:80 -sTCP:LISTEN`, stop it |
| A tool is missing | Usually Docker; managed tools download on first use | `olf doctor --provider local --phase foundation` names it. With `OLF_TOOLCHAIN_MODE=host` the tool must be on `PATH` |
| `x509: certificate signed by unknown authority` on image pulls | A proxy intercepts HTTPS | The person configures Docker to trust their CA, then `olf deploy --provider local --phase prefetch` |
| Image pulls slow or time out | Large images | `olf deploy --provider local --phase prefetch`, then deploy again |
| `olf forward` fails to bind | Its ports are taken | Free 3000, 8080, 8088, 8181, 8585, 8888, 9000 and 9333 |

## The cluster is unreachable

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
KUBECTL="$(olf toolchain path kubectl)"
"$KUBECTL" --context kind-openlakeforge-local cluster-info
```

If that fails and `docker ps --filter name=openlakeforge-local` lists no
containers, Docker was reset and the cluster is gone. Recreate it in order:

```bash
olf deploy --provider local --phase foundation
olf deploy --provider local --phase platform
olf deploy --provider local --phase artifacts
```

## A pod does not start

Find the pod and its namespace in `olf status --provider local --json`, then:

```bash
"$KUBECTL" --context kind-openlakeforge-local describe pod "<pod>" -n "<namespace>"
"$KUBECTL" --context kind-openlakeforge-local logs "<pod>" -n "<namespace>"
```

Shared services run in `olf-system`; each stage's Dagster and Superset run in
`olf-<stage>`.

## The deployment has drifted

When a deploy keeps failing on half-created resources, rebuild only the
platform and keep the cluster. This deletes the platform's data, so ask first:

```bash
olf destroy --provider local --phase platform
olf deploy --provider local --phase platform
olf deploy --provider local --phase artifacts
```

## Collect evidence for a bug report

```bash
export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
olf diagnostics collect olf-diagnostics --namespace olf-system --kube-context kind-openlakeforge-local
```

Repeat with `--namespace olf-dev` for a stage. The files can contain hostnames
and project names: tell the person to read them before attaching them to a
public issue.
