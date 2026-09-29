{#- Business day = calendar date in the business timezone of a UTC timestamp. -#}
{% macro business_date(ts_column) -%}
    (({{ ts_column }}) at time zone '{{ var("business_tz") }}')::date
{%- endmacro %}
