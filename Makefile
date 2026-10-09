# Everyday tasks. Run `make` to list them.
# The development targets need uv (https://docs.astral.sh/uv/); the Docker targets need Docker.

UV ?= uv
COMPOSE ?= docker compose
TEST_IMAGE ?= udp-multicast-proxy:test

.DEFAULT_GOAL := help
.PHONY: help all install lock upgrade format lint typecheck test check run \
	build up down restart logs status test-image test-docker test-integration clean

help: ## List the targets
	@awk 'BEGIN {FS = ":.*## "} \
		/^##@/ {printf "\n%s\n", substr($$0, 5)} \
		/^[a-z-]+:.*## / {printf "  %-17s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

all: check test-docker build ## Everything: local checks, the full Docker test suite, the service image

##@ Development

install: ## Create .venv with the locked dependencies
	$(UV) sync --locked

lock: ## Update uv.lock after changing dependencies in pyproject.toml
	$(UV) lock

upgrade: ## Upgrade every dependency in uv.lock to its latest version
	$(UV) lock --upgrade

format: ## Format the code
	$(UV) run ruff format

lint: ## Check formatting and lint
	$(UV) run ruff format --check
	$(UV) run ruff check

typecheck: ## Type check (mypy, strict)
	$(UV) run mypy

test: ## Run the unit and component tests (fast, no ffmpeg needed)
	$(UV) run pytest

check: lint typecheck test ## Lint, type check and test

run: ## Run the proxy locally (needs ffmpeg, ORIGINAL_M3U_URL and HOST_IP)
	$(UV) run udp-multicast-proxy

##@ Docker

build: ## Build the service image
	$(COMPOSE) build

up: ## Build and start the service in the background
	$(COMPOSE) up -d --build

down: ## Stop and remove the service
	$(COMPOSE) down

restart: ## Restart the service
	$(COMPOSE) restart

logs: ## Follow the service logs
	$(COMPOSE) logs -f

status: ## Show the service status
	$(COMPOSE) ps

test-image: ## Build the test image (lints and type checks while building)
	docker build --target test -t $(TEST_IMAGE) .

test-docker: test-image ## Run every test, including the end-to-end tests with real ffmpeg
	docker run --rm $(TEST_IMAGE)

test-integration: test-image ## Run only the end-to-end tests
	docker run --rm $(TEST_IMAGE) .venv/bin/pytest -m integration

##@ Housekeeping

clean: ## Remove tool caches and the locally generated playlist (keeps .venv)
	rm -rf .pytest_cache .mypy_cache .ruff_cache data
	find . -path ./.venv -prune -o -type d -name __pycache__ -prune -exec rm -rf {} +
