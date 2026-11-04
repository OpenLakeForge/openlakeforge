# Load an external source incrementally

How to load an API or a database into Bronze one change at a time, carry it
through Silver and Gold incrementally, and rebuild them when you need to. For
the contract each file follows, see the
[descriptor reference](../reference/descriptors.md): its `source.yaml`,
**Load modes** and **Full refresh** sections are the canonical rules this
guide applies.

The examples use a source `shop` with an HTTP API resource `products` and a
database resource `orders`, in a domain `sales`.

## Start from the scaffold

```bash
olf source new shop --kind api --resource products --cursor updated_at --primary-key sku
```

`--kind api` (or `--kind sql`) writes everything the rest of this guide
describes for one source: `source.yaml` with each resource incremental and its
`runtime` keys, a connector on `openlakeforge_runtime.bronze_incremental`, and an example CSV per
resource. The example is not loaded; give it the real columns, because

```bash
olf domain new sales --input shop/products
```

infers the Silver contract's columns from it, and writes the entity with
`incremental_mode: file`, `merge_scd1` on the declared primary key, and a
Parquet entry in `silver/sources.yml`. The sections below explain each piece,
for adapting the scaffold or writing a source by hand.

## Declare the source

In `lakehouse_code/bronze/shop/source.yaml`, give each resource its load mode
and name the settings and credentials the connector reads:

```yaml
apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: shop
displayName: Shop
description: Products from the shop API and orders from its database.
status: active
resources:
  - name: products
    load: { mode: incremental, cursor: updated_at, primary_key: [sku] }
  - name: orders
    load: { mode: incremental, cursor: id, primary_key: [id] }
runtime:
  config: [base_url, host, port, database]
  secrets: [api_token, username, password]
```

A cursor is a column that only grows when a row is added or changed: a
modification time, or a key that increases. A resource with no usable cursor
stays `full` and is replaced on every run.

## Write the connector

The connector lives in `lakehouse_code/bronze/shop/dlt/shop.py`. It is a dlt
source whose incremental resources declare their cursor with
`dlt.sources.incremental`, on the column `source.yaml` names. A loader runs it
through `openlakeforge_runtime.bronze_dlt.load_dlt_source`, which applies the declared modes and
refuses a cursor that differs from the descriptor.

`openlakeforge_runtime.bronze_incremental` holds the two read patterns most sources fit, so the
connector keeps only the request, the query and the row shape:

| Helper | For | What it guarantees |
| --- | --- | --- |
| `newest_first_pages` | An API that sorts by modification time but cannot filter on it | Stops at the cursor; reads a changed item once; waits between requests; with no cursor yet, loads the newest pages the budget allows; with one, fails rather than skip items when the budget runs out |
| `keyset_batches` | A table with an increasing key | Reads past the cursor in key order, at most `max_rows` a run, so a large table backfills a slice per run and a killed run resumes |
| `sql_engine` | A database source | A SQLAlchemy engine from the bound `host`, `port`, `database`, `username`, `password` |

```python
from collections.abc import Iterator, Sequence
from typing import Any

import dlt
import sqlalchemy as sa
from dlt.sources.helpers import requests
from openlakeforge_runtime.bronze_csv import BronzeLoadResult
from openlakeforge_runtime.bronze_dlt import load_dlt_source
from openlakeforge_runtime.bronze_incremental import keyset_batches, newest_first_pages, sql_engine

_ORDERS = sa.text("select id, sku, quantity, placed_at from orders where id > :last order by id limit :limit")


@dlt.source(name="shop")
def shop(
    base_url: str = dlt.config.value,
    api_token: str = dlt.secrets.value,
    host: str = dlt.config.value,
    port: int = dlt.config.value,
    database: str = dlt.config.value,
    username: str = dlt.secrets.value,
    password: str = dlt.secrets.value,
) -> Any:
    def page(number: int) -> list[dict[str, Any]]:
        response = requests.get(
            f"{base_url}/products",
            params={"sort": "-updated_at", "page": number, "per_page": 100},
            headers={"Authorization": f"Bearer {api_token}"},
        )
        response.raise_for_status()
        return response.json()["items"]

    @dlt.resource(name="products", primary_key="sku")
    def products(updated_at: Any = dlt.sources.incremental("updated_at", initial_value=0)) -> Iterator[Any]:
        yield from newest_first_pages(
            page, updated_at, cursor_field="updated_at", key="sku", page_size=100, max_pages=20, interval_seconds=1
        )

    @dlt.resource(name="orders", primary_key="id")
    def orders(id: Any = dlt.sources.incremental("id", initial_value=0)) -> Iterator[Any]:
        engine = sql_engine(
            "postgresql+psycopg2",
            {"host": host, "port": port, "database": database, "username": username, "password": password},
            connect_args={"connect_timeout": 30},
        )
        with engine.connect() as connection:
            yield from keyset_batches(connection, _ORDERS, id, cursor_field="id", batch_size=10_000, max_rows=100_000)

    return products, orders


def load_shop_entities_to_bronze(resources: Sequence[str]) -> dict[str, BronzeLoadResult]:
    return load_dlt_source("shop", shop(), resources)
```

