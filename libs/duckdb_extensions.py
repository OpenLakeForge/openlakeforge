"""DuckDB extensions Floe's merge write modes load, baked into the project-code image (#725).

Floe 0.8 runs `merge_scd1`/`merge_scd2` through DuckDB: an S3 secret, then
`ATTACH ... (TYPE iceberg)`. DuckDB resolves the extensions those need on first
use and, unless they are already in its extension directory, downloads them
over plain HTTP from extensions.duckdb.org. Floe Job pods have no egress, so the
image build installs them where the image user's DuckDB looks by default
(`~/.duckdb/extensions/<duckdb version>/<platform>/`), for the DuckDB version
the image itself installed.

Floe opens its own connection with DuckDB's default autoinstall setting, and
DuckDB 1.5 reads no environment variable for it, so the guarantee is the
verification below rather than a run-time setting: it loads every extension
the way Floe's statements do, with autoinstall off and the repository pointed
at an address that refuses connections, so a missing or mismatched extension
fails the build instead of a Job.

    python -m libs.duckdb_extensions install [DIRECTORY]
    python -m libs.duckdb_extensions verify [DIRECTORY]

DIRECTORY defaults to DuckDB's own default extension directory.
"""

from __future__ import annotations

import sys
from typing import Any

# `iceberg` loads `avro` while it initialises, so it is needed although no
# Floe statement names it.
EXTENSIONS = ("httpfs", "aws", "iceberg", "avro")

# Port 9 (discard) on loopback: any install attempt fails at once rather than
# reaching the network or waiting on a timeout.
_NO_REPOSITORY = "http://127.0.0.1:9"


def _connect(directory: str | None, **config: Any) -> Any:
    import duckdb

    if directory:
        config["extension_directory"] = directory
    return duckdb.connect(config=config)


def install(directory: str | None = None) -> None:
    connection = _connect(directory)
    for name in EXTENSIONS:
        connection.install_extension(name)


def verify(directory: str | None = None) -> None:
    """Load every extension as Floe's merge setup does, with no way to download one."""
    connection = _connect(
        directory,
        autoinstall_known_extensions=False,
        autoload_known_extensions=True,
        custom_extension_repository=_NO_REPOSITORY,
        autoinstall_extension_repository=_NO_REPOSITORY,
    )
    try:
        # Autoload, as Floe's own statements trigger it: the S3 secret loads
        # httpfs and aws, the iceberg secret loads iceberg and with it avro.
        # `CHAIN 'env'` keeps the credential chain off instance metadata, and
        # `VALIDATION 'none'` lets it be created where no credentials exist,
        # as in an image build.
        connection.execute(
            "CREATE SECRET (TYPE s3, PROVIDER credential_chain, CHAIN 'env', VALIDATION 'none', REGION 'us-east-1')"
        )
        connection.execute("CREATE SECRET (TYPE iceberg, TOKEN 'offline-check')")
    except Exception as exc:  # duckdb raises its own error hierarchy
        raise RuntimeError(
            f"DuckDB extensions for Floe merges do not load offline: {exc}"
        ) from exc
    loaded = {
        row[0]
        for row in connection.execute(
            "SELECT extension_name FROM duckdb_extensions() WHERE loaded"
        ).fetchall()
    }
    missing = sorted(set(EXTENSIONS) - loaded)
    if missing:
        raise RuntimeError(
            f"DuckDB extensions for Floe merges did not load offline: {', '.join(missing)}"
        )


def main(argv: list[str]) -> None:
    if len(argv) not in (1, 2) or argv[0] not in ("install", "verify"):
        raise SystemExit(
            "usage: python -m libs.duckdb_extensions install|verify [DIRECTORY]"
        )
    directory = argv[1] if len(argv) == 2 else None
    try:
        (install if argv[0] == "install" else verify)(directory)
    except RuntimeError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    if argv[0] == "verify":
        import duckdb

        print(
            f"DuckDB {duckdb.__version__} loads {', '.join(EXTENSIONS)} with no network."
        )


if __name__ == "__main__":
    main(sys.argv[1:])
