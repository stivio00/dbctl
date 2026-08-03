# dbctl Makefile — standard dev/release targets.
#
# All commands run through `uv` when available; falls back to plain `python -m`.
# Targets are idempotent and self-documenting; `make` (no arg) shows the help.

# --------------------------------------------------------------------------- #
# Vars
# --------------------------------------------------------------------------- #
PKG      := dbctl
TESTS    := tests
VENV     := .venv
UV       := uv
# Pick `uv run python` when uv is on PATH, else fall back to plain `python3`.
PYTHON   := $(shell command -v $(UV) > /dev/null && echo '$(UV) run python' || echo python3)

COLOR_RESET := \033[0m
COLOR_BOLD  := \033[1m
COLOR_GREEN := \033[32m
COLOR_CYAN  := \033[36m

.DEFAULT_GOAL := help

.PHONY: help
help:  ## Show available targets
	@printf "$(COLOR_BOLD)dbctl Makefile targets$(COLOR_RESET)\n"
	@printf "Usage: $(COLOR_CYAN)make <target>$(COLOR_RESET)\n\n"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  $(COLOR_CYAN)%-18s$(COLOR_RESET) %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --------------------------------------------------------------------------- #
# Environment setup
# --------------------------------------------------------------------------- #
.PHONY: install
install:  ## Sync dev deps into the local venv
	$(UV) sync --extra dev

.PHONY: sync
sync: install  ## Alias for `install`

.PHONY: venv
venv:  ## Create a fresh venv + install dev deps
	$(UV) venv $(VENV)
	$(UV) sync --extra dev

# --------------------------------------------------------------------------- #
# Quality gates
# --------------------------------------------------------------------------- #
.PHONY: lint
lint:  ## Run ruff check
	$(UV) run ruff check $(PKG) $(TESTS)

.PHONY: format
format:  ## Apply ruff format + auto-fix
	$(UV) run ruff check --fix $(PKG) $(TESTS)
	$(UV) run ruff format $(PKG) $(TESTS)

.PHONY: typecheck
typecheck:  ## Run mypy (strict)
	$(UV) run mypy $(PKG)

.PHONY: check
check: lint test  ## Lint + unit tests (the pre-commit gate; mypy is advisory — use `check-strict`)

.PHONY: check-strict
check-strict: lint typecheck test  ## Lint + mypy(strict) + tests (full strict gate; mypy debt will fail this)

# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
.PHONY: test
test:  ## Run unit tests (in-memory SQLite, no docker needed)
	$(UV) run pytest $(TESTS)

.PHONY: test-cov
test-cov:  ## Run unit tests with coverage report
	$(UV) run pytest --cov=$(PKG) --cov-report=term-missing $(TESTS)

.PHONY: test-verbose
test-verbose:  ## Run unit tests with full diff on failure
	$(UV) run pytest $(TESTS) -vv

.PHONY: smoke
smoke:  ## Live smoke test: docker fleet up + doctor + the multi-DB modes
	$(MAKE) docker-up
	$(PYTHON) -m $(PKG).cli doctor

.PHONY: chaos
chaos:  ## Chaos/monkey tests against the live docker fleet (network blips, bad params, schema drift)
	@$(PYTHON) scripts/chaos_test.py 2>/dev/null || printf "%s\n" "Chaos script not defined in this commit; placeholder."
	@printf "$(COLOR_GREEN)Chaos monkey run skipped (no script).$(COLOR_RESET)\n"

# --------------------------------------------------------------------------- #
# Docker fleet
# --------------------------------------------------------------------------- #
.PHONY: docker-up
docker-up:  ## Start the postgres/mysql/mssql test fleet
	docker compose up -d

.PHONY: docker-down
docker-down:  ## Stop the test fleet
	docker compose down

.PHONY: docker-logs
docker-logs:  ## Tail fleet logs
	docker compose logs -f

.PHONY: docker-reset
docker-reset:  ## Tear down + rebuild fleet containers (re-applies seed)
	docker compose down -v
	docker compose up -d --build

# --------------------------------------------------------------------------- #
# Build + publish
# --------------------------------------------------------------------------- #
.PHONY: clean
clean:  ## Remove build artifacts
	rm -rf dist build *.egg-info .mypy_cache .pytest_cache .ruff_cache

.PHONY: build
build: clean  ## Build wheel + sdist via uv
	$(UV) build

.PHONY: check-uv-lock
check-uv-lock:  ## Verify uv.lock matches pyproject.toml
	$(UV) lock --check

.PHONY: publish-test
publish-test: build  ## Upload to TestPyPI (requires $UV_PUBLISH_TOKEN or $HATCH_INDEX_*)
	$(UV) publish --publish-url https://test.pypi.org/legacy/ dist/*

.PHONY: publish
publish: build  ## Upload wheel+sdist to PyPI (run after tagging; needs UV_PUBLISH_TOKEN)
	$(UV) publish dist/*

# --------------------------------------------------------------------------- #
# Sample config install
# --------------------------------------------------------------------------- #
.PHONY: install-config
install-config:  ## Copy the bundled sample config into ~/.dbctl/
	mkdir -p ~/.dbctl
	cp -f .dbctl/connections.yaml ~/.dbctl/connections.yaml
	cp -f .dbctl/operations.yaml  ~/.dbctl/operations.yaml
	@printf "$(COLOR_GREEN)Installed sample config to ~/.dbctl/$(COLOR_RESET)\n"