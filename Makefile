# DataWarden developer commands. Run `make help`.
SHELL := /bin/bash
UV ?= uv
COMPOSE ?= docker compose -f infra/docker-compose.yml --env-file .env
PROFILE ?= core

.PHONY: help setup up down seed pipeline checks demo test test-fast e2e eval lint fmt fault-list fault reset api worker web local-db

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

setup: ## Create .env with generated secrets, install Python + web dependencies
	python3 scripts/gen_env.py
	$(UV) sync
	cd apps/web && npm ci

up: ## Start the stack with Docker Compose (PROFILE=core|full|observability)
	$(COMPOSE) --profile $(PROFILE) up -d --build --wait

down: ## Stop the Docker Compose stack
	$(COMPOSE) --profile full --profile observability down

local-db: ## Create databases, roles and schemas on the configured PostgreSQL (host mode)
	$(UV) run dw setup-db
	$(UV) run alembic upgrade head

seed: ## Regenerate synthetic sources and rebuild the warehouse (synthetic only)
	$(UV) run dw seed
	$(UV) run dw app-seed

pipeline: ## Run the pipeline once and report failing checks to the API
	$(UV) run dw pipeline --emit

checks: ## Run the protected quality checks
	$(UV) run dw checks --all

fault-list: ## List demo fault scenarios
	$(UV) run dw fault list

fault: ## Inject a demo fault: make fault SCENARIO=duplicate_payments
	$(UV) run dw fault inject $(SCENARIO)

reset: ## Reset demo faults (synthetic data only) and rebuild canonical models
	$(UV) run dw fault reset

demo: ## Scripted demo: duplicate fault -> investigation -> approval -> recovery
	$(UV) run dw demo

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
	$(UV) run dw eval

lint: ## Ruff + TypeScript checks
	$(UV) run ruff check src tests scripts
	$(UV) run ruff format --check src tests scripts
	cd apps/web && npm run lint

fmt:
	$(UV) run ruff format src tests scripts
	$(UV) run ruff check --fix src tests scripts
