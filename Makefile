SHELL := /bin/sh

UV ?= uv
PYTHON ?= $(UV) run --locked python

.PHONY: install dev uninstall test

install:
	$(UV) tool install --force --refresh .

dev:
	$(UV) tool install --force --editable .

uninstall:
	$(UV) tool uninstall fix

test:
	$(PYTHON) -m unittest discover -v
