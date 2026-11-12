"""Canonical user-authored lakehouse code for OpenLakeForge.

``lakehouse_code/`` is the provider-neutral, descriptor-driven contract between
data teams and the OpenLakeForge platform:

- ``governance.yml`` — the ``Governance`` descriptor: domains, and the data products nested under them.
- ``bronze/<source>/source.yaml`` — per-source ``Source`` descriptors and the
  dlt extract code that lands each source's resources in Bronze.
- ``silver/floe.yml``, ``silver/sources.yml`` and
  ``silver/<domain>/contracts/floe/`` — the Floe project, its Bronze sources,
  and the per-domain Floe contracts.
- ``gold/`` — the Gold dbt project, with a folder of models per product
  under ``gold/models/<product>/``.
- ``dashboards/superset/<product>/`` — Superset report bundles.
- ``pipelines/dagster/<product>.py`` — user-authored Dagster pipeline modules.
- ``definitions.py`` — aggregates every product pipeline module into one
  Dagster code location.

The canonical inventory loader is ``openlakeforge_core.load_lakehouse_inventory``.
"""