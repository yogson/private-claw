SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

.PHONY: start stop up down status format lint install sync test

install:
	uv sync --all-extras

sync:
	uv sync --group dev

# Single classic process (port 8000, ./data). Refused when <ASSISTANT_CONFIG_DIR>/instances.yaml
# exists: the launcher then manages every instance (make up / down / status).
start:
	@uv run python -m assistant.launcher check-single start
	@mkdir -p data/logs
	uv run uvicorn assistant.api.main:app --host 0.0.0.0 --port 8000 2>&1 | tee -a "data/logs/uvicorn-$$(date +%Y-%m-%d).log"
	# --reload

stop:
	@uv run python -m assistant.launcher check-single stop
	@pids=$$(lsof -ti :8000 2>/dev/null); \
	if [ -n "$$pids" ]; then \
		kill -TERM $$pids 2>/dev/null || true; \
		echo "Stopped process(es) on port 8000."; \
	else \
		echo "No process listening on port 8000."; \
	fi

# All instances from <ASSISTANT_CONFIG_DIR>/instances.yaml (see docs/DEVELOPMENT.md).
# Optional: NAME=<instance>.
up down status:
	.venv/bin/python -m assistant.launcher $@ $(NAME)

format:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

lint:
	uv run ruff check src/ tests/
	uv run mypy src/

test:
	uv run pytest tests/ -v
