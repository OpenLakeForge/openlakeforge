"""Floe EnvironmentProfile for the stage this project-code container runs in.

The Floe project under `lakehouse_code/silver/` says *what* to validate; this
profile says *where*: storages, the Iceberg catalog and the lineage endpoint,
all read from the provider-contract values the activation release puts on the
pod. It is rendered when the code location loads, in the code server and in
every run pod alike, so one image serves every stage and nothing stage-bound
is baked into it or published beside it.

Kept free of Dagster/Floe imports so `tools/olf` can unit test it.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

# `_domain.yml` sinks name this catalog; it is part of the project contract,
# not a stage binding, so the stage's own catalog name never reaches it.
CATALOG_NAME = "iceberg_catalog"


class FloeProfileError(RuntimeError):
    """The stage contract on this pod cannot drive a Floe 0.8 run."""


def render_profile(environ: Mapping[str, str]) -> dict[str, object]:
    def required(name: str) -> str:
        value = environ.get(name, "").strip()
        if not value:
            raise FloeProfileError(f"{name} is not set; the stage activation must provide it for Floe.")
        return value

    catalog_type = environ.get("OPENLAKEFORGE_CATALOG_TYPE", "rest")
    if catalog_type != "rest":
        provider = environ.get("OPENLAKEFORGE_CATALOG_PROVIDER", "")
        raise FloeProfileError(
            f"catalog {catalog_type!r} ({provider or 'unknown provider'}) is not supported: "
            "Floe 0.8 writes Iceberg through a REST catalog only."
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
    prefix = environ.get("OPENLAKEFORGE_CATALOG_WAREHOUSE_PREFIX", "").strip()
    if "://" not in prefix:
        prefix = f"s3://{silver_bucket}/{prefix.lstrip('/')}".rstrip("/")
    # The Secret is mounted whole (`envSecrets`), so its keys arrive under
    # their own names; Floe expands `${...}` from the environment at run time
    # and the credential itself never enters the profile.
    client_id = environ.get("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID_KEY", "POLARIS_FLOE_CLIENT_ID")
    client_secret = environ.get("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET_KEY", "POLARIS_FLOE_CLIENT_SECRET")
    catalog = {
        "name": CATALOG_NAME,
        "type": "rest",
        "uri": required("OPENLAKEFORGE_CATALOG_REST_URI"),
        "credential": f"client_credentials:${{{client_id}}}:${{{client_secret}}}",
        "warehouse": environ.get("OPENLAKEFORGE_CATALOG_WAREHOUSE") or required("OPENLAKEFORGE_CATALOG_NAME"),
        "oauth2_server_uri": required("OPENLAKEFORGE_CATALOG_TOKEN_URI"),
        "scope": environ.get("OPENLAKEFORGE_CATALOG_OAUTH_SCOPE") or "PRINCIPAL_ROLE:ALL",
        "warehouse_storage": "lakehouse_silver",
        "warehouse_prefix": prefix,
    }

    profile: dict[str, object] = {
        "apiVersion": "floe/v1",
        "kind": "EnvironmentProfile",
        "metadata": {"name": environ.get("OPENLAKEFORGE_STAGE", "") or "openlakeforge"},
        # In the Dagster run pod, through dagster-floe's in-process runner.
        # Sequential bounds the run pod's memory to one entity's frames.
        "execution": {"runner": {"type": "local"}, "orchestration": {"strategy": "sequential"}},
        "storages": {"default": "lakehouse_bronze", "definitions": storages},
        "catalogs": {"default": CATALOG_NAME, "definitions": [catalog]},
        "validation": {"strict": True},
    }
    # Terraform emits OPENLINEAGE_URL only when governance is on and does not
    # export the flag itself, so its absence falls back to the URL's presence.
    governance = environ.get("OPENLAKEFORGE_GOVERNANCE_ENABLED", "").strip()
    if governance == "true" or (not governance and environ.get("OPENLINEAGE_URL", "").strip()):
        profile["lineage"] = {
            "url": required("OPENLINEAGE_URL"),
            "endpoint": environ.get("OPENLINEAGE_ENDPOINT", "api/v1/openlineage/lineage"),
            "namespace": environ.get("OPENLINEAGE_NAMESPACE", "dagster"),
            "dataset_namespace": environ.get("OPENLAKEFORGE_FLOE_LINEAGE_DATASET_NAMESPACE", "polaris"),
            "timeout_secs": 5,
        }
    return profile


def write_profile(environ: Mapping[str, str] | None = None, directory: Path | None = None) -> Path:
    """Render the profile for this pod's stage and return its path (JSON is valid YAML)."""
    target = (directory or Path(tempfile.gettempdir()) / "openlakeforge-floe") / "profile.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(render_profile(os.environ if environ is None else environ), indent=2) + "\n")
    return target
