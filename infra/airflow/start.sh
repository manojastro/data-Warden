#!/usr/bin/env bash
# Write the simple-auth-manager password file from the generated secret, then start Airflow.
set -euo pipefail
mkdir -p /artifacts/airflow
python - <<'PY'
import json, os
user = os.environ.get("DW_AIRFLOW_USERNAME") or "datawarden"
json.dump({user: os.environ["DW_AIRFLOW_PASSWORD"]}, open("/artifacts/airflow/passwords.json", "w"))
PY
chmod 600 /artifacts/airflow/passwords.json
exec airflow standalone
