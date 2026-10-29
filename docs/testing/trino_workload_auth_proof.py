#!/usr/bin/env python3
"""Reproduction of the Trino workload-authentication proof (ADR 0016, #26).

Runs the pinned Trino image in one throwaway Docker container (no cluster) with password-file authentication over
HTTPS and file-based access control binding each credential to one principal and stage catalog, sends real requests
and prints PASS/FAIL per case. `tpch` stands in for the Polaris-backed catalogs. Needs docker and openssl.

    python3 docs/testing/trino_workload_auth_proof.py [--clients]

--clients adds the repository's client libraries (needs the local project-code and superset images and the pinned
OpenMetadata ingestion image). The container is removed on exit.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
from base64 import b64encode
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
NAME = "olf-trino-auth-proof"
NETWORK, TRAEFIK_NAME, TRAEFIK_PORT = "olf-trino-auth-proof-net", "olf-trino-auth-proof-traefik", 18000
HTTPS_PORT, HTTP_PORT = 18443, 18080
STAGES = ("dev", "prod")
# OpenMetadata crawls every stage catalog read-only (modules/query/trino).
PRINCIPALS = ("olf-dev-runtime", "olf-prod-runtime", "openmetadata")
ITERATIONS = 10_000
COUNT = "SELECT count(*) FROM {catalog}.tiny.nation"


def pinned_image(name: str = "trino") -> str:
    match = re.search(rf"^\s+{name}: (\S+@sha256:\S+)$", (REPO / "release/component-catalog.yaml").read_text(), re.M)
    if not match:
        raise SystemExit(f"no pinned {name} image in release/component-catalog.yaml")
    return match.group(1)


def principal(stage: str) -> str:
    return f"olf-{stage}-runtime"


def password_line(user: str, password: str, salt: bytes | None = None) -> str:
    """One password.db line in Trino's PBKDF2 format: user:iterations:salt:hash (hex)."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha1", password.encode(), salt, ITERATIONS, dklen=32)
    return f"{user}:{ITERATIONS}:{salt.hex()}:{digest.hex()}"


def access_rules(*, bind_principals: bool = True, acts_as: tuple[str, str] | None = None) -> dict:
    """Catalog rules as modules/query/trino renders them, plus principal binding.

    `principals` makes the verified credential, not a claimed `X-Trino-User`, decide the user. `acts_as=(stage,
    user)` also lets that stage's principal act as `user`, read-only on its catalog: the per-user shape of #179.
    """
    catalogs = [{"user": principal(s), "catalog": f"lakehouse_{s}", "allow": "all"} for s in STAGES]
    catalogs += [{"user": "openmetadata", "catalog": f"lakehouse_{s}", "allow": "read-only"} for s in STAGES]
    principals = [{"principal": p, "user": p, "allow": True} for p in PRINCIPALS]
    if acts_as:
        stage, user = acts_as
        catalogs.append({"user": user, "catalog": f"lakehouse_{stage}", "allow": "read-only"})
        principals[STAGES.index(stage)]["user"] = f"{principal(stage)}|{user}"
    catalogs += [{"catalog": "system", "allow": "read-only"}, {"catalog": ".*", "allow": "none"}]
    # The unconditional table rule is what the module renders for Polaris-backed catalogs.
    tables = [{"privileges": ["SELECT", "INSERT", "DELETE", "UPDATE", "OWNERSHIP"]}]
    rules: dict = {"catalogs": catalogs, "tables": tables}
    if bind_principals:
        rules["principals"] = [*principals, {"principal": ".*", "user": ".*", "allow": False}]
    return rules


