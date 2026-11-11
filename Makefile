.PHONY: help install test serve up down logs tor-check clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[35m%-11s\033[0m %s\n", $$1, $$2}'

install:  ## Install in editable mode with dev + browser extras
	pip install -e ".[dev,browser]"
	playwright install chromium

test:  ## Run the test suite
	pytest -q

serve:  ## Run the web UI + API locally on 127.0.0.1:8000
	openonion serve

up:  ## Start the full stack (API + Tor sidecar + Postgres)
	docker compose up --build

down:  ## Stop the stack
	docker compose down

logs:  ## Follow stack logs
	docker compose logs -f

tor-check:  ## Verify the stack exits through Tor
	docker compose exec api openonion tor-check

clean:  ## Remove caches and build output
	rm -rf .pytest_cache build dist *.egg-info src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
