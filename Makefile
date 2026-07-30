PYTHON ?= python3
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python

.PHONY: setup test typecheck lint check

setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -e ".[dev]"

test:
	$(VENV_PYTHON) -m pytest

typecheck:
	$(VENV_PYTHON) -m mypy

lint:
	$(VENV_PYTHON) -m ruff check .

check: lint typecheck test