class Server:
    """The throwaway Trino container and the files it mounts."""

    def __init__(self, workdir: Path, passwords: dict[str, str]) -> None:
        self.dir, self.etc, self.pub, self.passwords = workdir, workdir / "etc", workdir / "tls" / "pub", passwords

    def make_tls(self) -> None:
        tls = self.dir / "tls"
        self.pub.mkdir(parents=True)
        key = ["-newkey", "rsa:2048", "-nodes"]

        def openssl(*args: str) -> None:
            subprocess.run(["openssl", *args], cwd=tls, check=True, capture_output=True)

        openssl("req", "-x509", *key, "-keyout", "ca.key", "-out", "pub/ca.crt", "-days", "2", "-subj", "/CN=proof-ca")
        openssl("req", *key, "-keyout", "srv.key", "-out", "srv.csr", "-subj", "/CN=trino")
        (tls / "ext.cnf").write_text("subjectAltName=DNS:localhost,DNS:trino\n")
        ca = ["-CA", "pub/ca.crt", "-CAkey", "ca.key", "-CAcreateserial"]
        openssl("x509", "-req", "-in", "srv.csr", *ca, "-out", "srv.crt", "-days", "2", "-extfile", "ext.cnf")
        openssl("pkey", "-in", "srv.key", "-out", "srv.pk8")
        # Only `pub` reaches containers: the CA and server keys stay out of them.
        (self.pub / "server.pem").write_text((tls / "srv.pk8").read_text() + (tls / "srv.crt").read_text())
        # cert-manager's CombinedPEM output: the PKCS8 key, then the certificate chain (leaf, CA).
        chain = (tls / "srv.crt").read_text() + (self.pub / "ca.crt").read_text()
        (self.pub / "tls-combined.pem").write_text((tls / "srv.pk8").read_text() + chain)

    def write_files(self, *, secured: bool, deployed: bool = False, http: bool = True) -> None:
        """`deployed` is the shape modules/query/trino renders: the CombinedPEM keystore, the shared secret from the
        environment, `process-forwarded` and, unless `http`, no HTTP listener (the control keeps it)."""
        (self.etc / "catalog").mkdir(parents=True, exist_ok=True)
        config = "coordinator=true\nnode-scheduler.include-coordinator=true\nhttp-server.http.port=8080\n"
        config += "discovery.uri=http://localhost:8080\nhttp-server.https.enabled=true\nhttp-server.https.port=8443\n"
        config += f"http-server.https.keystore.path=/etc/trino/tls/{'tls-combined' if deployed else 'server'}.pem\n"
        config += "http-server.process-forwarded=true\n" if deployed else ""
        if deployed and not http:
            # A later line wins over the chart's own discovery.uri, which is written first. A node reaches itself
            # at a name in the certificate, trusting the CA, because HTTPS-only internal traffic is verified.
            config += "http-server.http.enabled=false\ndiscovery.uri=https://localhost:8443\n"
            config += "internal-communication.https.required=true\n"
            config += "internal-communication.https.truststore.path=/etc/trino/tls/ca.crt\n"
        if secured:
            config += "http-server.authentication.type=PASSWORD\n"
            config += "internal-communication.shared-secret=" + (
                "${ENV:TRINO_INTERNAL_SECRET}\n" if deployed else f"{secrets.token_hex(16)}\n"
            )
            (self.etc / "password-authenticator.properties").write_text(
                "password-authenticator.name=file\nfile.password-file=/etc/trino/password.db\nfile.refresh-period=1s\n"
            )
            self.write_passwords()
        (self.etc / "config.properties").write_text(config)
        internal = "node.internal-address=localhost\n" if deployed and not http else ""
        (self.etc / "node.properties").write_text(f"node.environment=proof\nnode.data-dir=/data/trino\n{internal}")
        (self.etc / "access-control.properties").write_text(
            "access-control.name=file\nsecurity.config-file=/etc/trino/rules.json\nsecurity.refresh-period=1s\n"
        )
        self.write_rules(access_rules(bind_principals=secured))
        for stage in STAGES:
            (self.etc / "catalog" / f"lakehouse_{stage}.properties").write_text("connector.name=tpch\n")

    def write_passwords(self) -> None:
        # Written in place: the container bind-mounts the file by inode.
        (self.etc / "password.db").write_text("".join(password_line(u, p) + "\n" for u, p in self.passwords.items()))

    def write_rules(self, rules: dict) -> None:
        (self.etc / "rules.json").write_text(json.dumps(rules, indent=1))

    def start(self) -> None:
        files = ("config", "node", "password-authenticator", "access-control")
        names = [f"{f}.properties" for f in files] + ["password.db", "rules.json", "catalog"]
        mounts = [(self.etc / name, f"/etc/trino/{name}") for name in names]
        subprocess.run(["docker", "network", "create", NETWORK], capture_output=True)
        cmd = ["docker", "run", "-d", "--name", NAME, "--memory", "1500m", "-v", f"{self.pub}:/etc/trino/tls:ro"]
        cmd += ["--network", NETWORK, "--network-alias", "trino"]
        cmd += ["-e", f"TRINO_INTERNAL_SECRET={secrets.token_hex(16)}"]
        cmd += ["-p", f"127.0.0.1:{HTTPS_PORT}:8443", "-p", f"127.0.0.1:{HTTP_PORT}:8080"]
        for source, target in mounts:
            cmd += ["-v", f"{source}:{target}:ro"] if source.exists() else []
        started = subprocess.run([*cmd, pinned_image()], capture_output=True, text=True)
        if started.returncode:
            raise SystemExit(f"docker run failed: {started.stderr.strip()}")

    @staticmethod
    def stop() -> None:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)

    @staticmethod
    def cleanup() -> None:
        subprocess.run(["docker", "rm", "-f", NAME, TRAEFIK_NAME], capture_output=True)
        subprocess.run(["docker", "network", "rm", NETWORK], capture_output=True)

    def statement(self, sql: str, *, creds: tuple[str, str] | None = None, header_user: str | None = None,
                  plain_http: bool = False, headers: dict[str, str] | None = None,
                  via_traefik: bool = False) -> tuple[bool, str]:
        """POST a statement the way every Trino client does: (succeeded, first value or error text)."""
        headers = {"X-Trino-Source": "olf-proof", **(headers or {})}
        if header_user:
            headers["X-Trino-User"] = header_user
        if creds:
            headers["Authorization"] = "Basic " + b64encode(":".join(creds).encode()).decode()
        if via_traefik:
            conn: http.client.HTTPConnection = http.client.HTTPConnection("localhost", TRAEFIK_PORT, timeout=60)
            headers["Host"] = "trino.test"
        elif plain_http:
            conn = http.client.HTTPConnection("localhost", HTTP_PORT, timeout=60)
        else:
            context = ssl.create_default_context(cafile=str(self.pub / "ca.crt"))
            conn = http.client.HTTPSConnection("localhost", HTTPS_PORT, timeout=60, context=context)
        conn.request("POST", "/v1/statement", sql, headers)
        while True:
            response = conn.getresponse()
            raw = response.read().decode()
            if response.status != 200:
                return False, f"HTTP {response.status} {' '.join(raw.split())[:100]}"
            body = json.loads(raw)
            if "error" in body:
                return False, body["error"]["message"]
            if "data" in body:
                return True, str(body["data"][0][0])
            if "nextUri" not in body:
                return True, ""
            uri = urlsplit(body["nextUri"])
            conn.request("GET", f"{uri.path}?{uri.query}" if uri.query else uri.path, headers=headers)

    def wait_ready(self, **kwargs: object) -> None:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                if self.statement("SELECT 1", **kwargs)[0]:  # type: ignore[arg-type]
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(2)
        raise SystemExit("trino did not become ready: " + subprocess.run(["docker", "logs", "--tail", "20", NAME],
                                                                          capture_output=True, text=True).stderr)