Choose the budgets from the source's limits: an API that serves at most N
pages, or allows N requests a minute, sets `max_pages` and `interval_seconds`;
a database's load sets `max_rows`. Declare the column types of anything a page
or batch can leave empty (`columns=` on the resource), or a later run can land
the column under another type.

Batches land at `<stage Bronze bucket>/shop/<resource>/` as Parquet, one file
per run, named so the newest sorts last; dlt keeps each cursor in the same
bucket, per stage.

## Read it into Silver

In `lakehouse_code/silver/sources.yml`, name the source's Parquet files:

```yaml
sources:
  - name: "shop"
    format: "parquet"
    path: "shop/{{resource}}"
    storage: "lakehouse_bronze"
    options: { glob: "*.parquet" }
```

An entity reading an incremental resource reads new files only and merges them
on the primary key: `incremental_mode: "file"` with `sink.write_mode`
`merge_scd1` (`merge_scd2` and `append` are also allowed). `olf check contracts`
rejects a mode that does not fit the resource; the rules are in **Load modes**
in the [descriptor reference](../reference/descriptors.md).

## Bind each stage

Every stage that runs the source binds the keys `runtime` lists: a ConfigMap
`olf-source-shop` and a Secret `olf-source-shop-credentials` in the stage
namespace (the naming rules are in the `source.yaml` section of the
[descriptor reference](../reference/descriptors.md)):

```bash
kubectl -n olf-dev create configmap olf-source-shop \
  --from-literal=base_url=https://api.example.test --from-literal=host=db.example.test \
  --from-literal=port=5432 --from-literal=database=shop
```

```bash
kubectl -n olf-dev create secret generic olf-source-shop-credentials \
  --from-literal=api_token="$SHOP_API_TOKEN" --from-literal=username=reader \
  --from-literal=password="$SHOP_DB_PASSWORD"
```

`olf infra project deploy` refuses to activate a revision in a stage that has
not bound every key.

On `local`, a stage's run pods reach nothing outside the cluster unless its
Deployment Profile opens the ports the source needs (see
[`spec.stages`](../reference/deployment-profile.md)):

```yaml
stages:
  dev:
    source_egress:
      ports: [443, 5432]
```

## Test it offline

`load_dlt_source` takes a local bucket and project root, so a test can run the
real connector against a controlled server or a temporary database and assert
on the Parquet it lands:

```python
from openlakeforge_runtime.bronze_dlt import load_dlt_source


def test_a_second_run_with_no_change_loads_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCES__SHOP__BASE_URL", fake_api.url)
    # ... the other SOURCES__SHOP__* bindings
    def load():
        return load_dlt_source(
            "shop", shop(), ("products",), environ={"OPENLAKEFORGE_STAGE": "dev"},
            bucket_url=(tmp_path / "bronze").as_uri(), lakehouse_root=LAKEHOUSE_CODE,
        )["products"].rows

    assert load() > 0
    assert load() == 0
```

Cover the first run, a run with no change, a changed row, a page budget that
runs out, and DEV and PROD keeping their own cursor.

## Run it

```bash
olf infra product run <product> --stage dev
```

A run is incremental: Bronze takes the rows past each cursor, Floe merges the
new files, and dbt runs the product's incremental models. To rebuild Silver and
Gold from everything Bronze holds, without extracting again:

```bash
olf infra product run <product> --stage dev --full-refresh bronze
```

A row deleted at the source never reaches Bronze, so no run removes it from
Silver; the known limits are listed under **Full refresh** in the
[descriptor reference](../reference/descriptors.md).
