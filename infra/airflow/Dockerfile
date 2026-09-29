# Airflow (full profile). DataWarden is installed into a separate virtualenv so its dependency
# pins (SQLAlchemy 2.x, dbt) never conflict with Airflow's own. DAG tasks call the same `dw`
# pipeline CLI that the core profile uses.
FROM apache/airflow:3.1.0-python3.11
USER root
# git (runtime workspace) and curl (health check) are already part of the official image.
COPY infra/requirements.lock.txt /opt/datawarden/infra/requirements.lock.txt
# Optional build secret `extra_ca` (see infra/Dockerfile).
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca SSL_CERT_FILE=/run/secrets/extra_ca REQUESTS_CA_BUNDLE=/run/secrets/extra_ca; fi; \
    python -m venv /opt/datawarden-venv \
 && /opt/datawarden-venv/bin/pip install --no-cache-dir -r /opt/datawarden/infra/requirements.lock.txt
COPY pyproject.toml README.md /opt/datawarden/
COPY src /opt/datawarden/src
COPY pipelines /opt/datawarden/pipelines
COPY data /opt/datawarden/data
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca SSL_CERT_FILE=/run/secrets/extra_ca REQUESTS_CA_BUNDLE=/run/secrets/extra_ca; fi; \
    /opt/datawarden-venv/bin/pip install --no-cache-dir --no-deps -e /opt/datawarden \
 && chown -R 1000:0 /opt/datawarden
COPY pipelines/airflow/dags /opt/airflow/dags
ENV DW_REPO_ROOT=/opt/datawarden DW_ARTIFACT_DIR=/artifacts DATAWARDEN_BIN=/opt/datawarden-venv/bin/dw
USER airflow
