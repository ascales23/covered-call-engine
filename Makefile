VENV ?= .venv
PY := $(VENV)/bin/python

.PHONY: install test verify run dry clean

install:
	python3 -m venv $(VENV)
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

verify:
	$(PY) main.py --verify

dry:
	$(PY) main.py --no-orders -v

run:
	$(PY) main.py

clean:
	rm -f covered_call.sqlite
	rm -rf __pycache__ */__pycache__ .pytest_cache
