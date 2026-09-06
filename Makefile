SHELL := /bin/sh

# Preserve spaces in the path; GNU make 3.81 prefixes MAKEFILE_LIST with a space.
PROJECT_DIR := $(shell makefile="$(MAKEFILE_LIST)"; makefile=$${makefile\# }; cd "$$(dirname "$$makefile")" && pwd)
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
