"""Airflow DAG for the full profile.

Airflow owns *scheduling*; DataWarden (LangGraph) owns incident reasoning. The task runs the same
`dw pipeline` implementation as the core profile, which reports check results through the signed
event-ingestion endpoint. A failure callback reports the Airflow task failure through the same
endpoint, so a real Airflow failure enters the same investigation workflow.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

DW = os.environ.get("DATAWARDEN_BIN", "dw")


def report_failure(context) -> None:
    ti = context["ti"]
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [DW, "airflow-callback", "--dag-id", ti.dag_id, "--task-id", ti.task_id, "--run-id", context["run_id"],
         "--try-number", str(ti.try_number)],
        check=False, timeout=120,
    )


with DAG(
    dag_id="datawarden_retail_revenue",
    description="Ingest source batches, run dbt, run protected checks, report to DataWarden",
    schedule="@daily",
    start_date=datetime(2026, 8, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0, "on_failure_callback": report_failure},
    params={"replay_dates": ""},
    tags=["datawarden", "synthetic"],
) as dag:
    BashOperator(
        task_id="run_pipeline",
        bash_command=(
            f"{DW} pipeline --trigger airflow --emit --airflow-run-id '{{{{ run_id }}}}' "
            "{% if params.replay_dates %}--replay-dates '{{ params.replay_dates }}'{% endif %}"
        ),
    )
