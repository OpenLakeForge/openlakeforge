"""Floe EnvironmentProfile for the stage this project-code container runs in.

The Floe project under `lakehouse_code/silver/` says *what* to validate; this
profile says *where*: storages, the Iceberg catalog and the lineage endpoint,
all read from the provider-contract values the activation release puts on the
pod. It is rendered when the code location loads, in the code server and in
every run pod alike, so one image serves every stage and nothing stage-bound
is baked into it or published beside it.

Floe runs one Kubernetes Job per domain (`job_scope: domain`) on this same
image. dagster-floe runs it as `floe run -c <project> -p <profile>`, so the
profile path must exist in the Job too: its entrypoint (`libs.floe_job`)
renders the same profile there from the contract values forwarded below.

Kept free of Dagster/Floe imports so `tools/olf` can unit test it.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

# `_domain.yml` sinks name this catalog; it is part of the project contract,
# not a stage binding, so the stage's own catalog name never reaches it.
CATALOG_NAME = "iceberg_catalog"


# Every non-secret value `render_profile` reads, handed to the Job so the
# entrypoint renders the stage's profile from the same inputs.
_JOB_ENV = (
    "AWS_ALLOW_HTTP",
    "AWS_DEFAULT_REGION",
    "AWS_ENDPOINT_URL_S3",
    "AWS_REGION",
    "AWS_S3_FORCE_PATH_STYLE",
    "OPENLAKEFORGE_ARTIFACT_BASE_URI",
    "OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID_KEY",
    "OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET_KEY",
    "OPENLAKEFORGE_CATALOG_GLUE_REGION",
    "OPENLAKEFORGE_CATALOG_GLUE_WAREHOUSE_PREFIX",
    "OPENLAKEFORGE_CATALOG_NAME",
    "OPENLAKEFORGE_CATALOG_OAUTH_SCOPE",
    "OPENLAKEFORGE_CATALOG_PROVIDER",
    "OPENLAKEFORGE_CATALOG_REST_URI",
    "OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX",
    "OPENLAKEFORGE_CATALOG_TOKEN_URI",
    "OPENLAKEFORGE_CATALOG_TYPE",
    "OPENLAKEFORGE_CATALOG_WAREHOUSE",
    "OPENLAKEFORGE_CATALOG_WAREHOUSE_PREFIX",
    "OPENLAKEFORGE_FLOE_LINEAGE_DATASET_NAMESPACE",
    "OPENLAKEFORGE_GOVERNANCE_ENABLED",
    "OPENLAKEFORGE_STAGE",
    "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET",
    "OPENLAKEFORGE_STORAGE_ENDPOINT",
    "OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS",
    "OPENLAKEFORGE_STORAGE_REGION",
    "OPENLAKEFORGE_STORAGE_SILVER_BUCKET",
    "OPENLINEAGE_ENDPOINT",
    "OPENLINEAGE_NAMESPACE",
    "OPENLINEAGE_URL",
)
# The Job's entrypoint: a console script, because Floe's profile takes a
# single `command` string and appends `run -c ... --entities ...` as args.
JOB_COMMAND = "olf-floe-job"


class FloeProfileError(RuntimeError):
    """The stage contract on this pod cannot drive a Floe 0.8 run."""


def render_profile(environ: Mapping[str, str], *, in_job: bool = False) -> dict[str, object]:
    """The stage's profile; `in_job` renders it for the Floe Job itself, which runs Floe locally."""

    def required(name: str) -> str:
        value = environ.get(name, "").strip()
        if not value:
            raise FloeProfileError(f"{name} is not set; the stage activation must provide it for Floe.")
        return value

    catalog_type = environ.get("OPENLAKEFORGE_CATALOG_TYPE", "rest")
    if catalog_type not in ("rest", "glue"):
        provider = environ.get("OPENLAKEFORGE_CATALOG_PROVIDER", "")
        raise FloeProfileError(
            f"catalog {catalog_type!r} ({provider or 'unknown provider'}) is not supported: "
            "Floe writes Iceberg through a REST or AWS Glue catalog."
        )

    # Terraform-managed code deployments (`manage_user_deployments`) carry
    # the storage binding only under its AWS SDK aliases, and the activation
    # release under both; accept either so one image loads on both paths.
    def first(*names: str) -> str:
        return next((value for name in names if (value := environ.get(name, "").strip())), "")

    region = first("OPENLAKEFORGE_STORAGE_REGION", "AWS_REGION", "AWS_DEFAULT_REGION")
    if not region:
        raise FloeProfileError(
            "OPENLAKEFORGE_STORAGE_REGION is not set; the stage activation must provide it for Floe."
        )
    endpoint = first("OPENLAKEFORGE_STORAGE_ENDPOINT", "AWS_ENDPOINT_URL_S3")
    path_style = first("OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS", "AWS_S3_FORCE_PATH_STYLE").lower()
    # Run reports land beneath the stage's own artifact prefix: an activated
    # stage's object-store grant covers `activations/<stage>/`, not the
    # bucket root the project's `report.path` is relative to.
    ops_bucket, _, ops_prefix = required("OPENLAKEFORGE_ARTIFACT_BASE_URI").removeprefix("s3://").partition("/")
    locations = {
        "lakehouse_bronze": (required("OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"), ""),
        "lakehouse_silver": (required("OPENLAKEFORGE_STORAGE_SILVER_BUCKET"), ""),
        "openlakeforge_ops": (ops_bucket, ops_prefix.strip("/")),
    }
    storages = []
    for name, (bucket, prefix) in locations.items():
        storage: dict[str, object] = {"name": name, "type": "s3", "bucket": bucket, "region": region}
        if prefix:
            storage["prefix"] = prefix
        if endpoint:
            storage["endpoint"] = endpoint
        if path_style:
            storage["path_style_access"] = path_style == "true"
        storages.append(storage)

    silver_bucket = required("OPENLAKEFORGE_STORAGE_SILVER_BUCKET")
    # The Secret is mounted whole (`envSecrets`), so its keys arrive under
    # their own names; Floe expands `${...}` from the environment at run time
    # and the credential itself never enters the profile.
    client_id = environ.get("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID_KEY") or "POLARIS_FLOE_CLIENT_ID"
    client_secret = environ.get("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET_KEY") or "POLARIS_FLOE_CLIENT_SECRET"
    catalog: dict[str, object]
    if catalog_type == "glue":
        # One Glue database per domain, named as `olf catalog sync-namespaces`
        # creates it: the stage's prefix (the catalog is shared by every
        # stage) and the domain's Silver namespace, which Floe substitutes
        # for `{domain}`. Credentials are the pod's own AWS identity.
        catalog = {
            "name": CATALOG_NAME,
            "type": "glue",
            "region": environ.get("OPENLAKEFORGE_CATALOG_GLUE_REGION") or region,
            "database": required("OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX") + "{domain}",
            "create_database_if_missing": False,
            "warehouse_storage": "lakehouse_silver",
            "warehouse_prefix": _absolute_prefix(
                silver_bucket, environ.get("OPENLAKEFORGE_CATALOG_GLUE_WAREHOUSE_PREFIX") or "warehouse/iceberg"
            ),
        }
    else:
        catalog = {
            "name": CATALOG_NAME,
            "type": "rest",
            "uri": required("OPENLAKEFORGE_CATALOG_REST_URI"),
            "credential": f"client_credentials:${{{client_id}}}:${{{client_secret}}}",
            "warehouse": environ.get("OPENLAKEFORGE_CATALOG_WAREHOUSE") or required("OPENLAKEFORGE_CATALOG_NAME"),
            "oauth2_server_uri": required("OPENLAKEFORGE_CATALOG_TOKEN_URI"),
            "scope": environ.get("OPENLAKEFORGE_CATALOG_OAUTH_SCOPE") or "PRINCIPAL_ROLE:ALL",
            "warehouse_storage": "lakehouse_silver",
            "warehouse_prefix": _absolute_prefix(silver_bucket, environ.get("OPENLAKEFORGE_CATALOG_WAREHOUSE_PREFIX", "")),
        }

    lineage_on = _governance_enabled(environ)
    runner = (
        {"type": "local"}
        if in_job
        else _job_runner(
            environ,
            required,
            polaris_client=(client_id, client_secret) if catalog_type == "rest" else None,
            lineage=lineage_on,
        )
    )
    profile: dict[str, object] = {
        "apiVersion": "floe/v1",
        "kind": "EnvironmentProfile",
        "metadata": {"name": environ.get("OPENLAKEFORGE_STAGE", "") or "openlakeforge"},
        # Sequential: a domain's Job runs its entities one after another, so
        # its memory is bounded by one entity's frames.
        "execution": {"runner": runner, "orchestration": {"strategy": "sequential"}},
        "storages": {"default": "lakehouse_bronze", "definitions": storages},
        "catalogs": {"default": CATALOG_NAME, "definitions": [catalog]},
        "validation": {"strict": True},
    }
    if lineage_on:
        profile["lineage"] = {
            "url": required("OPENLINEAGE_URL"),
            "endpoint": environ.get("OPENLINEAGE_ENDPOINT", "api/v1/openlineage/lineage"),
            "namespace": environ.get("OPENLINEAGE_NAMESPACE", "dagster"),
            "dataset_namespace": environ.get("OPENLAKEFORGE_FLOE_LINEAGE_DATASET_NAMESPACE")
            or ("aws_glue" if catalog_type == "glue" else "polaris"),
            "timeout_secs": 5,
        }
    return profile


