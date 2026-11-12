{#- Each model's schema is its product folder's <product>_gold (ADR 0004). dbt takes this macro
    only from the root project, so it delegates to the OpenLakeForge package's. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
  {{ openlakeforge_dbt.generate_schema_name(custom_schema_name, node) }}
{%- endmacro %}
