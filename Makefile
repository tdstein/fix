SHELL := /bin/sh

PROJECT_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
INSTALL_DIR := $(HOME)/.local/bin
FIX_PATH := $(INSTALL_DIR)/fix
MODULE_PATH := $(INSTALL_DIR)/fix.py

.PHONY: install uninstall

install:
	mkdir -p "$(INSTALL_DIR)"
	install -m 755 "$(PROJECT_DIR)fix" "$(FIX_PATH)"
	install -m 644 "$(PROJECT_DIR)fix.py" "$(MODULE_PATH)"

uninstall:
	rm -f "$(FIX_PATH)" "$(MODULE_PATH)"
