.PHONY: install dev api ui protocol check

API_PORT ?= 8710
UI_PORT ?= 5173

install:
	uv sync
	pnpm --dir web install

dev:
	$(MAKE) -j2 api ui

api:
	uv run --env-file .env uvicorn infonium.api:app --reload --port $(API_PORT)

ui:
	API_PORT=$(API_PORT) pnpm --dir web dev --port $(UI_PORT)

protocol:
	uv run infonium-protocol > protocol/openapi.json
	pnpm --dir web exec openapi-typescript ../protocol/openapi.json -o src/protocol.d.ts

check:
	uv run ruff check
	uv run ruff format --check
	uv run mypy
	pnpm --dir web exec tsc --noEmit
	uv run infonium-protocol | diff - protocol/openapi.json
	pnpm --dir web exec openapi-typescript ../protocol/openapi.json | diff - web/src/protocol.d.ts
