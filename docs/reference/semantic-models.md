# Semantic models reference

How a data product declares its business metrics, keys, relationships and AI
context as an Apache Ossie semantic model under `lakehouse_code/semantic/`, and
what `olf check contracts` verifies about it. This page covers the offline
adapter: reading, validating and exposing the models. Publishing them to
OpenMetadata is not part of it yet.

OpenLakeForge does not define its own semantic YAML. Metrics here are metadata:
they are validated and exposed, not compiled to SQL or executed.

## Where a model lives

```text
lakehouse_code/semantic/<product>/<name>.ossie.yaml
```

`<product>` must be a product in `governance.yml`; any other folder fails the
check. A product may hold several files. Each file holds one model.

| Concern | Owner |
| --- | --- |
| Column names, descriptions, key tests, relationship tests | dbt `schema.yml` |
| Metrics, dimensions and time fields, keys, relationships, `ai_context` | Ossie file |
| Domain, product, owners, classification | `governance.yml` |

## Supported version and subset

The adapter reads the Ossie **0.1.1** content published at the upstream tag
`osi-0.1.1-rc1`. Apache has published no final Ossie release yet, so this is a
release candidate; moving to a final 0.1.1 or to 0.3.0 is a follow-up. Documents
declare `version: "0.1.1"`, the schema's constant, and any other version,
`*.dev0` included, is rejected.

The packaged schema is [`ossie-0.1.1.schema.json`](../schema/ossie-0.1.1.schema.json),
vendored unchanged from upstream `core-spec/osi-schema.json` (Apache License
2.0). Its source is recorded in
[`ossie-0.1.1.schema.provenance.json`](../schema/ossie-0.1.1.schema.provenance.json):
tag, commit `faf581054dcf7964d5fe0ceae7d6f415c8ce32a5`, raw URL and SHA-256. A
test fails if the schema file changes without that record.

Inside 0.1.1 the adapter accepts only the subset that Ossie 0.3 reads the same
way. Each rule below is named in the diagnostic (`file:line: [rule] message`).

| Rule | Requirement |
| --- | --- |
| `ossie-version` | `version: 0.1.1` |
| `one-model-per-file` | `semantic_model` has exactly one item |
| `no-custom-extensions` | no `custom_extensions` at any level |
| `logical-source` | a dataset `source` is `<schema>.<table>`, such as `sales_core_gold.fct_order_lines`; no catalog, stage or query |
| `ansi-expression` | every field and metric has an `ANSI_SQL` expression; other dialects are kept, not validated |
| `field-equals-column` | a field's expression is its own name, so field names equal column names |
| `metric-qualified-references` | a metric reads fields as `<dataset>.<field>`; a bare word that is not a SQL keyword, type name or interval unit is refused |
| `single-column-relationships` | a relationship joins one column to one column |

## What `olf check contracts` verifies

The check is offline and reads `schema.yml` files as text, so it needs no dbt
run and runs on Slim. It reports as `semantic_models`.

| Rule | Fails when |
| --- | --- |
| `schema` | the file does not match the vendored JSON Schema |
| `source-exists` | a dataset source is not a model of the owning product, or an `access: public` model of a product it depends on through `ref()` |
| `field-is-column` | a field is not a column the model's `schema.yml` declares |
| `key-test` | a `primary_key` or `unique_keys` entry has no dbt `unique` test; a primary key also needs `not_null` on each column |
| `relationship-test` | a relationship has no dbt `relationships` test between the same columns |
| `relationship-target-key` | the `to` column of a relationship has no dbt `unique` test |
| `metric-reference` | a metric reads a field that its dataset does not declare |
| `duplicate-name`, `duplicate-key` | two datasets, relationships, metrics or models share a name, or a YAML key is written twice |
| `product-exists` | the folder under `semantic/` is not a product |

Metric expressions are scanned as text, not parsed. The bare words a metric may use are
SQL keywords, type names and interval units, plus function calls; a legitimate
construct outside that list is refused until it is added in
`openlakeforge_core/semantic.py`.

dbt properties are read from `schema.yml` only. A model disabled in its properties
file is ignored; one disabled by `config(enabled=false)` in its SQL is not, and a
relationships test that targets `ref('<package>', '<model>')` is read as a test
against the project's own model of that name.

A composite key matches a model-level `unique_combination_of_columns` test on
the same columns. A field description that differs from the dbt column
description is a warning, because dbt owns column text.

Glossary references are not validated against Ossie models yet: Ossie 0.1.1 has
no term field, so there is no reference to check. Glossary terms are linked from
`governance.yml` and `schema.yml`, not from the Ossie file.

## Unsupported-feature matrix

| Ossie construct | Handling |
| --- | --- |
| Several models in one file; `semantic_model` as a list of more than one | rejected (`one-model-per-file`) |
| `custom_extensions` | rejected (`no-custom-extensions`) |
| Composite relationships | rejected (`single-column-relationships`) |
| Expressions in a field, computed fields | rejected (`field-equals-column`) |
| Dialects other than `ANSI_SQL` | kept in the file, not validated or exposed |
| Query-valued `source` | rejected (`logical-source`) |
| Ossie 0.3 syntax (`nonportable_extensions`, root-level model) | rejected (`ossie-version`) |

## What the inventory exposes

`LakehouseInventory.semantic_inventory` returns the models that passed and the
diagnostics for those that did not. Each `SemanticModel` carries its datasets,
keys, relationships, metrics with their expression and `ai_context`, and its
provenance: Ossie version, file and a SHA-256 of the file. Consumers read that
model, not the Ossie document, so a later Ossie release changes the parser and
not them.

See [Build and manage data products](../guides/data-products.md#add-semantic-metrics)
for the workflow.