class Report:
    failed = 0

    def case(self, label: str, expect_ok: bool, result: tuple[bool, str], needle: str) -> None:
        ok, detail = result
        good = ok == expect_ok and needle in detail
        self.failed += not good
        verdict = "accepted" if ok else "rejected"
        print(f"{'PASS' if good else 'FAIL'}  {label}\n        -> {verdict}: {detail}", flush=True)


def baseline_cases(server: Server, report: Report) -> None:
    """The configuration deployed today (no authentication, the header is the identity) and its migration limits."""
    sql, prod = COUNT.format(catalog="lakehouse_prod"), principal("prod")
    result = server.statement(sql, header_user=prod, plain_http=True)
    report.case("0a BEFORE, no authentication: X-Trino-User PROD alone, plain HTTP", True, result, "25")
    # TLS can be introduced first: callers can move to HTTPS while still sending only the header.
    report.case("0b BEFORE, HTTPS listener without authentication: header only", True,
                server.statement(sql, header_user=prod), "25")
    # A caller sending a password is refused until authentication is enabled: the cutover is one step.
    result = server.statement(sql, creds=(prod, "x"), header_user=prod)
    report.case("0c BEFORE: a caller already sending a password, HTTPS", False, result, "insecure authentication")


def server_cases(server: Server, report: Report) -> None:
    dev, prod = ((principal(s), server.passwords[principal(s)]) for s in STAGES)
    prod_user = prod[0]

    def ask(label: str, ok: bool, needle: str, catalog: str, **kwargs: object) -> None:
        report.case(label, ok, server.statement(COUNT.format(catalog=catalog), **kwargs), needle)  # type: ignore[arg-type]

    ask("a1 DEV credential reads the DEV catalog", True, "25", "lakehouse_dev", creds=dev)
    ask("a2 DEV credential reads the PROD catalog", False, "Cannot access catalog", "lakehouse_prod", creds=dev)
    ask("b1 DEV credential claims PROD (X-Trino-User)", False, "cannot become user", "lakehouse_prod", creds=dev,
        header_user=prod_user)
    ask("c1 X-Trino-User only, HTTPS", False, "HTTP 401", "lakehouse_prod", header_user=prod_user)
    ask("c2 X-Trino-User only, plain HTTP port", False, "HTTP 403", "lakehouse_prod", header_user=prod_user,
        plain_http=True)
    ask("c3 right user, wrong password", False, "HTTP 401", "lakehouse_prod", creds=(prod_user, "wrong"))
    ask("c4 valid credential over plain HTTP", False, "HTTP 403", "lakehouse_prod", creds=prod, plain_http=True)
    ask("d  PROD workload reads the PROD catalog", True, "25", "lakehouse_prod", creds=prod)
    probe = http.client.HTTPConnection("localhost", HTTP_PORT, timeout=30)  # what the chart's probes do
    probe.request("GET", "/v1/info")
    answer = probe.getresponse()
    report.case("h  chart probe: GET /v1/info on the HTTP port, no credential", True,
                (answer.status == 200, f"HTTP {answer.status} {answer.read().decode()[:80]}"), "ACTIVE")

    old = server.passwords[prod_user]
    server.passwords[prod_user] = secrets.token_urlsafe(24)
    server.write_passwords()
    time.sleep(3)  # file.refresh-period is 1s; no restart
    ask("e1 after rotation: old PROD password", False, "HTTP 401", "lakehouse_prod", creds=(prod_user, old))
    ask("e2 after rotation: new PROD password", True, "25", "lakehouse_prod",
        creds=(prod_user, server.passwords[prod_user]))

    server.write_rules(access_rules(acts_as=("dev", "alice")))
    time.sleep(3)  # security.refresh-period is 1s
    ask("g1 DEV credential acting as alice, DEV catalog", True, "25", "lakehouse_dev", creds=dev, header_user="alice")
    ask("g2 DEV credential acting as alice, PROD catalog", False, "Cannot access catalog", "lakehouse_prod",
        creds=dev, header_user="alice")
    ask("g3 DEV credential acting as bob", False, "cannot become user", "lakehouse_dev", creds=dev, header_user="bob")


