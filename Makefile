# Convenience targets. On this macOS dev machine the Homebrew Python has a broken
# pyexpat (see README) — DYLD_LIBRARY_PATH works around it. Harmless elsewhere;
# remove once `brew reinstall python@3.14` is done.
export DYLD_LIBRARY_PATH := /opt/homebrew/opt/expat/lib

PY := .venv/bin/python

.PHONY: install test serve seed mock demo clean

install:
	python3 -m venv .venv && $(PY) -m pip install -r requirements.txt

test:
	$(PY) -m pytest

serve:
	$(PY) -m uvicorn server.main:app --port 8000 --reload

seed:
	$(PY) -m tools.seed_employees --count 60 --reset

mock:
	$(PY) -m tools.mock_punch_source --period 2026-08 --days 28 --twice

# Full offline walk-through: seed -> push punches -> recompute -> show queue ->
# draft export -> attempt (blocked) final export. Needs `make serve` running.
demo: seed mock
	$(PY) -m tools.manage recompute  --period 2026-08
	$(PY) -m tools.manage exceptions --period 2026-08
	$(PY) -m tools.manage export     --period 2026-08 --kind draft
	-$(PY) -m tools.manage export    --period 2026-08 --kind final

clean:
	rm -f data/*.db data/*.db-wal data/*.db-shm
	rm -rf data/exports __pycache__ .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
