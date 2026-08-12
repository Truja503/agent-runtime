.PHONY: help install test lint typecheck check run demo approve

PY ?= .venv/bin/python

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "%-12s %s\n", $$1, $$2}'

install:  ## Create a virtualenv and install the project with dev extras
	python3.12 -m venv .venv || python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

test:  ## Run the test suite
	$(PY) -m pytest -q

lint:  ## Run ruff
	.venv/bin/ruff check .

typecheck:  ## Run mypy
	.venv/bin/mypy

check: lint typecheck test  ## Everything CI runs

run:  ## Start the API on :8000
	.venv/bin/uvicorn --factory app.main:app --reload --port 8000

demo:  ## Run the offline end-to-end demonstration
	$(PY) scripts/demo.py

approve:  ## List privileged requests awaiting a human
	$(PY) -m privileged.cli list
