"""Warehouse and application database bootstrap (setup-time only, uses the admin credential).

Role model (warehouse ``datawarden_wh``):

``dw_pipeline``   ingestion service. INSERT/SELECT on ``raw`` (append-only), writes ``ops``.
                  Member of ``dw_transformer`` so its dbt runs build canonical models.
``dw_transformer`` NOLOGIN group that owns canonical ``staging`` and ``marts`` relations.
``dw_executor``   approved-repair executor. Member of ``dw_transformer``; owns ``recovery``.
``dw_agent_ro``   agent tools. Read-only transactions, 5s statement timeout, SELECT on
                  non-PII columns of raw/staging/marts and operational ``ops`` tables only.
``dw_shadow``     shadow runs. SELECT on raw; can only create ``shadow_*`` schemas through a
                  vetted SECURITY DEFINER function; no privileges on canonical schemas.
``dw_validator``  protected validation. Read-only access to canonical, shadow, and ops.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg import sql

from datawarden.config import Settings, get_settings

log = logging.getLogger(__name__)

LOGIN_ROLES = ("pipeline", "agent_ro", "shadow", "executor", "validator")

RAW_TABLES = {
    "raw_order_events": """
        event_id text NOT NULL, event_type text NOT NULL, order_id text NOT NULL,
        customer_id text NOT NULL, order_total_paise bigint NOT NULL, currency text NOT NULL,
        customer_note text, event_ts timestamptz NOT NULL, schema_version text NOT NULL""",
    "raw_payment_events": """
        event_id text NOT NULL, payment_id text NOT NULL, order_id text NOT NULL,
        attempt_no integer NOT NULL, status text NOT NULL, amount_paise bigint NOT NULL,
        currency text NOT NULL, method text, event_ts timestamptz NOT NULL,
        schema_version text NOT NULL""",
    "raw_refund_events": """
        event_id text NOT NULL, refund_id text NOT NULL, payment_id text NOT NULL,
        order_id text NOT NULL, amount_paise bigint NOT NULL, currency text NOT NULL,
        status text NOT NULL, event_ts timestamptz NOT NULL, schema_version text NOT NULL""",
    "raw_customer_events": """
        event_id text NOT NULL, event_type text NOT NULL, customer_id text NOT NULL,
        full_name text, email text, phone text, city text, event_ts timestamptz NOT NULL,
        schema_version text NOT NULL""",
}
PII_COLUMNS = {"raw_customer_events": {"full_name", "email", "phone"}}

OPS_DDL = """
CREATE TABLE IF NOT EXISTS ops.ingested_batches (
    batch_id text PRIMARY KEY, entity text NOT NULL, schema_version text NOT NULL,
    delivery_seq integer NOT NULL, delivery_date date NOT NULL, sha256 text NOT NULL,
    record_count integer NOT NULL, run_id text NOT NULL, ingested_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ops.pipeline_runs (
    run_id text PRIMARY KEY, trigger text NOT NULL, status text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
    source_watermark integer, code_commit text, params jsonb NOT NULL DEFAULT '{}', error text);
CREATE TABLE IF NOT EXISTS ops.task_runs (
    id bigserial PRIMARY KEY, run_id text NOT NULL REFERENCES ops.pipeline_runs(run_id),
    task text NOT NULL, status text NOT NULL, attempt integer NOT NULL DEFAULT 1,
    started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
    detail jsonb NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS ops.run_logs (
    id bigserial PRIMARY KEY, run_id text NOT NULL, task text NOT NULL, level text NOT NULL,
    ts timestamptz NOT NULL DEFAULT now(), message text NOT NULL);
CREATE TABLE IF NOT EXISTS ops.schema_observations (
    id bigserial PRIMARY KEY, entity text NOT NULL, batch_id text NOT NULL,
    schema_version text NOT NULL, fingerprint text NOT NULL, fields jsonb NOT NULL,
    contract_status text NOT NULL, detail text, run_id text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ops.quality_results (
    id bigserial PRIMARY KEY, check_id text NOT NULL, check_type text NOT NULL,
    asset text NOT NULL, run_id text, status text NOT NULL, severity text NOT NULL,
    partition_date date, observed jsonb NOT NULL DEFAULT '{}', threshold jsonb NOT NULL DEFAULT '{}',
    message text NOT NULL, evaluated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS quality_results_check_idx ON ops.quality_results (check_id, evaluated_at DESC);
CREATE TABLE IF NOT EXISTS ops.demo_settings (key text PRIMARY KEY, value text NOT NULL);
INSERT INTO ops.demo_settings VALUES ('synthetic', 'true') ON CONFLICT DO NOTHING;
"""

# Append-only guard for raw tables. Only the vetted demo purge function may delete, and only
# in a synthetic warehouse.
APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION raw.forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('dw.demo_purge', true) = 'on' AND TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'raw tables are append-only (% on %)', TG_OP, TG_TABLE_NAME;
END $$;

CREATE OR REPLACE FUNCTION ops.demo_purge_batches(batch_ids text[]) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, raw, ops AS $$
DECLARE n integer := 0; c integer;
BEGIN
  IF (SELECT value FROM ops.demo_settings WHERE key = 'synthetic') IS DISTINCT FROM 'true' THEN
    RAISE EXCEPTION 'demo purge refused: warehouse is not marked synthetic';
  END IF;
  PERFORM set_config('dw.demo_purge', 'on', true);
  DELETE FROM raw.raw_order_events WHERE _batch_id = ANY(batch_ids); GET DIAGNOSTICS c = ROW_COUNT; n := n + c;
  DELETE FROM raw.raw_payment_events WHERE _batch_id = ANY(batch_ids); GET DIAGNOSTICS c = ROW_COUNT; n := n + c;
  DELETE FROM raw.raw_refund_events WHERE _batch_id = ANY(batch_ids); GET DIAGNOSTICS c = ROW_COUNT; n := n + c;
  DELETE FROM raw.raw_customer_events WHERE _batch_id = ANY(batch_ids); GET DIAGNOSTICS c = ROW_COUNT; n := n + c;
  DELETE FROM ops.ingested_batches WHERE batch_id = ANY(batch_ids);
  PERFORM set_config('dw.demo_purge', 'off', true);
  RETURN n;
END $$;

CREATE OR REPLACE FUNCTION ops.create_shadow_schema(name text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
  IF name !~ '^shadow_[a-z0-9_]{1,50}$' THEN
    RAISE EXCEPTION 'invalid shadow schema name %', name;
  END IF;
  EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I AUTHORIZATION dw_shadow', name);
  EXECUTE format('GRANT USAGE ON SCHEMA %I TO dw_validator', name);
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE dw_shadow IN SCHEMA %I '
                 'GRANT SELECT ON TABLES TO dw_validator', name);
END $$;

CREATE OR REPLACE FUNCTION ops.drop_shadow_schema(name text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
  IF name !~ '^shadow_[a-z0-9_]{1,50}$' THEN
    RAISE EXCEPTION 'invalid shadow schema name %', name;
  END IF;
  EXECUTE format('DROP SCHEMA IF EXISTS %I CASCADE', name);
END $$;
"""


def _ensure_role(cur: psycopg.Cursor, name: str, password: str | None) -> None:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    exists = cur.fetchone() is not None
    if password is None:
        if not exists:
            cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(name)))
        return
    if not password:
        raise RuntimeError(f"password for role {name} is empty; run `make setup` to generate .env")
    verb = "ALTER" if exists else "CREATE"
    cur.execute(
        sql.SQL("{} ROLE {} LOGIN PASSWORD {}").format(sql.SQL(verb), sql.Identifier(name), sql.Literal(password))
    )


def _ensure_database(cur: psycopg.Cursor, name: str, owner: str) -> None:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
    if cur.fetchone() is None:
        cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(owner)))
    cur.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))


