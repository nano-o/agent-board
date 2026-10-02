# Provenance

agent-board was extracted on 2026-09-28 from the Isabelle tooling
repository `isabelle-formal-modeling-tooling` (a private repository,
`nano-o/isabelle-formal-modeling-tooling` on GitHub), at commit
`aa46d0c28ee08d7cc45db791ede4cef3c164debb`. The board's files last changed
there in `5b0f349` ("Fix coordination board concurrency, delivery, and ref
guards", 2026-09-17), and first appeared in `1aa0024` and `ef979bd`
(2026-09-16).

The first commit of this repository is the import itself: each file below
holds, unchanged, the content of its source path at `aa46d0c`, so the later
commits show every change made for standalone use.

- `bin/agent-board` from `scripts/board.sh`
- `src/agent_board.py` from `scripts/board.py`
- `tests/regression_test.py` from `tests/board_regression_test.py`
- `tests/cli_test.sh` from `tests/board_test.sh`
- `skills/agent-coordination/SKILL.md` from
  `extension/skills/isabelle-coordination/SKILL.md`
- `integrations/claude/hook.sh` from `extension/bin/board-hook.sh`
- `docs/design.md` from `docs/coordination-board.md`
- `docs/history/implementation-handoff-2026-09.md` from
  `docs/coordination-board-implementation-handoff.md`

`docs/project-integration.md`, added later, copies the shared rules and the
agent-board part of that repository's `docs/delivery-contracts.md` at the
same commit.

**Licenses.** At the import the source repository had no license file and
declared no license. Everything imported here was written for that
repository, in commits authored by Giuliano Losa, and contains no third-party
code; the board uses only Python's standard library and Git. Both
repositories are now licensed under the Apache License, Version 2.0
([LICENSE](LICENSE)).