def deployed_cases(server: Server, report: Report) -> None:
    """The server as modules/query/trino configures it (HTTPS only, process-forwarded on), restarted from above."""
    sql = COUNT.format
    dev, prod, om = ((u, server.passwords[u]) for u in PRINCIPALS)
    spoof = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "trino.test"}

    def restart(*, http: bool) -> None:
        server.stop()
        server.write_files(secured=True, deployed=True, http=http)
        server.start()
        server.wait_ready(creds=dev)

    restart(http=True)
    report.case("k0 CONTROL, HTTP listener on with process-forwarded: a spoofed X-Forwarded-Proto gets a password "
                "accepted over plain HTTP", True,
                server.statement(sql(catalog="lakehouse_prod"), creds=prod, plain_http=True, headers=spoof), "25")
    restart(http=False)
    report.case("k1 deployed shape (HTTPS only, CombinedPEM keystore, secret from the environment): DEV reads DEV",
                True, server.statement(sql(catalog="lakehouse_dev"), creds=dev), "25")
    try:
        http.client.HTTPConnection("localhost", HTTP_PORT, timeout=5).request("GET", "/v1/info")
        refused = (False, "the HTTP port answered")
    except OSError as error:
        refused = (True, type(error).__name__)
    report.case("k2 no HTTP listener: the HTTP port refuses the connection", True, refused, "")
    report.case("k3 forwarded headers as Traefik sends them, valid PROD credential, HTTPS", True,
                server.statement(sql(catalog="lakehouse_prod"), creds=prod, headers=spoof), "25")
    report.case("k4 forwarded headers, X-Trino-User only, HTTPS", False,
                server.statement(sql(catalog="lakehouse_prod"), header_user=prod[0], headers=spoof), "HTTP 401")
    # The chart's readiness and startup probes run the image's health-check, which uses HTTPS once HTTP is disabled.
    check = subprocess.run(["docker", "exec", NAME, "/usr/lib/trino/bin/health-check"], capture_output=True, text=True)
    context = ssl.create_default_context(cafile=str(server.pub / "ca.crt"))
    probe = http.client.HTTPSConnection("localhost", HTTPS_PORT, timeout=30, context=context)
    probe.request("GET", "/v1/info")
    answer = probe.getresponse()
    body = answer.read().decode()
    report.case("h2 chart probes: health-check exits 0 and GET /v1/info on HTTPS needs no credential", True,
                (check.returncode == 0 and answer.status == 200, f"exit {check.returncode}, HTTP {answer.status} "
                                                                   f"{body[:60]}"), "ACTIVE")
    for stage in STAGES:
        report.case(f"o1 OpenMetadata credential reads the {stage.upper()} catalog", True,
                    server.statement(sql(catalog=f"lakehouse_{stage}"), creds=om), "25")
    report.case("o2 OpenMetadata credential claims olf-prod-runtime", False,
                server.statement(sql(catalog="lakehouse_prod"), creds=om, header_user=prod[0]), "cannot become user")
    traefik_cases(server, report)


