# Makefile for smpte-leader-gen
#
# Uses the project virtualenv interpreter so targets are reproducible
# regardless of which Python is on the caller's PATH.

PYTHON ?= .venv/bin/python
PIP := $(PYTHON) -m pip

.DEFAULT_GOAL := verify

.PHONY: help install verify test acceptance clean

help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## Editable install with dev extras into the project venv.
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

verify: ## Run the full test suite.
	$(PYTHON) -m pytest -q

test: verify ## Alias for 'verify'.

acceptance: ## Render every profile + rate and assert ffprobe properties (needs ffmpeg).
	$(PYTHON) scripts/acceptance.py

clean: ## Remove caches and build artifacts.
	rm -rf .pytest_cache build dist *.egg-info smpte_leader_gen/__pycache__ tests/__pycache__
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