def bootstrap_app_database(settings: Settings | None = None) -> None:
    s = settings or get_settings()
    with psycopg.connect(s.admin_dsn("postgres", s.app_db_host, s.app_db_port), autocommit=True) as conn:
        cur = conn.cursor()
        _ensure_role(cur, s.app_db_user, s.app_db_password.get_secret_value())
        _ensure_database(cur, s.app_db_name, s.app_db_user)
        cur.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(s.app_db_name), sql.Identifier(s.app_db_user)
            )
        )


def bootstrap_warehouse(settings: Settings | None = None, *, recreate: bool = False) -> None:
    """Create roles, database, schemas, raw tables, and privileges. Idempotent.

    ``recreate`` drops and recreates all warehouse schemas (synthetic demo reset only).
    """
    s = settings or get_settings()
    with psycopg.connect(s.admin_dsn("postgres"), autocommit=True) as conn:
        cur = conn.cursor()
        _ensure_role(cur, "dw_transformer", None)
        for role in LOGIN_ROLES:
            _ensure_role(cur, f"dw_{role}", getattr(s, f"wh_{role}_password").get_secret_value())
        _ensure_database(cur, s.wh_db_name, s.pg_admin_user)
        for role in LOGIN_ROLES:
            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(s.wh_db_name), sql.Identifier(f"dw_{role}")
                )
            )
        cur.execute("GRANT dw_transformer TO dw_pipeline, dw_executor")
        # dbt incremental merges stage rows in temporary tables.
        cur.execute(
            sql.SQL("GRANT TEMPORARY ON DATABASE {} TO dw_transformer, dw_shadow").format(sql.Identifier(s.wh_db_name))
        )
        cur.execute(
            sql.SQL("ALTER ROLE dw_agent_ro IN DATABASE {} SET default_transaction_read_only = on").format(
                sql.Identifier(s.wh_db_name)
            )
        )
        cur.execute(
            sql.SQL("ALTER ROLE dw_agent_ro IN DATABASE {} SET statement_timeout = '5s'").format(
                sql.Identifier(s.wh_db_name)
            )
        )
        cur.execute(
            sql.SQL("ALTER ROLE dw_agent_ro IN DATABASE {} SET search_path = marts, staging, raw").format(
                sql.Identifier(s.wh_db_name)
            )
        )
        cur.execute(
            sql.SQL("ALTER ROLE dw_validator IN DATABASE {} SET default_transaction_read_only = on").format(
                sql.Identifier(s.wh_db_name)
            )
        )
        cur.execute(
            sql.SQL("ALTER ROLE dw_shadow IN DATABASE {} SET statement_timeout = '120s'").format(
                sql.Identifier(s.wh_db_name)
            )
        )

    with psycopg.connect(s.wh_dsn("admin"), autocommit=True) as conn:
        cur = conn.cursor()
        if recreate:
            cur.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'shadow\\_%'")
            for (name,) in cur.fetchall():
                cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
            for schema in ("marts", "staging", "raw", "ops", "recovery"):
                cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        cur.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        cur.execute("CREATE SCHEMA IF NOT EXISTS raw")
        cur.execute("CREATE SCHEMA IF NOT EXISTS ops")
        cur.execute("CREATE SCHEMA IF NOT EXISTS staging AUTHORIZATION dw_transformer")
        cur.execute("CREATE SCHEMA IF NOT EXISTS marts AUTHORIZATION dw_transformer")
        cur.execute("CREATE SCHEMA IF NOT EXISTS recovery AUTHORIZATION dw_executor")
        cur.execute(OPS_DDL)
        cur.execute(APPEND_ONLY_FN)
        for table, cols in RAW_TABLES.items():
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS raw.{table} (
                    _load_id bigserial PRIMARY KEY, {cols},
                    _batch_id text NOT NULL, _source_checksum text NOT NULL, _line_no integer NOT NULL,
                    _run_id text NOT NULL, _ingested_at timestamptz NOT NULL DEFAULT now(),
                    UNIQUE (_batch_id, _line_no))""")
            cur.execute(f"CREATE INDEX IF NOT EXISTS {table}_event_idx ON raw.{table} (event_id)")
            cur.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON raw.{table}")
            cur.execute(f"""CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON raw.{table}
                            FOR EACH ROW EXECUTE FUNCTION raw.forbid_mutation()""")
            cur.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON raw.{table}")
            cur.execute(f"""CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON raw.{table}
                            FOR EACH STATEMENT EXECUTE FUNCTION raw.forbid_mutation()""")

        # --- privileges ---------------------------------------------------------------------
        cur.execute("""
            GRANT USAGE ON SCHEMA raw, ops TO dw_pipeline, dw_transformer, dw_validator, dw_executor;
            GRANT USAGE ON SCHEMA raw TO dw_shadow;
            GRANT INSERT, SELECT ON ALL TABLES IN SCHEMA raw TO dw_pipeline;
            GRANT USAGE ON ALL SEQUENCES IN SCHEMA raw TO dw_pipeline;
            GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA ops TO dw_pipeline;
            GRANT USAGE ON ALL SEQUENCES IN SCHEMA ops TO dw_pipeline;
            REVOKE UPDATE ON ops.ingested_batches FROM dw_pipeline;
            GRANT SELECT ON ALL TABLES IN SCHEMA raw TO dw_transformer, dw_shadow, dw_validator;
            GRANT SELECT ON ALL TABLES IN SCHEMA ops TO dw_validator, dw_executor;
            GRANT INSERT ON ops.pipeline_runs, ops.task_runs, ops.run_logs TO dw_executor;
            GRANT UPDATE ON ops.pipeline_runs, ops.task_runs TO dw_executor;
            GRANT USAGE ON ALL SEQUENCES IN SCHEMA ops TO dw_executor;
            GRANT USAGE ON SCHEMA staging, marts TO dw_validator, dw_agent_ro;
            REVOKE ALL ON FUNCTION ops.demo_purge_batches(text[]) FROM PUBLIC;
            REVOKE ALL ON FUNCTION ops.create_shadow_schema(text) FROM PUBLIC;
            REVOKE ALL ON FUNCTION ops.drop_shadow_schema(text) FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION ops.create_shadow_schema(text), ops.drop_shadow_schema(text) TO dw_shadow;
            GRANT EXECUTE ON FUNCTION ops.demo_purge_batches(text[]) TO dw_pipeline;
            ALTER DEFAULT PRIVILEGES FOR ROLE dw_transformer IN SCHEMA staging, marts
                GRANT SELECT ON TABLES TO dw_validator, dw_agent_ro;
            GRANT SELECT ON ALL TABLES IN SCHEMA staging, marts TO dw_validator, dw_agent_ro;
        """)
        # Agents: raw tables minus PII columns, operational ops tables, never recovery/shadow.
        cur.execute("GRANT USAGE ON SCHEMA raw, ops TO dw_agent_ro")
        for table, cols in RAW_TABLES.items():
            names = [c.strip().split()[0] for c in cols.split(",") if c.strip()]
            names += ["_load_id", "_batch_id", "_source_checksum", "_line_no", "_run_id", "_ingested_at"]
            allowed = [n for n in names if n not in PII_COLUMNS.get(table, set())]
            cur.execute(sql.SQL("REVOKE ALL ON raw.{} FROM dw_agent_ro").format(sql.Identifier(table)))
            cur.execute(
                sql.SQL("GRANT SELECT ({}) ON raw.{} TO dw_agent_ro").format(
                    sql.SQL(", ").join(sql.Identifier(n) for n in allowed), sql.Identifier(table)
                )
            )
        cur.execute("""GRANT SELECT ON ops.ingested_batches, ops.pipeline_runs, ops.task_runs, ops.run_logs,
                       ops.schema_observations, ops.quality_results TO dw_agent_ro""")
    log.info("warehouse bootstrap complete")
