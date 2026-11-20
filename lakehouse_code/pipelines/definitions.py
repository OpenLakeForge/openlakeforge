"""The Dagster code location: the module `definitions_module` in the deployment contract names."""

from __future__ import annotations

from openlakeforge_runtime.code_location import build_code_location

from lakehouse_code.pipelines.products import PRODUCTS

defs = build_code_location(PRODUCTS)
