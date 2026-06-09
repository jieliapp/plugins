.PHONY: test validate

PLUGIN_CREATOR_DIR := /Users/alice/Library/Mobile Documents/com~apple~CloudDocs/dotfiles/config/claude/skills/.system/plugin-creator
VALIDATOR_DEPS_DIR ?= /tmp/codex-plugin-validator-pyyaml

test:
	python3 -m unittest plugins/claude-code/tests/test_plugin_scripts.py
	python3 -m unittest plugins/codex/tests/test_plugin_scripts.py

validate:
	claude plugin validate . --strict
	claude plugin validate ./plugins/claude-code --strict
	PYTHONPATH="$(VALIDATOR_DEPS_DIR):$$PYTHONPATH" python3 -c "import yaml" >/dev/null 2>&1 || python3 -m pip install --quiet --upgrade --target "$(VALIDATOR_DEPS_DIR)" PyYAML
	PYTHONPATH="$(VALIDATOR_DEPS_DIR):$$PYTHONPATH" python3 "$(PLUGIN_CREATOR_DIR)/scripts/validate_plugin.py" ./plugins/codex
