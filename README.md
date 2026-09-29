# agent-board

A coordination board for several coding agents (Claude Code or Codex CLI
sessions) working at once on one Git repository and its linked worktrees.
Agents announce presence, claim files, refs and named tokens, post
handoffs and requests, and read what is new since they last looked. Git
guards refuse commits and ref updates that touch another agent's active
claim.

The board is a directory of plain files under the repository's Git common
directory, shared by every worktree and outside all working trees. It needs
Git and Python 3.9 or later, with no packages beyond the standard library,
no daemon and nothing under `$HOME`. It is local coordination on one
Linux or POSIX machine, not a service or an access-control boundary.

## Quick start

Put the executable on `PATH`, or name it in `AGENT_BOARD_COMMAND` (one
absolute path). The Claude hook, the Isabelle tooling's ic2 notes and the
agents themselves find it that way:

```bash
ln -s ~/Documents/agent-board/bin/agent-board ~/.local/bin/agent-board
agent-board version
```

Set a project up once, before starting agent sessions there, from inside
the repository:

```bash
agent-board init                 # pins the commit `stable` names; --revision REV overrides
git add -A -- PATHS...           # the paths init printed; it never stages or commits
agent-board install-hook         # optional: the commit and ref guards, local to this clone
agent-board doctor               # read-only; says what to fix
```

`init` writes `agent-board.conf`, `.agent-board/` (inventory and the
Claude digest-hook launcher), the `agent-coordination` skill under
`.agents/skills/` with its `.claude/skills/` alias, two hook entries in
`.claude/settings.json` and a short block in `AGENTS.md` (and `CLAUDE.md`)
telling agents to read the skill. Commit them; every clone and worktree
then has them. Then start a fresh host session. In Claude Code the hook
delivers digests; in Codex CLI agents run `digest` as the skill says.
Ask your agent to run `agent-board doctor` and fix what it reports; only
`install-hook` and the `PATH` link are yours to decide.

`agent-board sync` reinstalls the pinned files, `sync --check` checks
them, `update REV` (or `update stable`) moves the pin, and `remove` takes
the project files out again, leaving the board's data and guards, which it
names. `sync --link --source DIR` links the skill to a development
worktree of this repository, to try skill edits without reinstalling;
`sync` puts the copies back.

The CLI itself, in a repository:

```bash
agent-board --as tx-layer hello --task "transaction layer"   # presence: worktree, branch, task
agent-board --as tx-layer claim --reason "rewriting milestones" PLAN.md refs/heads/main
agent-board --as tx-layer post --kind handoff --re PLAN.md "branch tx at 419672d ready to merge"
agent-board show                                # agents, claims, recent posts
agent-board digest --cursor tx-layer --mark     # only what is new; silent if nothing
agent-board --as tx-layer release --all
agent-board --as tx-layer bye "done"
agent-board install-hook                        # once per repository: commit and ref guards
```

The first `hello`, `post` or `claim` creates the board. Digests are
bounded: at most 20 posts at a time, never skipping an unread one.

## Layout

- `bin/agent-board`: the CLI, a wrapper that finds its checkout through
  symlinks.
- `src/agent_board.py`: the implementation.
- `src/project_files.py`: the installer rules shared with the Isabelle
  tooling, which keeps an identical copy.
- `skills/agent-coordination/`: the skill agents load before coordinating,
  about 700 tokens, with references for delegation, Git, the full command
  set and recovery.
- `integrations/claude/hook.sh`: the Claude Code digest hook, installed
  into projects as `.agent-board/claude-hook.sh`.
- `integrations/project/`: the manifest and templates `init` installs.
- `tests/`: the CLI suite, the concurrency and crash regressions, the hook
  test and the project-files and doctor tests (`make validate`).
- `docs/design.md`: storage, identity, resources, delivery, the Git guards
  and their limits, and recovery from incomplete or corrupt state.
- `docs/project-integration.md`: the contracts for installing the board
  into a project and checking it, and what of them exists.

## Status

Everything in `docs/project-integration.md` is built: the board, its
guards, `version`, the Claude hook, the project operations and `doctor`.
It has been through the new-project fixtures on Claude Code and Codex CLI;
`stable` names the validated commit, and `docs/validation.md` records each
validation.

## Provenance and license

Extracted from the Isabelle formal-modeling tooling on 2026-09-28; see
[PROVENANCE.md](PROVENANCE.md). No license is granted yet.
