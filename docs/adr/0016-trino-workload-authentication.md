# ADR 0016: Trino workload authentication — password file over HTTPS

## Status

Decided and proven in isolation; **not built**: the #26 decision gate changes
no caller, chart value or module. What binds today is the selection, the
principal mapping and the migration order. Until the migration lands Trino runs
unauthenticated and its row in `docs/technical-debt.md` stays open.

## Context

One Trino 480 coordinator (chart 1.42.2) in `olf-system` serves every stage's
catalog (`lakehouse_<stage>`). Access is decided by file-based rules
(`modules/query/trino`, `catalog_access_rules`) keyed on the **user name**,
which is whatever the client sends in `X-Trino-User`; Trino accepts it
unauthenticated, so any pod that reaches the Service can claim
`olf-prod-runtime` (proved below, case 0a). Superset, dbt, OpenMetadata and
`olf e2e` are the callers ([`identity-callers.md`](../architecture/identity-callers.md)).
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
| JWT | Rejected. Trino has one issuer and one key set and accepts any trusted key for any `sub`, so a per-stage signing key does not stop that key minting `olf-prod-runtime`; the binding lives in the minting job and its key is the crown jewel. Superset, dbt and OpenMetadata cannot refresh a token, so they would hold long-lived bearer tokens that cannot be revoked one by one. Short Keycloak tokens would make every dashboard query depend on the issuer being up |
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
HTTPS 401, any query on the HTTP port 403, wrong password 401, valid password
with an `X-Trino-User` outside the principal's list "Principal X cannot become
user Y" before any catalog rule, and only then the catalog and table rules for
the resulting user. Callers of one stage share its credential (the
shared-service-account limitation in `identity-callers.md`).

### Secrets

Created by an in-cluster Job owned by `modules/query/trino`, never by Terraform
([`identity-credentials.md`](../architecture/identity-credentials.md),
`modules/identity/keycloak/credentials.tf`); the contract fields are the migration's.

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
| Keystore | Trino reads a PEM file of key then certificate (proved with an openssl-built file). That cert-manager's `CombinedPEM` output supplies it, and whether Trino reloads it on renewal, are **to verify** |
| Workload trust | callers use `trino.olf-system.svc.cluster.local:8443` and mount ConfigMap `olf-local-ca` (`ca.crt`, already in `olf-system` and every stage namespace, ADR 0013 decision 8) |
| HTTP port | stays, because the chart's probes use it. Proved: `GET /v1/info` needs no credential (h) and a query or password on it is refused (c2, c4). Callers should not be offered it (#46) |
| Ingress hop | Traefik reaches Trino over HTTP with `X-Forwarded-*` today. Whether Trino then treats that as secure was **not tested**; the human OAuth2 change decides it, with `web-ui.authentication.type` keeping the UI login separate from workload passwords |

### Consumer configuration

| Caller | Change in the migration |
| --- | --- |
| Superset (per stage) | `olf report import` writes `trino://<principal>@trino.olf-system.svc.cluster.local:8443/lakehouse_<stage>`, the CA as `server_cert`, the password in `encrypted_extra` (`auth_method: basic`) read from `trino-credential` through RBAC. The connection uuid is unchanged |
| dbt in Dagster run pods | `libs/dbt/profiles/*.yml`: `method: ldap`, `password: env_var('OPENLAKEFORGE_DBT_TRINO_PASSWORD')` from a `secretKeyRef` on `trino-credential`, `cert:` the mounted CA, FQDN host, port 8443. `impersonation_user` is there for #179 |
| OpenMetadata | the bootstrap Job creates the service with `authType: {password}` and `verify:` the CA path; the ingestion pods must mount the CA (**to verify**) |
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
| 3. Callers move to HTTPS and the CA, still sending only the user name (accepted, case 0b) | platform, artifacts | none; proves trust from every caller |
| 4. Cutover: enable `PASSWORD` and `principals`, ship the passwords in the same deploy | platform, then artifacts | Trino already restarts (`Recreate`); callers fail until their password lands, in-flight runs retry |
| 5. `olf e2e` runs the isolation cases, an unattended pipeline and a dashboard query | e2e | none |

Recovery is the inverse deploy (authentication off, callers back to the user
name); a server-only toggle does not work, because an unauthenticated server
refuses a caller that sends a password. Break-glass for a lockout is a
platform-phase apply with authentication disabled, a setting the migration must
define and exercise. A deleted principal Secret is regenerated (a rotation), a
deleted `trino-password-file` rebuilt; an issuer outage changes nothing.

### Per-user identity (#179)

Not foreclosed, and exercised (g1-g3): with the DEV principal also allowed to
act as `alice`, the DEV password acts as `alice` on DEV only. The client hooks
are Superset's `impersonate_user` and dbt-trino's `impersonation_user`.

## Proof

`python3 docs/testing/trino_workload_auth_proof.py --clients` starts the
pinned image in one Docker container, no cluster, `tpch` standing in for the
Polaris-backed catalogs, and removes it on exit; its file generation has a unit
self-check in `tools/olf/tests/test_trino_workload_auth_proof.py`. Run on
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
