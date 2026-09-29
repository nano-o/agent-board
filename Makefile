SHELL := /bin/bash

SHELL_FILES := bin/agent-board integrations/claude/hook.sh \
	tests/cli_test.sh tests/claude_hook_test.sh tests/test_lib.sh

.PHONY: help validate

help:
	@echo "Targets:"
	@echo "  validate   Lint the shell files and run the CLI, regression and Claude hook tests"

validate:
	bash -n $(SHELL_FILES)
	shellcheck $(SHELL_FILES)
	python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])' src/agent_board.py
	PYTHONDONTWRITEBYTECODE=1 python3 tests/regression_test.py
	bash tests/cli_test.sh
	bash tests/claude_hook_test.sh
