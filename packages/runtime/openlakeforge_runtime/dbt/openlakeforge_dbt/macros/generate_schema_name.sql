{#- A Gold model's schema is its product's: the folder under models/ it sits in
    (ADR 0004). A `schema` config is kept verbatim so `olf check contracts`
    can reject it by name instead of it being silently rewritten. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
  {%- if custom_schema_name is not none -%}
    {{ custom_schema_name | trim }}
  {%- elif node.fqn | length > 2 and not node.fqn[1].startswith('_') -%}
    {{ env_var('OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX', '') }}{{ node.fqn[1] }}_gold
  {%- elif node.resource_type == 'model' -%}
    {{ exceptions.raise_compiler_error(
        "Gold model " ~ node.name ~ " is not in a product folder: put it under models/<product>/") }}
  {%- else -%}
    {{ target.schema }}
  {%- endif -%}
{%- endmacro %}
