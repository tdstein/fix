SHELL := /bin/sh

PROJECT_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
INSTALL_DIR := $(HOME)/.local/bin
UV ?= uv
PYTHON ?= $(UV) run python

.PHONY: install uninstall test

install:
	$(UV) tool install --force --refresh "$(PROJECT_DIR)"

uninstall:
	-$(UV) tool uninstall fix
	rm -f "$(INSTALL_DIR)/fix" "$(INSTALL_DIR)/fix.py"

test:
	$(PYTHON) -m unittest discover -v
