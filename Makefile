.PHONY: start format lint install sync test

install:
	uv sync --all-extras

sync:
	uv sync --group dev

start:
	uv run uvicorn assistant.api.main:app --host 0.0.0.0 --port 8000 --reload

format:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

lint:
	uv run ruff check src/ tests/
	uv run mypy src/

test:
	uv run pytest tests/ -v
