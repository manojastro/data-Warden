# DataWarden developer commands. Run `make help`.
SHELL := /bin/bash
UV ?= uv
COMPOSE ?= docker compose -f infra/docker-compose.yml --env-file .env
PROFILE ?= core
# Run `dw` inside the Compose worker when the stack is up, otherwise on the host with uv.
IN_DOCKER := $(shell docker compose -f infra/docker-compose.yml --env-file .env ps --status running -q worker 2>/dev/null)
ifeq ($(strip $(IN_DOCKER)),)
DW = $(UV) run dw
else
DW = $(COMPOSE) exec -T worker dw
endif

.PHONY: help setup setup-env up down seed pipeline checks demo test test-fast e2e eval lint fmt fault-list fault reset api worker web local-db

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

setup-env: ## Create/refresh .env with generated secrets (keeps existing values)
	python3 scripts/gen_env.py

setup: setup-env ## Create .env, install Python + web dependencies
	$(UV) sync
	cd apps/web && npm ci

up: ## Start the stack with Docker Compose (PROFILE=core|full|observability)
	@if [ "$(PROFILE)" = "full" ]; then export DW_AIRFLOW_BASE_URL=http://airflow:8080; fi; \
	if [ "$(PROFILE)" = "observability" ]; then export DW_OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318; fi; \
	$(COMPOSE) --profile $(PROFILE) up -d --build --wait

down: ## Stop the Docker Compose stack
	$(COMPOSE) --profile full --profile observability down

local-db: ## Create databases, roles and schemas on the configured PostgreSQL (host mode)
	$(UV) run dw setup-db
	$(UV) run alembic upgrade head

seed: ## Regenerate synthetic sources and rebuild the warehouse (synthetic only)
	$(DW) seed
	$(DW) app-seed

pipeline: ## Run the pipeline once and report failing checks to the API
	$(DW) pipeline --emit

checks: ## Run the protected quality checks
	$(DW) checks --all

fault-list: ## List demo fault scenarios
	$(DW) fault list

fault: ## Inject a demo fault: make fault SCENARIO=duplicate_payments
	$(DW) fault inject $(SCENARIO)

reset: ## Reset demo faults (synthetic data only) and rebuild canonical models
	$(DW) fault reset

demo: ## Scripted demo: duplicate fault -> investigation -> approval -> recovery
	$(DW) demo

api: ## Run the API locally (host mode)
	$(UV) run uvicorn datawarden.api.main:app --host 0.0.0.0 --port 8000

worker: ## Run the worker locally (host mode)
	$(UV) run dw worker

web: ## Run the dashboard dev server
	cd apps/web && npm run dev

test: ## Run all Python tests (unit + integration + scenarios; needs PostgreSQL)
	$(UV) run pytest

test-fast: ## Unit tests only (no database)
	$(UV) run pytest -m "not integration"

e2e: ## Playwright journey against a running stack
	cd apps/web && npx playwright test

eval: ## Benchmark fixed seeds across fault scenarios; writes docs/EVALUATION.md
	$(DW) eval

lint: ## Ruff + TypeScript checks
	$(UV) run ruff check src tests scripts
	$(UV) run ruff format --check src tests scripts
	cd apps/web && npm run lint

fmt:
	$(UV) run ruff format src tests scripts
	$(UV) run ruff check --fix src tests scripts
