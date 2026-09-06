.PHONY: install dev serve test lint clean

install:
	python3 -m venv .venv && .venv/bin/pip install -e .

dev:
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev,rip]"

serve:
	.venv/bin/crate serve

test:
	.venv/bin/pytest

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache build dist *.egg-info