def traefik_cases(server: Server, report: Report) -> None:
    """Traefik re-encrypting to 8443 with the CA as its transport trust, as the file provider configures it."""
    config = server.dir / "traefik.yml"
    config.write_text(
        "http:\n  routers:\n    trino: {rule: Host(`trino.test`), service: trino, entryPoints: [web]}\n"
        "  services:\n    trino:\n      loadBalancer:\n        serversTransport: trino\n"
        "        servers: [{url: 'https://trino:8443'}]\n"
        "  serversTransports:\n    trino: {rootCAs: [/ca/ca.crt], serverName: trino}\n"
    )
    # Plain HTTP into Traefik stands in for the websecure entrypoint; trusting the forwarded headers lets the
    # request carry the X-Forwarded-Proto: https that websecure would set.
    cmd = ["docker", "run", "-d", "--name", TRAEFIK_NAME, "--network", NETWORK, "-p", f"127.0.0.1:{TRAEFIK_PORT}:8000"]
    cmd += ["-v", f"{config}:/etc/traefik/dynamic.yml:ro", "-v", f"{server.pub}:/ca:ro", pinned_image("traefik")]
    cmd += ["--entrypoints.web.address=:8000", "--entrypoints.web.forwardedHeaders.insecure=true"]
    cmd += ["--providers.file.filename=/etc/traefik/dynamic.yml"]
    started = subprocess.run(cmd, capture_output=True, text=True)
    if started.returncode:
        raise SystemExit(f"docker run traefik failed: {started.stderr.strip()}")
    sql, dev = COUNT.format(catalog="lakehouse_dev"), (principal("dev"), server.passwords[principal("dev")])
    headers = {"X-Forwarded-Proto": "https"}
    result: tuple[bool, str] = (False, "traefik did not answer")
    for _ in range(15):
        try:
            result = server.statement(sql, creds=dev, headers=headers, via_traefik=True)
            if result[0]:
                break
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(2)
    report.case("t1 through Traefik to the HTTPS port, verified against the CA: DEV credential reads DEV", True,
                result, "25")
    report.case("t2 through Traefik, X-Trino-User only", False,
                server.statement(sql, header_user=dev[0], headers=headers, via_traefik=True), "HTTP 401")


