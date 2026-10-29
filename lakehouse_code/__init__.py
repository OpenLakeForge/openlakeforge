"""Canonical user-authored lakehouse code for OpenLakeForge.

``lakehouse_code/`` is the provider-neutral, descriptor-driven contract between
data teams and the OpenLakeForge platform:

- ``governance.yml`` — the ``Governance`` descriptor: domains and products.
- ``bronze/<source>/source.yaml`` — per-source ``Source`` descriptors and the
  dlt extract code that lands each source's resources in Bronze.
- ``silver/<domain>/contracts/floe/`` — Floe validation contracts and their
  generated manifests.
- ``gold/<product>/dbt/`` — product dbt projects.
- ``dashboards/superset/<product>/`` — Superset report bundles.
- ``pipelines/dagster/<product>.py`` — user-authored Dagster pipeline modules.
- ``definitions.py`` — aggregates every product pipeline module into one
  Dagster code location.

The canonical inventory loader is ``openlakeforge_domain.load_lakehouse_inventory``.
"""