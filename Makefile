.PHONY: test validate

test:
	python3 -m unittest plugins/claude-code/tests/test_plugin_scripts.py

validate:
	claude plugin validate . --strict
	claude plugin validate ./plugins/claude-code --strict