def _absolute_prefix(bucket: str, prefix: str) -> str:
    prefix = prefix.strip()
    return prefix if "://" in prefix else f"s3://{bucket}/{prefix.lstrip('/')}".rstrip("/")


def _governance_enabled(environ: Mapping[str, str]) -> bool:
    # Terraform emits OPENLINEAGE_URL only when governance is on and does not
    # export the flag itself, so its absence falls back to the URL's presence.
    governance = environ.get("OPENLAKEFORGE_GOVERNANCE_ENABLED", "").strip()
    return governance == "true" or (not governance and bool(environ.get("OPENLINEAGE_URL", "").strip()))


def _job_runner(
    environ: Mapping[str, str],
    required: Callable[[str], str],
    *,
    polaris_client: tuple[str, str] | None,
    lineage: bool,
) -> dict[str, object]:
    # The run pod's own image: its Floe project and entrypoint are the ones
    # this code location loaded. DAGSTER_CURRENT_IMAGE is set on code servers
    # only, so the activation exports the image to run pods explicitly.
    image = environ.get("OPENLAKEFORGE_PROJECT_CODE_IMAGE") or environ.get("DAGSTER_CURRENT_IMAGE", "")
    if not image:
        raise FloeProfileError("OPENLAKEFORGE_PROJECT_CODE_IMAGE is not set; Floe Jobs run on the project-code image.")
    namespace = environ.get("OPENLAKEFORGE_KUBE_NAMESPACE") or required("NAMESPACE")
    # Secrets travel by reference: the Job resolves them from its own
    # namespace, and no credential value enters the profile or the Job spec.
    secrets: list[dict[str, str]] = []
    if polaris_client is not None:
        catalog_secret = required("OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME")
        secrets.extend({"name": key, "secret_name": catalog_secret, "key": key} for key in polaris_client)
    storage_secret = environ.get("OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME", "")
    if storage_secret:
        for name, key_env in (
            ("AWS_ACCESS_KEY_ID", "OPENLAKEFORGE_STORAGE_ACCESS_KEY_ID_KEY"),
            ("AWS_SECRET_ACCESS_KEY", "OPENLAKEFORGE_STORAGE_SECRET_ACCESS_KEY_KEY"),
        ):
            secrets.append({"name": name, "secret_name": storage_secret, "key": environ.get(key_env) or name})
    lineage_secret = environ.get("OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME", "")
    lineage_key = environ.get("OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY", "")
    if lineage and lineage_secret and lineage_key:
        secrets.append({"name": "OPENLINEAGE_API_KEY", "secret_name": lineage_secret, "key": lineage_key})
    return {
        "type": "kubernetes_job",
        "job_scope": "domain",
        "image": image,
        "namespace": namespace,
        # The run launcher's account: the chart grants it Jobs and pod logs
        # in the stage namespace, which is all dagster-floe needs.
        "service_account": environ.get("OPENLAKEFORGE_FLOE_SERVICE_ACCOUNT") or "dagster",
        "command": JOB_COMMAND,
        "timeout_seconds": 3600,
        "ttl_seconds_after_finished": 3600,
        "poll_interval_seconds": 5,
        "env": {name: environ[name] for name in _JOB_ENV if environ.get(name)},
        "secrets": secrets,
    }


def profile_path(directory: Path | None = None) -> Path:
    return (directory or Path(tempfile.gettempdir()) / "openlakeforge-floe") / "profile.yml"


def write_profile(
    environ: Mapping[str, str] | None = None,
    directory: Path | None = None,
    *,
    target: Path | None = None,
    in_job: bool = False,
) -> Path:
    """Render the profile for this pod's stage and return its path (JSON is valid YAML)."""
    target = target or profile_path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    profile = render_profile(os.environ if environ is None else environ, in_job=in_job)
    target.write_text(json.dumps(profile, indent=2) + "\n")
    return target
