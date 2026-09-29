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
absolute path):

```bash
ln -s ~/Documents/agent-board/bin/agent-board ~/.local/bin/agent-board
agent-board version
```

In a repository:

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

The first `hello`, `post` or `claim` creates the board. Agents learn the
protocol from the skill in `skills/agent-coordination/`. Claude Code
sessions get digests from the hook in `integrations/claude/`; Codex agents
run `digest` themselves.

## Layout

- `bin/agent-board`: the CLI, a wrapper that finds its checkout through
  symlinks.
- `src/agent_board.py`: the implementation.
- `skills/agent-coordination/`: the skill agents load before coordinating.
- `integrations/claude/hook.sh`: the Claude Code digest hook.
- `tests/`: the CLI suite, the concurrency and crash regressions, and the
  hook test (`make validate`).
- `docs/design.md`: storage, identity, resources, delivery, the Git guards
  and their limits, and recovery from incomplete or corrupt state.
- `docs/project-integration.md`: the contracts for installing the board
  into a project and checking it, and what of them exists.

## Status

The board, its guards, `version` and the Claude hook work. Installing the
board's project files (`init`, `sync`, `update`, `remove`) and a read-only
`doctor` are specified in `docs/project-integration.md` and not built yet.
Until then, a project uses the board through the commands above, and an
agent reads the skill from this checkout.

## Provenance and license

Extracted from the Isabelle formal-modeling tooling on 2026-09-28; see
[PROVENANCE.md](PROVENANCE.md). No license is granted yet.
