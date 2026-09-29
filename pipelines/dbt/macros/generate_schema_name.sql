{#- Canonical targets use the exact custom schema (staging, marts). Shadow targets build every
    model inside the single incident-scoped shadow schema, so a shadow run cannot write to a
    canonical schema even by accident (its role has no privileges there either). -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if target.name.startswith('shadow') or custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
