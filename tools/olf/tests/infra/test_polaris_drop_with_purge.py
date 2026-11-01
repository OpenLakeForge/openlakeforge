"""Polaris lets Trino drop views and tables.

Trino drops an Iceberg view or table through the REST catalog as a purge. Polaris refuses a purge unless
DROP_WITH_PURGE_ENABLED is on, so every dbt incremental model failed on its second run: dbt drops its
`__dbt_tmp` view, and Polaris answered 403 "Unable to purge entity".
"""

from __future__ import annotations

import re
from pathlib import Path

RELEASE = Path(__file__).resolve().parents[4] / "infra/terraform/modules/catalog/polaris/release.tf"


def test_the_polaris_release_enables_drop_with_purge() -> None:
    release = RELEASE.read_text()
    features = release[release.index("features = {") : release.index("}", release.index("features = {"))]

    assert re.search(r"^\s*DROP_WITH_PURGE_ENABLED\s*=\s*true\s*$", features, re.MULTILINE)
