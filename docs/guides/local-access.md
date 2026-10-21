# Access the local stack

How to reach the services of a local OpenLakeForge deployment: plain HTTP or
HTTPS under `olf.localhost`, how names resolve from a browser and from the
command line, and `olf forward` as the fallback for services without a route.

The local stack runs one ingress (Traefik) on `127.0.0.1` ports 80 and 443 and
publishes each service as a hostname under `spec.access.base_domain`, which is
`olf.localhost` by default. It is local evaluation infrastructure: do not
expose it beyond your workstation.

## Find the URLs

```bash
olf status --provider local
```

The `URLs` section lists one entry per service, keyed by contract reference.
With the default profile they look like this:

| Service | URL |
| --- | --- |
| Portal | `http://olf.localhost` |
| Dagster, per stage | `http://dagster-dev.olf.localhost` |
| Trino | `http://trino.olf.localhost` |
| Superset, per stage (Full preset) | `http://superset-dev.olf.localhost` |
| OpenMetadata (Full preset) | `http://openmetadata.olf.localhost` |

`olf status --provider local --json` prints the same URLs under a `urls` key.
Polaris and SeaweedFS have no route; see
[Reach a service without a route](#reach-a-service-without-a-route).

## Choose HTTP or HTTPS

`spec.access` in `openlakeforge.yaml` decides the scheme. `olf init` writes
plain HTTP:

```yaml
spec:
  access:
    issuer: none
```

With `issuer: none` the routes are `http://` URLs and there is no certificate
step. To serve HTTPS, remove the `issuer` line so the field takes its default,
`local-ca`:

```yaml
spec:
  access: {}
```

Then apply the change:

```bash
olf deploy --provider local
```

`olf deploy` creates a local certificate authority (CA) once, in the cluster,
and cert-manager signs every route with it. A re-deploy keeps the CA. The
local provider ships only these two issuers; any other value is rejected
before the cluster is touched.

> [!NOTE]
> `issuer: none` is accepted only for `provider: local` with a base domain
> ending in `.localhost`; `olf` rejects it for any other provider or domain.

### Trust the CA in your browser

A browser rejects the HTTPS routes until it trusts the local CA. Export it and
print the commands for your machine:

```bash
olf access trust
```

It writes `openlakeforge-local-ca.crt`, prints the path, and prints the trust
commands below for the detected system. `--output <path>` chooses where the
file goes. `olf` prints the commands rather than running them, because each
needs your own privileges.

| System | Store | Command |
| --- | --- | --- |
| Linux (Debian, Ubuntu) | system | `sudo cp "$CRT" /usr/local/share/ca-certificates/openlakeforge-local-ca.crt && sudo update-ca-certificates` |
| Linux (Fedora, RHEL) | system | `sudo cp "$CRT" /etc/pki/ca-trust/source/anchors/openlakeforge-local-ca.crt && sudo update-ca-trust` |
| Linux | Chrome and Chromium (NSS) | `certutil -d sql:$HOME/.pki/nssdb -A -t C,, -n "OpenLakeForge local CA" -i "$CRT"` |
| macOS | System keychain | `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain "$CRT"` |
| WSL2 | Windows, current user | `powershell.exe -NoProfile -Command "Import-Certificate -FilePath '$(wslpath -w "$CRT")' -CertStoreLocation 'Cert:\CurrentUser\Root'"` |

Set `CRT` to the path `olf access trust` printed before you run one:

```bash
CRT=/path/printed/by/olf/openlakeforge-local-ca.crt
```

Firefox keeps its own store: Settings, Privacy & Security, View Certificates,
Authorities, Import.

On WSL2 your browser runs on Windows, so the CA must go into the Windows
store. Trusting it inside the Linux distribution only covers tools you run
there.

`olf` itself needs none of this: its clients read the CA from the cluster and
verify against it.

## Resolve names

Browsers resolve every `*.localhost` name to your machine (RFC 6761), so
`/etc/hosts` needs no entry. Other command-line tools often do not: glibc
without systemd-resolved and macOS may fail to resolve the name. `olf`'s own
clients pin `*.localhost` to `127.0.0.1` themselves. For `curl`, do the same
with `--resolve`:

```bash
curl --resolve dagster-dev.olf.localhost:80:127.0.0.1 \
  http://dagster-dev.olf.localhost/server_info
```

For HTTPS, use port 443 and the `https` URL, and tell curl which CA to trust
with `--cacert "$CRT"`.

With `spec.identity` enabled, every route except `auth.<base_domain>` sits
behind the sign-in perimeter. An unauthenticated request then answers `302`
to the sign-in page, or `401` when it sends `-H 'Accept: application/json'`.
A signed-in user whose role the service does not permit gets a `403` page.

## Reach a service without a route

Polaris and SeaweedFS have no route. `olf forward` starts
`kubectl port-forward` processes to fixed ports on `localhost` and keeps them
running until you press Ctrl-C:

```bash
olf forward --provider local
```

| Port | Service |
| ---: | --- |
| 3000 | Dagster |
| 8080 | Trino |
| 8088 | Superset (Full preset) |
| 8181 | Polaris API |
| 8585 | OpenMetadata (Full preset) |
| 8888 | SeaweedFS Filer UI |
| 9000 | SeaweedFS S3 API |
| 9333 | SeaweedFS Master UI |

Stop whatever holds a port before you run `olf forward`.

### Make `olf` use port-forwards

`olf`'s own commands, such as `olf e2e run`, call services through the
ingress. They port-forward instead for a route behind an enabled sign-in
perimeter, because they hold no browser session; that needs permission to
create `pods/portforward` on the cluster. Set `OLF_PORT_FORWARD=1` to make
them port-forward for every route, for example while you debug the ingress:

```bash
OLF_PORT_FORWARD=1 olf e2e run --env local
```

## Check it worked

```bash
curl --resolve dagster-dev.olf.localhost:80:127.0.0.1 -s -o /dev/null \
  -w '%{http_code}\n' http://dagster-dev.olf.localhost/server_info
```

With `issuer: none` and no sign-in it prints `200`. With HTTPS, use the
`https` URL, port 443 and `--cacert "$CRT"`. With sign-in on it prints `302`.
