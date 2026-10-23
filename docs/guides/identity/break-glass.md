# Break-glass access and issuer recovery

How a cluster administrator reaches a service when the issuer (Dex, and behind
it GitHub) is unavailable, and how to bring sign-in back. It applies to the
local provider.

## What happens when the issuer is down

The perimeter fails closed. It does not extend a session it cannot refresh and
has no grace period. Sessions that are already open keep working until their
access token expires, at most 5 minutes, then every protected route refuses
requests. New sign-ins fail. The portal is behind the perimeter, so it refuses
too. Superset's and OpenMetadata's own sessions cannot be renewed either.

Break-glass is the way in that does not depend on the issuer: your Kubernetes
access.

## Use `olf break-glass`

```bash
olf break-glass dagster --local-port 3000 --reason "Dex is down; checking the daily run"
```

Then open `http://127.0.0.1:3000`. The command runs until you exit (Ctrl-C) or
the time limit passes, and prints the record it left. `--reason` is required
and recorded. Dagster's remote port is 80, which an unprivileged user cannot
bind locally, hence `--local-port`.

| Service | What you get |
| --- | --- |
| `dex` | A port-forward to the Dex Service on `127.0.0.1:5556`, for diagnosis. Dex has no admin console or account. |
| `trino` | A port-forward to the Trino Service, bound to `127.0.0.1`. Over HTTPS when Trino serves it (the command prints the scheme): connect to `https://localhost:<port>`, not `127.0.0.1`, because the certificate names `localhost` and has no IP address; verify with the local CA, see `olf access trust`. |
| `dagster` | A port-forward to the Dagster webserver (remote port 80), in the DEV stage only. |
| `superset`, `openmetadata` | A shell in the service's pod, for its CLI. Their UIs accept only issuer login and are not reachable during an outage. `superset` reaches the DEV stage only. |

There is no `--stage` option: `dagster` and `superset` always target the DEV
namespace (tracked in #521).

| Option | Default | Meaning |
| --- | --- | --- |
| `--ttl-minutes` | 30 | Hard stop. The access ends when the process exits or the time passes. |
| `--local-port` | the remote port | Local port for the port-forward. |

Properties:

- **Time-boxed.** Nothing persistent grants access: the port-forward ends with
  its process, so there is nothing to remove afterwards.
- **Recorded.** Each use writes one ConfigMap, `break-glass-<time>-<id>`, in
  `olf-system`, labelled `openlakeforge.io/break-glass`. It holds the service,
  reason, kubeconfig user, start and TTL, and on exit the end and duration. It
  is never deleted. A record with no end is an unclean exit.
- **Perimeter untouched.** No Ingress, route or perimeter setting changes, and
  the ports bind to `127.0.0.1`.
- **Your kubeconfig is the authority.** The Kubernetes user recorded is what
  your kubeconfig says, not proof of identity.

See recent uses:

```bash
olf status --provider local
```

The "Recent break-glass use" section lists the latest records with their time,
user, service, duration and reason.

## Recover the issuer

1. Open a path to Dex to diagnose it, or read its state directly:

   ```bash
   export KUBECONFIG="$HOME/.openlakeforge/state/local/kubeconfig.yaml"
   KUBECTL="$(olf toolchain path kubectl)"
   "$KUBECTL" -n olf-system get pods
   "$KUBECTL" -n olf-system logs deployment/dex
   ```

2. Restore Dex. If it was scaled to zero, scale it back up, with the replica
   count it had (`<n>`, normally 1):

   ```bash
   "$KUBECTL" -n olf-system scale deploy/dex --replicas=<n>
   ```

   If it is running but unhealthy, restart it instead:

   ```bash
   "$KUBECTL" -n olf-system rollout restart deployment dex
   ```

3. If GitHub itself is unreachable, nothing in the cluster fixes it; use
   break-glass to keep working until it returns.
4. Sign in again on the portal.

## What was measured

`olf e2e run --env local` runs an issuer-outage drill with the perimeter on:
Dex is scaled to zero, a fresh sign-in is refused, Trino and Dagster answer
through recorded break-glass sessions, the ingresses are unchanged, then Dex is
restored and a fresh sign-in works. The drill signs in against the e2e's own
stub GitHub, not real GitHub, and runs only when Dex points at that stub. On
the local kind cluster in CI, the first
break-glass answer came 1 second after Dex was down, and sign-in worked within
2 seconds of Dex being ready again. Those are the timings of the cluster and
the run, not guarantees. The drill does not cover an open session.

Dex logs each sign-in and refresh: `kubectl -n olf-system logs
deployment/dex`. Kubernetes audit logging is not enabled on kind by default;
until it is, the recorded user is advisory.
