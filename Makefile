.PHONY: venv install test test-pg lint run-ui run-api run-worker bootstrap check up down logs backup

venv:
	python3 -m venv .venv

install:
	. .venv/bin/activate && pip install -r requirements.txt && pip install -e .

test:
	. .venv/bin/activate && python -m pytest -q

test-pg:
	. .venv/bin/activate && MOTOTRADE_TEST_DATABASE_URL=$(PG_TEST_URL) python -m pytest -q

lint:
	. .venv/bin/activate && ruff check mototrade ui tests || true

bootstrap:
	. .venv/bin/activate && alembic upgrade head && python -m mototrade.cli bootstrap

check:
	. .venv/bin/activate && python -m mototrade.cli check

run-ui:
	. .venv/bin/activate && streamlit run ui/app.py

run-api:
	. .venv/bin/activate && uvicorn mototrade.api.app:app --reload --port 8000

run-worker:
	. .venv/bin/activate && python -m mototrade.cli worker --once

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

backup:
	docker compose exec db pg_dump -U $${POSTGRES_USER:-mototrade} -Fc $${POSTGRES_DB:-mototrade} > backups/manual_$$(date +%Y%m%d_%H%M%S).dump
