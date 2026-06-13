.PHONY: test validate

PLUGIN_CREATOR_DIR ?= $(HOME)/Library/Mobile Documents/com~apple~CloudDocs/dotfiles/config/claude/skills/.system/plugin-creator
VALIDATOR_DEPS_DIR ?= /tmp/codex-plugin-validator-pyyaml

test:
	node --test plugins/claude-code/tests/runtime-node.test.mjs
	node --test plugins/codex/tests/runtime-node.test.mjs

validate:
	claude plugin validate . --strict
	claude plugin validate ./plugins/claude-code --strict
	PYTHONPATH="$(VALIDATOR_DEPS_DIR):$$PYTHONPATH" python3 -c "import yaml" >/dev/null 2>&1 || python3 -m pip install --quiet --upgrade --target "$(VALIDATOR_DEPS_DIR)" PyYAML
	PYTHONPATH="$(VALIDATOR_DEPS_DIR):$$PYTHONPATH" python3 "$(PLUGIN_CREATOR_DIR)/scripts/validate_plugin.py" ./plugins/codex
