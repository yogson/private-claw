.PHONY: start stop format lint install sync test

install:
	uv sync --all-extras

sync:
	uv sync --group dev

start:
	uv run uvicorn assistant.api.main:app --host 0.0.0.0 --port 8000
	# --reload

stop:
	@pids=$$(lsof -ti :8000 2>/dev/null); \
	if [ -n "$$pids" ]; then \
		kill -TERM $$pids 2>/dev/null || true; \
		echo "Stopped process(es) on port 8000."; \
	else \
		echo "No process listening on port 8000."; \
	fi

format:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

lint:
	uv run ruff check src/ tests/
	uv run mypy src/

test:
	uv run pytest tests/ -v
