help: ## Shows this help
	@echo "$$(grep -h '#\{2\}' $(MAKEFILE_LIST) | sed 's/: #\{2\} /	/' | column -t -s '	')"

install: ## Install the project for local dev
	uv sync --upgrade
	uv tool install --editable --reinstall .

lint: ## Check project linting rules
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
