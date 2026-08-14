SHELL := /bin/sh

PROJECT_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
INSTALL_DIR := $(HOME)/.local/bin
FIX_PATH := $(INSTALL_DIR)/fix

.PHONY: install uninstall

install:
	mkdir -p "$(INSTALL_DIR)"
	install -m 755 "$(PROJECT_DIR)fix.py" "$(FIX_PATH)"

uninstall:
	rm -f "$(FIX_PATH)" "$(INSTALL_DIR)/fix.py"
