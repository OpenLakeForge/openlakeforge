# Run behind your own HTTPS

This guide puts OpenLakeForge behind a reverse proxy, tunnel or load balancer that you operate and that terminates HTTPS, using `spec.access.tls: external` on the local provider. OpenLakeForge changes no DNS record and requests no certificate.

## What you own and what OpenLakeForge does

| You provide | OpenLakeForge does |
| --- | --- |
| A public name, `base_domain`, and a wildcard record `*.<base_domain>` plus the bare name pointing at your edge | Serves plain HTTP on Traefik's `web` entrypoint, with no TLS store and no host port 443 |
| A certificate your browsers trust for both names, installed on the edge | Generates every route URL, OIDC redirect URI and the sign-in cookie as `https://`, the cookie `Secure` |
| A proxy that forwards to Traefik and sets `X-Forwarded-Proto: https` and `X-Forwarded-Host` | Trusts those two headers from `trusted_proxies`, and from no one else. The sign-in proxy behind Traefik then reads the same headers, so its redirects stay `https://` |

Examples of an edge: Caddy or nginx on the host, a Cloudflare Tunnel, a cloud load balancer. A request that reaches Traefik from an address outside the range you name is handled as plain HTTP, whatever `X-Forwarded-Proto` it carries.

## Configure

1. Set the access block in `openlakeforge.yaml`. `external` is refused on a `.localhost` name, and `trusted_proxies` is required with it (and refused with any other `tls`): the CIDR blocks your proxy connects to Traefik from. `0.0.0.0/0` is refused. Only those peers' `X-Forwarded-Proto` and `X-Forwarded-Host` are believed.

   ```yaml
   spec:
     access:
       base_domain: olf.example.com
       tls: external
       trusted_proxies: [203.0.113.0/24]
   ```

   List the range as Traefik sees it, not your proxy's public address. For a proxy on the machine that hosts kind, start from the subnet of Docker's `kind` network (`docker network inspect kind`), and confirm with the sign-in check below: if the range is wrong, redirects stay `http://`.

2. Apply the profile:

   ```bash
   uv run --project tools/olf olf infra platform apply -f openlakeforge.yaml
   ```

3. Point your edge at Traefik. On kind, Traefik listens on `127.0.0.1:80` of the machine that runs the cluster; forward every host under `base_domain` there and keep the `Host` header.

4. For sign-in, register `https://auth.<base_domain>/callback` with the identity provider, as in [connect GitHub](identity/connect-github.md).

`olf infra access trust` does not apply: no local CA signs the routes.

## Limits

| Caller | Behaviour with `external` |
| --- | --- |
| Browsers, `olf`, the perimeter proxy, Polaris, the platform API | Work as with `local-ca`; the in-cluster callers reach Dex at its Service |
| OpenMetadata, Superset | Still call `https://auth.<base_domain>` from inside the cluster. The cluster's DNS must resolve that name to your edge and the edge's certificate must be publicly trusted. Tracked on the remaining items of #991 |

OpenLakeForge adds no CoreDNS rewrite for `external`: Traefik behind your edge serves no TLS for a pod to reach.

## Check it worked

```bash
curl -sI https://<base_domain>/ | head -1
```

It answers `200` through your edge. Open `https://<base_domain>/` in a browser and sign in: the session cookie is `Secure` and the callbacks are `https://` URLs.
