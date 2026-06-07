.PHONY: test validate

test:
	python3 -m unittest claude-code/tests/test_plugin_scripts.py

validate:
	claude plugin validate ./claude-code --strict
