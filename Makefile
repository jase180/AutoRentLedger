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

ifeq ($(OS),Windows_NT)
BOOTSTRAP_PYTHON ?= py -3.11
PYTHON ?= $(VENV)/Scripts/python.exe
else
BOOTSTRAP_PYTHON ?= python3.11
PYTHON ?= $(VENV)/bin/python
endif

CLI = "$(PYTHON)" -m autorentledger.cli

.DEFAULT_GOAL := help

.PHONY: help venv install setup db-status db-upgrade db-check backup web run \
	sync daily overview property-cash expenses lint format test check ci

help:
	@echo "AutoRentLedger commands"
	@echo "  make setup                         Create venv, install dev dependencies, upgrade/check DB"
	@echo "  make web                           Start read-only UI at http://127.0.0.1:8000"
	@echo "  make web PORT=8080                 Start UI on another loopback port"
	@echo "  make db-status | db-upgrade | db-check | backup"
	@echo "  make sync | daily                  Run normal evidence workflows"
	@echo "  make overview PERIOD=YYYY-MM       Show the monthly rent overview"
	@echo "  make property-cash PERIOD=YYYY-MM [PROPERTY=ID]"
	@echo "  make expenses                      List active Property expenses"
	@echo "  make lint | format | test | check  Developer verification"

venv:
	$(BOOTSTRAP_PYTHON) -m venv "$(VENV)"

install: venv
	"$(PYTHON)" -m pip install --upgrade pip
	"$(PYTHON)" -m pip install -e ".[dev]"

setup: install
	$(CLI) db upgrade --database "$(DATABASE)"
	$(CLI) db check --database "$(DATABASE)"

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

run: web

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

format:
	"$(PYTHON)" -m ruff check . --fix

test:
	"$(PYTHON)" -m pytest -q

check: lint test

ci: check
