# AutoRentLedger developer and local-operation shortcuts.
# Override variables at invocation time, for example:
#   make web PORT=8080
#   make property-cash PERIOD=2026-10 PROPERTY=2

VENV ?= .venv
DATABASE ?= data/autorentledger.db
HOST ?= 127.0.0.1
PORT ?= 8000
PERIOD ?=
PROPERTY ?=
VENV_CREATED = $(VENV)/.created
VENV_INSTALLED = $(VENV)/.installed

ifeq ($(OS),Windows_NT)
BOOTSTRAP_PYTHON ?= py -3
PYTHON ?= $(VENV)/Scripts/python.exe
else
BOOTSTRAP_PYTHON ?= python3
PYTHON ?= $(VENV)/bin/python
endif

CLI = "$(PYTHON)" -m autorentledger.cli

.DEFAULT_GOAL := help

.PHONY: help python-check venv install setup web-config start db-status db-upgrade db-check \
	backup web run sync daily overview property-cash expenses lint fix format test check ci

help:
	@echo "AutoRentLedger"
	@echo "First time:"
	@echo "  make setup                         Create/install the environment and upgrade/check the DB"
	@echo "  make web-config                    Securely create the ignored local web configuration"
	@echo "Normal use:"
	@echo "  make start                         Check the DB and start the authenticated loopback UI"
	@echo "  make daily                         Run the backed-up daily workflow"
	@echo "Checks and safety:"
	@echo "  make check                         Run Ruff and the full test suite"
	@echo "  make db-check                      Check database health without changing it"
	@echo "  make backup                        Create a verified database backup"
	@echo "Other shortcuts:"
	@echo "  make sync | overview PERIOD=YYYY-MM | property-cash PERIOD=YYYY-MM"
	@echo "  make db-status | db-upgrade | web | expenses"
	@echo "  make lint | fix | format | test"

python-check:
	$(BOOTSTRAP_PYTHON) -c "import sys; sys.exit('AutoRentLedger requires Python 3.11 or newer.') if sys.version_info < (3, 11) else None"

$(VENV_CREATED): | python-check
	$(BOOTSTRAP_PYTHON) -m venv "$(VENV)"
	$(BOOTSTRAP_PYTHON) -c "from pathlib import Path; Path(r'$@').touch()"

$(VENV_INSTALLED): $(VENV_CREATED) pyproject.toml
	"$(PYTHON)" -m pip install --upgrade pip
	"$(PYTHON)" -m pip install -e ".[dev]"
	"$(PYTHON)" -c "from pathlib import Path; Path(r'$@').touch()"

venv: $(VENV_CREATED)

install: $(VENV_INSTALLED)

setup: install
	$(CLI) db upgrade --database "$(DATABASE)"
	$(CLI) db check --database "$(DATABASE)"

web-config: $(VENV_INSTALLED)
	$(CLI) web-config

start: $(VENV_INSTALLED)
	$(CLI) db check --database "$(DATABASE)"
	$(CLI) web --database "$(DATABASE)" --host "$(HOST)" --port "$(PORT)"

db-status:
	$(CLI) db status --database "$(DATABASE)"

db-upgrade:
	$(CLI) db upgrade --database "$(DATABASE)"

db-check:
	$(CLI) db check --database "$(DATABASE)"

backup:
	$(CLI) db backup --database "$(DATABASE)"

web:
	$(CLI) web --database "$(DATABASE)" --host "$(HOST)" --port "$(PORT)"

run: start

sync:
	$(CLI) sync --database "$(DATABASE)"

daily:
	$(CLI) daily --database "$(DATABASE)"

overview:
	$(if $(strip $(PERIOD)),,$(error PERIOD is required; use make overview PERIOD=YYYY-MM))
	$(CLI) overview --database "$(DATABASE)" --period "$(PERIOD)"

property-cash:
	$(if $(strip $(PERIOD)),,$(error PERIOD is required; use make property-cash PERIOD=YYYY-MM))
	$(CLI) property-cash --database "$(DATABASE)" --period "$(PERIOD)" $(if $(strip $(PROPERTY)),--property "$(PROPERTY)",)

expenses:
	$(CLI) expenses --database "$(DATABASE)"

lint:
	"$(PYTHON)" -m ruff check .

fix:
	"$(PYTHON)" -m ruff check . --fix

format:
	"$(PYTHON)" -m ruff format .

test:
	"$(PYTHON)" -m pytest -q

check: lint test

ci: check
