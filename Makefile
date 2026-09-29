SHELL := /bin/bash

SHELL_FILES := bin/agent-board integrations/claude/hook.sh \
	tests/cli_test.sh tests/claude_hook_test.sh tests/test_lib.sh

.PHONY: help validate

help:
	@echo "Targets:"
	@echo "  validate   Lint the shell files and run the CLI, regression, project-files and Claude hook tests"

validate:
	bash -n $(SHELL_FILES)
	shellcheck $(SHELL_FILES)
	python3 -c 'import ast, sys; [ast.parse(open(f).read(), f) for f in sys.argv[1:]]' src/agent_board.py src/project_files.py
	python3 -c 'import json, sys; [json.load(open(f)) for f in sys.argv[1:]]' integrations/project/*.json
	PYTHONDONTWRITEBYTECODE=1 python3 tests/regression_test.py
	PYTHONDONTWRITEBYTECODE=1 python3 tests/project_test.py
	bash tests/cli_test.sh
	bash tests/claude_hook_test.sh
