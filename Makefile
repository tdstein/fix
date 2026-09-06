SHELL := /bin/sh

# Preserve spaces in the path; GNU make 3.81 prefixes MAKEFILE_LIST with a space.
PROJECT_DIR := $(shell makefile="$(MAKEFILE_LIST)"; makefile=$${makefile\# }; cd "$$(dirname "$$makefile")" && pwd)
UV ?= uv
PYTHON ?= $(UV) run --locked python

.PHONY: install dev uninstall test

install:
	$(UV) tool install --force --refresh "$(PROJECT_DIR)"

dev:
	$(UV) tool install --force --editable "$(PROJECT_DIR)"

uninstall:
	$(UV) tool uninstall fix

test:
	cd "$(PROJECT_DIR)" && $(PYTHON) -m unittest discover -v
