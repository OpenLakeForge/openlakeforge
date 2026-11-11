"""The committed OpenAPI document (ADR 0021, decision 5), rendered from the app.

The API serves no `/openapi.json`: it would be a route outside the permission
matrix. The document is a reviewed file instead, and a test fails when it and
the app disagree. Regenerate it, from packages/platform-api, with:

    uv run --locked python -m openlakeforge_platform_api.openapi
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import Settings

# Dev layout only: the wheel does not ship docs/, and nothing at run time calls this.
DOCUMENT_PATH = Path(__file__).resolve().parents[3] / "docs" / "schema" / "platform-api.openapi.json"
REGENERATE = "cd packages/platform-api && uv run --locked python -m openlakeforge_platform_api.openapi"


def render() -> str:
    """The document as stable text: sorted keys, two-space indent, one trailing newline."""
    # Nothing in the document depends on a deployment's settings; these only let the app be built.
    settings = Settings(
        issuer_url="https://issuer.invalid",
        audiences=("olf-cli",),
        role_claim="groups",
        role_mapping={},
        precedence=("platform-admin", "data-engineer", "analyst", "viewer"),
        stages=("dev",),
    )
    return json.dumps(create_app(settings).openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    DOCUMENT_PATH.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {DOCUMENT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
