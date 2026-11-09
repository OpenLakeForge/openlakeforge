# ADR 0016: Trino workload authentication — none; Polaris decides

## Status

**Decided and built on the local root (#983).** Trino has no authentication
of its own, no access rules and no HTTPS listener. This record once chose
password-file authentication over HTTPS (#26, #471); ADR 0019 section 9
(#960) removed its causes, and it was rewritten in place to say what binds now.

## Context

Trino authenticated workloads because one coordinator held every stage's
Polaris credential and a static S3 key, so it had to tell its callers apart
itself: a password per stage principal, `principals` rules binding it to one
stage user, and an HTTPS listener, because Trino authenticates only a request
it considers secure (#932). ADR 0019 section 9 removed both causes on the
local root: one coordinator per stage (#976) makes the stage boundary a
network boundary, and the keyless catalogs (#982) leave Trino no credential
with a grant and no storage key, so Polaris decides every table access from
the token each caller sends.

## Decision

1. **No workload authentication.** The local root's coordinators serve plain
   HTTP on 8080 only. Every caller sends its own Polaris token as the `token`
   extra credential (dbt and Gold maintenance, `olf infra query` and `olf
   e2e`, `olf query` and the platform API, Superset per user, the OpenMetadata
   crawl). The user Trino records is a claim: it labels the query, it never
   changes what Polaris allows (#964).
2. **No access rules on a keyless catalog.** A Glue catalog is not keyless, so
   AWS's shared coordinator keeps the claim-based `catalog_access_rules` until
   AWS moves to Polaris (#681); nothing else renders them.
3. **The Web UI signs in as a fixed user** (`web-ui.authentication.type=fixed`)
   behind the perimeter, which keeps its data-engineer floor. The UI shows
   every query; what a query reads is still Polaris's decision.
4. **Stage isolation** is the Polaris grant of the presented token plus the
   NetworkPolicy of ADR 0017: a stage's coordinator is reachable from its own
   namespace, Traefik, OpenMetadata and the platform API only.

## Consequences

| Accepted cost | Why |
| --- | --- |
| The user in Trino's query log is a claim | Access is decided by the token alone; Polaris logs the principal `github`, not the person (ADR 0019 section 9) |
| Anyone who passes the perimeter's Web UI floor sees every query in the UI | The UI shows SQL and timings, not rows; reading data still needs a token Polaris accepts |
| AWS and Azure keep one shared coordinator | Their cloud milestone moves them; AWS keeps claim-based rules for Glue until then |

## History

Rewritten in place (#983): Trino authentication, its `principals` and people
rules, the credential Job (`trino-credential-*`, `trino-password-file`,
`trino-internal-secret`), the HTTPS listener with its certificate and
Traefik's re-encrypting transport, and the Web UI's OAuth2 sign-in are
removed from the local root. The record below describes what was built
before.


One coordinator per stage on the local root (#976): the certificate, the credential Job, the password file and
the internal secret are per stage namespace, and a coordinator knows only its own stage's principal and
`openmetadata`. Traefik's CA trust Secret stays in `olf-system`, where it can be mounted.

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
per artifacts deploy as the Job `openmetadata-trino-ingestion` in the stage namespace (#976), beside the coordinator and the `openmetadata` principal Secret:
the same pinned `ingestion-base` image, `metadata ingest -c`, the bot JWT from
a `secretKeyRef`, and with a TLS query contract the HTTPS port with `verify` on
the mounted `olf-local-ca`. The OpenMetadata bootstrap no longer deploys or
triggers that pipeline. The service it registers still has no password, so
`password` stays unusable until step 4.
The hourly catalog refresh is the same pod as a CronJob per governed stage
(`openmetadata-trino-ingestion-<catalog>`), applied by `deploy-metadata` from
the same builder. The pipeline-client CronJob and the
`<service>-metadata-ingestion` pipeline registration are gone.
Plain-HTTP mode (`tls: none`): the Trino Web UI uses
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
OpenMetadata crawl Job reads `trino-credential-openmetadata` from the coordinator's namespace
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

`JWT` beside `PASSWORD` for people's Dex tokens, the people catalogs and their
rules, and the platform S3 key under `OPENLAKEFORGE_S3_*` (ADR 0019, build
step 5).

Per-user identity: Trino fetches the JWT keys for people's tokens from Dex's
Service rather than through the ingress. The first fresh deployment with people's
access never finished: Traefik waits for Trino's route, and Trino crash-looped on
an unresolvable `auth.<base_domain>` while fetching the keys at startup.
An attempt (#932) to serve `PASSWORD` on the HTTP listener with
`http-server.authentication.allow-insecure-over-http=true` was reverted: Trino
then skips its authenticators on a request it does not consider secure, so a
request with no credential was accepted as the `X-Trino-User` it named and one
with a password was refused with `401 Password not allowed for insecure
authentication` (observed in the coordinator of the full local e2e). Password
authentication holds only on the HTTPS listener, or on a request Traefik marks
`X-Forwarded-Proto: https`; the HTTPS-only listener above is therefore the
decision, not a preference.

#960: scheduled for retirement. ADR 0019 section 9 makes Polaris the authority
for workloads as well as people, with one Trino per stage and no credential in
Trino; this record's mechanism stays binding until #967 removes it. Spikes
#961 and #964 confirmed the keyless engine, so no fallback is kept.
