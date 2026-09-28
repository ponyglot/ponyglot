.PHONY: help install check lint format test migrations-check

help:  ## Show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

install:  ## Install the package with development dependencies
	uv sync

check: lint migrations-check test  ## Everything CI would run

lint:  ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

format:  ## Apply ruff fixes and formatting
	uv run ruff check --fix .
	uv run ruff format .

migrations-check:  ## Fail if models lack migrations
	uv run python -m django makemigrations ponyglot --check --dry-run --settings=tests.settings

test:  ## Run the tests (SQLite, no services needed)
	uv run pytest