def client_cases(server: Server, report: Report) -> None:
    """The repository's real client libraries and CLI, at the versions it ships."""
    user = principal("dev")
    password = server.passwords[user]
    sql = COUNT.format(catalog="lakehouse_dev")
    project_code, superset = "ghcr.io/openlakeforge/openlakeforge/project-code:local", "ghcr.io/openlakeforge/superset:local"

    def run(label: str, image: str, entrypoint: str, args: list[str], expect: str = "25", mount: str = "") -> None:
        cmd = ["docker", "run", "--rm", "--network", f"container:{NAME}", "-e", f"U={user}", "-e", f"P={password}"]
        cmd += ["-v", f"{server.pub}:/tls:ro"] + (["-v", mount] if mount else [])
        result = subprocess.run([*cmd, "--entrypoint", entrypoint, image, *args], capture_output=True, text=True)
        output = re.sub(r"\x1b\[[0-9;]*m", "", (result.stdout + result.stderr).strip())
        # `dbt debug` exits non-zero in this image for an unrelated reason (no VCS binary); its connection line decides.
        passed = expect in output and (result.returncode == 0 or entrypoint == "dbt")
        shown = next((line.strip() for line in output.splitlines() if expect in line), output[-160:])
        report.case(label, True, (passed, shown), "")

    sqlalchemy = (
        "import os,sqlalchemy as s;"
        "e=s.create_engine('trino://%s:%s@localhost:8443/lakehouse_dev'%(os.environ['U'],os.environ['P']),"
        "connect_args={'http_scheme':'https','verify':'/tls/ca.crt'});"
        f"print(e.connect().execute(s.text({sql!r})).scalar())"
    )
    schema = "metadata.generated.schema.entity.services.connections.database"
    openmetadata = (
        f"import os,sqlalchemy as s;from {schema}.common.basicAuth import BasicAuth;"
        f"from {schema}.trinoConnection import TrinoConnection as C;"
        "from metadata.ingestion.source.database.trino.connection import TrinoConnection as T;"
        "c=C(hostPort='localhost:8443',username=os.environ['U'],authType=BasicAuth(password=os.environ['P']),"
        "catalog='lakehouse_dev',verify='/tls/ca.crt');"
        f"print(T(c).client.connect().execute(s.text({sql!r})).scalar())"
    )
    run("client: trino driver + SQLAlchemy URI (Superset image)", superset, "/app/.venv/bin/python", ["-c", sqlalchemy])
    ingestion = pinned_image("openmetadata_ingestion")
    om_args = ["-W", "ignore", "-c", openmetadata]
    run("client: OpenMetadata Trino connector, BasicAuth + verify", ingestion, "python", om_args)

    dbt = server.dir / "dbt"
    dbt.mkdir()
    (dbt / "dbt_project.yml").write_text("name: proof\nversion: '1.0.0'\nprofile: proof\n")
    (dbt / "profiles.yml").write_text(
        "proof:\n  target: t\n  outputs:\n    t:\n      type: trino\n      method: ldap\n"
        "      host: localhost\n      port: 8443\n      user: \"{{ env_var('U') }}\"\n"
        "      password: \"{{ env_var('P') }}\"\n      database: lakehouse_dev\n      schema: tiny\n"
        "      cert: /tls/ca.crt\n"
    )
    args = ["debug", "--profiles-dir", "/dbt", "--project-dir", "/dbt", "--log-path", "/tmp/l"]
    label = "client: dbt-trino 1.10.2, method ldap (dbt debug)"
    run(label, project_code, "dbt", args, "OK connection ok", f"{dbt}:/dbt:ro")

    cli = ["docker", "exec", "-e", f"TRINO_PASSWORD={password}", NAME, "trino", "--server", "https://localhost:8443"]
    cli += ["--truststore-path", "/etc/trino/tls/ca.crt", "--user", user, "--password", "--execute", sql]
    result = subprocess.run(cli, capture_output=True, text=True)
    label = "client: Trino CLI in the coordinator container (olf e2e path)"
    report.case(label, True, (result.returncode == 0, (result.stdout + result.stderr).strip()), "25")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clients", action="store_true", help="also run the repository's client libraries")
    args = parser.parse_args()
    print(f"image: {pinned_image()}")
    report = Report()
    Server.cleanup()
    # Under $HOME, not /tmp: a snap-packaged Docker daemon cannot bind-mount /tmp.
    with tempfile.TemporaryDirectory(prefix="olf-trino-proof-", dir=Path.home()) as tmp:
        passwords = {user: secrets.token_urlsafe(24) for user in PRINCIPALS}
        server = Server(Path(tmp), passwords)
        try:
            server.make_tls()
            server.write_files(secured=False)
            server.start()
            server.wait_ready(header_user="ready", plain_http=True)
            baseline_cases(server, report)
            server.stop()
            server.write_files(secured=True)
            server.start()
            server.wait_ready(creds=(principal("dev"), passwords[principal("dev")]))
            server_cases(server, report)
            if args.clients:
                client_cases(server, report)
            deployed_cases(server, report)
        finally:
            server.cleanup()
    print(f"\n{'FAILED: ' + str(report.failed) if report.failed else 'all cases passed'}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
