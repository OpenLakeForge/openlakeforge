# ADR 0016: Trino workload authentication — password file over HTTPS

## Status

Decided, proven in isolation, and **migrated on the local root**: step 4 (the
`password` default and the isolation gate) is done (#471). AWS and Azure remain
unauthenticated. Built: the
credential Job in `modules/query/trino` creates the principal Secrets,
`trino-password-file` and `trino-internal-secret` and replicates each stage's
principal. The switch `trino_workload_authentication` has three levels, `off`
(nothing differs from before), `tls` (the module issues the
serving certificate and adds an HTTPS listener beside the HTTP one; dbt run
pods, `olf e2e` and Superset use it, sending only the user name: migration steps 2-3),
and `password` (the local default; the HTTP listener is replaced, `PASSWORD` authentication and the
`principals` rules turn on, and Traefik gets a verified HTTPS transport to
Trino: step 4). The OpenMetadata Trino crawl is an OLF-owned Job (its
server-created ingestion pods cannot receive the CA) that verifies the HTTPS
listener at `tls`. At `password` every caller reads its own credential from a
Secret (step 4, clients: below), and the switch defaults to `password` in the
local root when the profile has `spec.access`, which publishes the CA ConfigMap
callers verify against; without it the default stays `off`. `olf e2e` runs the
isolation probes below. What binds is
the selection, the principal mapping and the migration order. The local root authenticates by default; AWS and Azure serve Trino
unauthenticated, and the row in `docs/technical-debt.md` stays open for them.

## Context

One Trino 480 coordinator (chart 1.42.2) in `olf-system` serves every stage's
catalog (`lakehouse_<stage>`). Access is decided by file-based rules
(`modules/query/trino`, `catalog_access_rules`) keyed on the **user name**,
which is whatever the client sends in `X-Trino-User`; Trino accepts it
unauthenticated, so any pod that reaches the Service can claim
`olf-prod-runtime` (proved below, case 0a). Superset, dbt, OpenMetadata and
`olf e2e` are the callers ([`identity-callers.md`](../architecture/identity/identity-callers.md)).
#26 requires one non-interactive mechanism for all of them, separate from the
human OAuth2 login for the Trino UI.

## Decision

**Trino `PASSWORD` authentication with the `file` authenticator, on an HTTPS
listener, with a `principals` rule binding each verified principal to exactly
its own user.** One password per principal, created in-cluster. The username is
a claim; the password is the proof.

### Options

Client support is read from the images this repository pins.

| Caller (pinned client) | Password | JWT | Client certificate |
| --- | --- | --- | --- |
| Superset 6.1.0 (`trino` 0.340) | URI password or `encrypted_extra` `auth_method: basic` | `auth_method: jwt` | `auth_method: certificate` (key files in the pod) |
| dbt-trino 1.10.2 (`trino` 0.338) | `method: ldap` | `method: jwt` | `method: certificate` |
| OpenMetadata 1.13.6 Trino connector | `authType: BasicAuth` | `authType: JwtAuth` | **none**: only Basic, JWT, Azure, OAuth2 |
| Trino CLI 480 (`olf e2e`) | `--password` (`TRINO_PASSWORD`) | `--access-token` | `--keystore-path` |
| Credential to principal | the file entry **is** the principal | the `sub` of any token a trusted key signed | the subject of any certificate the CA signed |

| Option | Outcome |
| --- | --- |
| **Password file** | **Selected.** All four clients support it. A leaked DEV password yields only DEV. Revoked by deleting a line, rotated without a restart (case e). No dependency on the issuer or a signing service at query time |
| JWT | Rejected. Trino has one issuer and one key set and accepts any trusted key for any `sub`, so a per-stage signing key does not stop that key minting `olf-prod-runtime`; the binding lives in the minting job and its key is the crown jewel. Superset, dbt and OpenMetadata cannot refresh a token, so they would hold long-lived bearer tokens that cannot be revoked one by one. Short issuer tokens would make every dashboard query depend on the issuer being up |
| Client certificates | Rejected. OpenMetadata's connector has no certificate option, so one caller would need a second mechanism, and Superset would need key files mounted in each pod. Otherwise attractive: cert-manager creates the keys in-cluster |
| Trusted `X-Trino-User` header | Rejected by #26: a claimed name is not proof |
| LDAP, Kerberos | Rejected: need a directory or KDC the platform does not run |

What each option needs **on the Trino server** is from Trino's documentation
and was not run; the client capabilities above and the password path were.

### Principal mapping

| Principal (`password.db` user) | May become | Catalog access (rules unchanged) | Used by |
| --- | --- | --- | --- |
| `olf-<stage>-runtime` | itself only | `all` on `lakehouse_<stage>` | that stage's Superset, dbt run pods, `olf e2e` |
| `openmetadata` | itself only | `read-only` on every stage catalog | OpenMetadata service and ingestion |

`principals` rules in `rules.json` list each principal with the one user it may
assume and end with a deny-all rule. Trino then answers: no credential on
HTTPS 401, any query on an HTTP listener 403 (the module has none, see k2),
wrong password 401, valid password with an `X-Trino-User` outside the principal's list "Principal X cannot become
user Y" before any catalog rule, and only then the catalog and table rules for
the resulting user. Callers of one stage share its credential (the
shared-service-account limitation in `identity-callers.md`).

### Secrets

Created by an in-cluster Job owned by `modules/query/trino`, never by Terraform
([`identity-credentials.md`](../architecture/identity/identity-credentials.md),
`modules/identity/dex/credentials.tf`); the contract fields are the migration's.

| Secret | Namespace | Content | Created |
| --- | --- | --- | --- |
| `trino-credential-<principal>` | `olf-system` | `username`, `password` (32 random alphanumeric) | only if missing |
| `trino-credential` | that principal's stage namespace only | copy of its `username`, `password` | by the Job, like the Polaris credential replication |
| `trino-password-file` | `olf-system` | `password.db`, derived from the principal Secrets on every run | rebuilt; never the source of truth |
| `trino-internal-secret` | `olf-system` | `internal-communication.shared-secret` | only if missing |

The bootstrap image (`alpine/k8s:1.30.0`) has `python3` but no `openssl` or
`htpasswd`, so the Job writes PBKDF2 entries with `hashlib.pbkdf2_hmac`
(`password_line` in the proof script is the reference; Trino also accepts
bcrypt). A stage namespace never receives another stage's Secret. Chart wiring:
`auth.passwordAuthSecret: trino-password-file`,
`server.config.authenticationType: PASSWORD`, `server.config.https.enabled`.

### TLS and trust

| Piece | Decision |
| --- | --- |
| Certificate | cert-manager `Certificate` from the `local-ca` ClusterIssuer (ADR 0013), SANs `trino`, `trino.olf-system`, `trino.olf-system.svc`, the cluster-local FQDN and `localhost` (the in-pod CLI), PKCS8 key |
| Keystore | Trino reads a PEM file of the PKCS8 key then the certificate chain. The `Certificate` asks cert-manager for `CombinedPEM` (key `tls-combined.pem`, a GA output format in the pinned v1.21.2: no feature gate); the Secret is mounted as a directory at `/etc/trino/tls`. Case k1 serves from a file in that layout. **Not followed on renewal**: with the file replaced by the symlink swap a Secret volume uses, the served certificate was unchanged 90 seconds later (run by hand, not in the script), so renewal needs a coordinator restart; the certificate lasts a year (`renewBefore` 30 days) |
| Workload trust | callers use `trino.olf-system.svc.cluster.local:8443` and mount ConfigMap `olf-local-ca` (`ca.crt`, already in `olf-system` and every stage namespace, ADR 0013 decision 8) |
| HTTP port | **Disabled** (`http-server.http.enabled=false`) when authentication is on, so no cleartext listener exists (k2). Without that, the HTTP port refuses a password only until a pod spoofs `X-Forwarded-Proto: https` (k0). The server then reaches itself at `https://localhost:8443` (a name in the certificate) with the CA as its internal truststore, `node.internal-address=localhost` and `internal-communication.https.required=true`; without those the node's calls to itself fail the handshake on its pod IP, which the certificate cannot name. That is single-node only, as local is (`workers: 0`) |
| Probes | The image's `health-check`, which the chart runs for readiness and startup, uses HTTPS once HTTP is disabled, and `GET /v1/info` on HTTPS needs no credential (h2). The chart's **liveness** probe is hardwired to the named HTTP port and takes only timings, so with HTTP off it can only fail; the module sets it to once an hour with 1000 failures, which makes it inert. A hung coordinator is therefore pulled from service by readiness but not restarted. A chart that lets the liveness probe be replaced would restore it |
| Ingress hop | Traefik re-encrypts to Trino's HTTPS port and verifies it against the local CA. `http-server.process-forwarded=true` stays, since the forwarded headers Traefik adds are otherwise refused with `406`; with no HTTP listener a spoofed `X-Forwarded-Proto` has nothing to downgrade (k0 against k2). The Trino Service carries `traefik.ingress.kubernetes.io/service.serversscheme: https` and `...service.serverstransport: trino@file`; the transport is in Traefik's file provider with `rootCAs` and `serverName` (the Service FQDN), and the CA comes from a Secret that cert-manager issues for that purpose (`trino-ca-bundle`) so that Traefik does not mount Trino's private key. Cases t1 and t2 run the pinned Traefik image with that transport against the proof server; the Kubernetes annotations that select it were not run. The Web UI sign-in is below (it is `web-ui.authentication.type`, not an addition to `PASSWORD`) |

### Web UI sign-in (human OAuth2)

Trino 480 (docs: <https://trino.io/docs/480/security/oauth2.html>,
<https://trino.io/docs/480/admin/properties-web-interface.html>; the filter is
`io.trino.server.ui.OAuth2WebUiAuthenticationFilter`, read at tag `480`).

| Question | Finding |
| --- | --- |
| Coexistence with workloads | `web-ui.authentication.type=oauth2` is chosen apart from `http-server.authentication.type`: `OAuth2WebUiModule` installs the OAuth2 service itself, and Trino's own `TestOAuth2WebUiAuthenticationFilterWithJwt` sets no `http-server.authentication.type`. `/v1` stays `PASSWORD` (or none at `off`/`tls`); dbt, Superset, the ingestion Job and `olf e2e` do not touch the UI. No `PASSWORD,OAUTH2` list is needed, and none is used, because that would also let API clients present Dex bearer tokens |
| Secure request | The filter redirects any request whose `isSecure()` is false to the "UI disabled" page. Behind Traefik, `isSecure()` is true when `process-forwarded` is on and the request carries `X-Forwarded-Proto: https`. So the ingress must be HTTPS (`spec.access.issuer` not `none`); the coordinator's own listener may be either. **With `issuer: none` the UI cannot sign in through Dex: it keeps its own login** (the module receives no `web_ui_oidc`) |
| Redirect URI | `request.getUriInfo().getBaseUri().resolve("/oauth2/callback")`; with `process-forwarded` that is `https://trino.<base_domain>/oauth2/callback` from `X-Forwarded-Host` and `-Proto`. The `trino` static Dex client carried `https://trino.<base_domain>/*` (a Keycloak leftover; Dex matches exactly), now the exact callback. Not run: whether Jetty adds `:443` to the URI from `X-Forwarded-Port` |
| Issuer reachability | Trino loads the issuer's metadata at startup and fails after 30 seconds, and `https://auth.<base_domain>` is served by Traefik, which is created after Trino (it takes Trino's backend transport). So the module turns discovery off and sets `auth-url`, `token-url`, `jwks-url` from Dex's fixed paths (`/auth`, `/token`, `/keys`): **Dex only**; an `issuer: external` issuer needs discovery and an egress flow, and is not wired. Trino reaches Dex by the public URL through the CoreDNS rewrite (flow F4) and trusts the local CA through `oauth2-jwk.http-client.trust-store-path` (the file already mounted for the internal truststore) |
| Principal | `principal-field=email` with `scopes=openid,email` (Dex adds `email` to the token only when asked; Trino requires the field in the access token). The Trino user is the e-mail address, with the default user mapping |
| Client secret | `TRINO_OIDC_CLIENT_SECRET` from the Secret the `identity.oidc` contract names (`dex-client-trino`), read in `config.properties` as `${ENV:...}`; never in Terraform |
| When it is on | `spec.identity` with the `github` issuer, an HTTPS route, and `trino_workload_authentication` `tls` or `password`. Off otherwise, so authentication-off mode (#421) is unchanged |

A signed-in person is user `<email>`, to which the catalog rules give no
catalog: the sign-in restricts who opens the UI, not what a person may query,
and at `password` the final `principals` rule would also refuse that user as a
query submitter. Data access for people is #179. Rendered config is tested in
`tools/olf/tests/infra/test_trino_oidc_config.py`; the browser flow is not proved.

### Consumer configuration

| Caller | Change in the migration |
| --- | --- |
| Superset (per stage) | `olf infra report import` writes `trino://<principal>:XXXXXXXXXX@trino.olf-system.svc.cluster.local:8443/lakehouse_<stage>` and the CA as `server_cert`; it reads the password from `trino-credential` through RBAC and sends it only in the import request's `passwords` field. The `XXXXXXXXXX` placeholder is Superset's own: without it `set_sqlalchemy_uri` overwrites the imported password. Superset stores the password encrypted in its metadata DB; it is in neither the zip nor the URI. The connection uuid is unchanged |
| dbt in Dagster run pods | `libs/dbt/profiles/*.yml`: `method: ldap`, `password: env_var('OPENLAKEFORGE_DBT_TRINO_PASSWORD')` from a `secretKeyRef` on `trino-credential`, `cert:` the mounted CA, FQDN host, port 8443. `impersonation_user` is there for #179 |
| OpenMetadata | the metadata crawl is the OLF-owned Job `openmetadata-trino-ingestion` (`olf infra openmetadata deploy-metadata`; pinned `ingestion-base` image, `metadata ingest`), which mounts `olf-local-ca` and sets `verify`, not the OpenMetadata pipeline client, whose pods accept no volumes in 1.13.6. The Job's config carries `authType: {password}` (from `trino-credential-openmetadata`) when the query contract names a credential; the service the bootstrap registers carries no password |
| `olf e2e` | the CLI in the coordinator pod: `--server https://localhost:8443 --truststore-path <CA> --user <principal>`, password in `TRINO_PASSWORD`, never on a command line. `_isolation.py` gains the direct-request cases below |

### Rotation

Delete `trino-credential-<principal>` and replace the Job: it regenerates the
Secret, rebuilds `password.db` and re-copies the stage Secret; Trino re-reads
the file with no restart (case e). Consumers then re-sync: Superset by
re-running the connection import, dbt run pods at their next launch,
OpenMetadata by re-running its bootstrap; until then that principal's callers
see 401. A zero-downtime overlap was not exercised.

### Migration and recovery

An unauthenticated Trino refuses a password even over HTTPS (case 0c), so
callers cannot be given credentials first. Order:

| Step | Phase | Effect on callers |
| --- | --- | --- |
| 1. Contract carries the credential reference, scheme and CA | platform | none |
| 2. Job creates and replicates credentials; Trino gets the certificate and HTTPS listener, authentication still off | platform | none |
| 3. Callers move to HTTPS and the CA, still sending only the user name (accepted, case 0b). OpenMetadata's Trino ingestion moves by becoming an OLF-owned Job (consumer table) | platform, artifacts | none; proves trust from every caller |
| 4. Cutover: enable `PASSWORD` and `principals`, ship the passwords in the same deploy | platform, then artifacts | Trino already restarts (`Recreate`); callers fail until their password lands, in-flight runs retry |
| 5. `olf e2e` runs the isolation cases, an unattended pipeline and a dashboard query | e2e | none |

Recovery is the inverse deploy (authentication off, callers back to the user
name); a server-only toggle does not work, because an unauthenticated server
refuses a caller that sends a password. Break-glass for a lockout is a
platform-phase apply with authentication disabled, a setting the migration must
define and exercise. A deleted principal Secret is regenerated (a rotation), a
deleted `trino-password-file` rebuilt; an issuer outage changes nothing.

### Migration checklist

Found in review; each was to be settled before step 4. For the local root
step 4 is done (#471); the cloud-roots item below stays open for AWS and Azure.

- Cloud roots: only the local root has cert-manager and `olf-local-ca` (ADR
  0013 decision 8), and AWS and Azure expose Trino over HTTP; define the CA and
  certificate there, or a provider-neutral trust input in the contract.
- `infra/helm/values/local/trino.yaml` sets `http-server.process-forwarded=true`
  in all three roots, which the proof server omits. **Settled for the local
  root**: with the HTTP listener on, a pod can send `X-Forwarded-Proto: https`
  to port 8080 and have a password accepted over plain HTTP (k0), so the module
  disables the HTTP listener and keeps `process-forwarded` for Traefik's
  re-encrypted requests (k2, k3, t1). `olf e2e`'s perimeter check that a role
  granted `query` reaches the route (`check_role_decisions`, status below 400)
  is met by the Web UI sign-in redirect (below), which the caller
  migration adds; the route itself no longer fails at the transport.
- OpenMetadata stores the Trino password encrypted with its Fernet key. Done:
  a per-deployment `openmetadata-fernet-key` Secret, generated in-cluster and
  never in Terraform state, replaces the public upstream default.

### Per-user identity (#179)

Not foreclosed, and exercised (g1-g3): with the DEV principal also allowed to
act as `alice`, the DEV password acts as `alice` on DEV only. The client hooks
are Superset's `impersonate_user` and dbt-trino's `impersonation_user`.

## Proof

`python3 docs/testing/trino_workload_auth_proof.py --clients` starts the
pinned image in one Docker container, no cluster, `tpch` standing in for the
Polaris-backed catalogs, and removes it on exit; its file generation has a unit
self-check in `tools/olf/tests/infra/test_trino_workload_auth_proof.py`. Run on
2026-10-13 on a WSL2 Docker host (Trino 480 by the catalog digest, a throwaway
`openssl` CA): exit 0, all 21 cases passing. Requests are
`POST /v1/statement` for `SELECT count(*) FROM lakehouse_<stage>.tiny.nation`
(25 when allowed); messages are Trino's, verbatim.

| Case | Request | Result |
| --- | --- | --- |
| 0a | today: no authentication, `X-Trino-User: olf-prod-runtime`, PROD | accepted: 25 |
| 0b | HTTPS listener, no authentication, header only | accepted: 25 |
| 0c | same server, a caller sending a password | `401 Password not allowed for insecure authentication` |
| a1 | DEV credential, DEV catalog | accepted: 25 |
| a2 | DEV credential, PROD catalog | `Access Denied: Cannot access catalog lakehouse_prod` |
| b1 | DEV credential with `X-Trino-User: olf-prod-runtime`, PROD catalog | `Access Denied: Principal olf-dev-runtime cannot become user olf-prod-runtime` |
| c1 | `X-Trino-User` only, HTTPS | `HTTP 401 Unauthorized` |
| c2, c4 | `X-Trino-User` only, or a valid PROD credential, on the HTTP port | `403 ... Authentication over HTTP is not enabled` |
| c3 | right user, wrong password | `401 Access Denied: Invalid credentials` |
| d | PROD credential, PROD catalog (the unattended PROD workload) | accepted: 25 |
| h | `GET /v1/info` on the HTTP port, no credential (the chart's probe) | `200`, `"state":"ACTIVE"` |
| e1, e2 | PROD password rewritten in `password.db`, no restart | old: `401 Invalid credentials`; new: accepted: 25 |
| g1-g3 | DEV principal also allowed as `alice` (read-only on DEV) | as `alice`: DEV 25, PROD `Cannot access catalog lakehouse_prod`; as `bob`: `cannot become user bob` |
| k0 | Control: HTTP listener on with `process-forwarded=true`, a valid PROD credential on the HTTP port with a spoofed `X-Forwarded-Proto: https` | accepted: 25 (why the listener is disabled) |
| k1 | The configuration `modules/query/trino` renders: HTTPS only, CombinedPEM keystore, `internal-communication.shared-secret=${ENV:TRINO_INTERNAL_SECRET}`, `process-forwarded`, internal HTTPS truststore; DEV credential, DEV catalog | accepted: 25 |
| k2 | On that server, a connection to the HTTP port | refused |
| k3, k4 | Forwarded headers as Traefik sends them, over HTTPS: a valid PROD credential; `X-Trino-User` only | accepted: 25; `HTTP 401` |
| h2 | The chart's `health-check` inside the container, and `GET /v1/info` on HTTPS with no credential | exit 0; `200`, `"state":"ACTIVE"` |
| t1, t2 | Pinned Traefik with a file-provider transport (`rootCAs`, `serverName`) to `https://trino:8443`, `X-Forwarded-Proto: https`: DEV credential; `X-Trino-User` only | accepted: 25; `HTTP 401` |
| o1, o2 | `openmetadata` credential: both stage catalogs; claiming `olf-prod-runtime` | accepted: 25 each; `Principal openmetadata cannot become user olf-prod-runtime` |

k0-k4, h2, t1, t2 and o1-o2 were added on 2026-10-14 and run without `--clients`; the
rules the script serves now include the `openmetadata` principal and the
unconditional `tables` rule, as the module renders them.

Clients, against the same secured server, each returned 25 (dbt: `Connection
test: [OK connection ok]`): the `trino` 0.340 driver via a SQLAlchemy
`trino://<principal>:<password>@host:8443/...` URI with `http_scheme=https` and
`verify=<CA>` in the Superset image; the OpenMetadata 1.13.6 connector with
`BasicAuth` and `verify`; `dbt debug` (dbt-trino 1.10.2, `method: ldap`,
`cert: <CA>`); and Trino CLI 480 with `--server https://localhost:8443
--truststore-path <CA> --user <principal> --password`.

The Superset case runs the driver and URI its engine spec builds; the
`encrypted_extra` code path was read, not run. **Not proved:** Polaris-backed
catalogs, a kind deployment, cert-manager-issued certificates, Traefik in
front, JWT or certificate authentication on the server, any caller inside the
platform.

## Consequences

- Stage isolation in Trino is enforced, not advisory, once the migration lands.
- A stage's callers share one credential, also stored in Superset's and
  OpenMetadata's metadata databases (encrypted with their own keys). Anything
  that can read a stage's `trino-credential` can use it; containing that is
  RBAC and NetworkPolicy (#46), not this ADR.
- PBKDF2-SHA1 is weaker than bcrypt against an offline attack on `password.db`;
  accepted because the passwords are 32 random characters.

## History

New record (#26 decision gate): mechanism selected and proven; nothing built.
Credential Job and the default-off switch landed (migration step 2, first half).
HTTPS-only listener, certificate, `PASSWORD` authentication, `principals` rules
and a Traefik re-encrypting transport added behind the switch; the HTTP
listener is disabled and `process-forwarded` kept (cases k0-k4, t1, t2), and
keystore reload settled by a manual renewal check.
Step 3 for the pipeline callers: dbt run pods (profile, run env, CA mount) and
`olf e2e` reach Trino over HTTPS at the `tls` level; Superset and OpenMetadata
remain. Callers send no password until `password`, because an unauthenticated
HTTPS listener refuses one (case 0c); the consumer table's "password" rows
describe step 4, not step 3.
Step 3 for Superset: with a TLS query contract, `olf report import` writes the
database URI on the `tls_port` with `engine_params.connect_args`
`http_scheme: https` and `verify` set to the `olf-local-ca` ConfigMap, which
the superset module mounts in the Superset pods. This mounts the CA where the
driver runs instead of storing it as `server_cert`, which step 4 may still
choose. OpenMetadata ingestion was blocked: in 1.13.6 the Trino
connection has no `sslConfig` or CA-content field, only a `verify` string that
the ingestion passes to `requests` as a file path, and the k8s pipeline client
(`K8sPipelineClientConfig`) accepts no volumes, only plain-string
`extraEnvVars`. **Chosen (maintainer review):** OLF runs the Trino metadata
ingestion itself as a Kubernetes Job. Rejected: an image carrying the CA (the
CA is per cluster), an OpenMetadata upgrade (an unknown, larger change) and
`verify: false`.
Step 3 for OpenMetadata: `olf openmetadata deploy-metadata` runs the crawl once
per artifacts deploy as the Job `openmetadata-trino-ingestion` in `olf-system`:
the same pinned `ingestion-base` image, `metadata ingest -c`, the bot JWT from
a `secretKeyRef`, and with a TLS query contract the HTTPS port with `verify` on
the mounted `olf-local-ca`. The bootstrap Job no longer deploys or triggers
that pipeline. The service it registers still has no password, so `password`
stays unusable until step 4.
The hourly catalog refresh is the same pod as a CronJob per governed stage
(`openmetadata-trino-ingestion-<catalog>`), applied by `deploy-metadata` from
the same builder. The pipeline-client CronJob and the
`<service>-metadata-ingestion` pipeline registration are gone.
Plain-HTTP mode (`issuer: none`): the Trino Web UI uses
`web-ui.authentication.type=fixed` with user `olf-web` instead of its form;
`/v1` workload authentication is unchanged.

Web UI sign-in through Dex behind `tls` or `password`, over HTTPS routes with the
`github` issuer (#26): properties above; the `trino` Dex client's redirect URI
is exact.
Step 4, clients: the query contract carries `credential_secret_name`
(`trino-credential`) only while `password` is on. With it, dbt run pods get
`method: ldap` and `OPENLAKEFORGE_DBT_TRINO_PASSWORD` from a `secretKeyRef` on
the stage Secret (the Dagster module and the activation both set them);
`olf report import` reads the stage Secret and sends it in the `passwords`
form field of the Superset import, so it is in neither the zip nor the URI; the
OpenMetadata crawl Job reads `trino-credential-openmetadata` from `olf-system`
into `authType.password`; `olf e2e` and `olf query` read
`trino-credential-olf-<stage>-runtime` and pass it to the in-pod CLI on stdin,
which exports `TRINO_PASSWORD` only when the pod mounts `password.db`. The
service the OpenMetadata bootstrap registers still carries no password. The
Fernet-key prerequisite is met: an in-cluster Job creates a per-deployment
`openmetadata-fernet-key` Secret, which OpenMetadata reads by `secretKeyRef`.
Step 4, default and gate: the local root defaults `trino_workload_authentication`
to `password` when `spec.access` is present. `olf e2e` (`_isolation.py`), with
Trino requiring a password, runs the DEV principal with `--session-user
olf-prod-runtime` (expects `cannot become user`), the PROD user name with no
password and the DEV user name with a wrong one (expect 401), beside the
existing direct cross-stage query (`Access Denied`) and the pipeline runs that
authenticate with their own credentials as the positive controls.
