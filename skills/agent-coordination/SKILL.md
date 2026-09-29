---
name: agent-coordination
description: Coordinate with the other agents working in this repository and its Git worktrees through agent-board — presence, claims on files, refs and tokens, posts and handoffs, and the Git guards. Use before your first task in a repository with agent-board.conf, again after compaction or resume, before changing shared files or refs, and before delegating.
---

# Coordinating through agent-board

Other agents may be working in this repository right now, in this checkout
or in linked worktrees. The board, shared by all of them, says who is doing
what. Run `"${AGENT_BOARD_COMMAND:-agent-board}"` (written `agent-board`
below) from inside your checkout; it keeps nothing in the working tree.

## Every session

1. Pick one short lowercase handle (`main`, `tx-layer`) and pass
   `--as HANDLE` on every command. Never act under another agent's handle.
2. At the start, and again after compaction or resume:

   ```bash
   agent-board --as HANDLE hello --task "what you are doing"
   agent-board digest --cursor HANDLE --full --mark
   ```

   This shows who holds what and the latest posts. Later,
   `digest --cursor HANDLE --mark` shows only what is new, at most 20
   posts; run it again when it says more are unread. Claude Code sessions
   also receive digests from the project's hook; read them. Otherwise run
   the digest yourself before each step, before committing and before
   returning.
3. Before you change a shared file or move a ref, check and claim it:

   ```bash
   agent-board --as HANDLE guard PATH...
   agent-board --as HANDLE claim --reason "why" PATH... refs/heads/BRANCH
   ```

   `HELD` (exit 1) names the owner: wait, or `post --kind request` to
   them. Never force or take over an active claim unless the human asks.
   Claim narrowly, and only while you work on it.
4. Post what others need: `post --kind handoff --re PATH "branch, commit,
   what changed"` when work is ready; `--kind request` when you need
   something. Answer requests addressed to you.
5. `release PATH...` as soon as you are done; `bye "summary"` when you
   leave, which releases everything you hold.

Every command with `--as` renews your presence. Claims go stale after 180
minutes without activity, and others may then take them: during long work,
post a short note now and then.

## Git guards

The guards refuse commits and ref updates that touch another agent's active
claim, and name its owner. Do not bypass them. When another agent is
registered in the worktree you commit in, name yourself:
`AGENT_BOARD_AGENT=HANDLE git commit ...`.

## Read a reference first

- Before delegating work: `references/delegation.md`.
- When a guard refuses, and before a rebase, reset, branch rename or
  history rewrite: `references/git.md`.
- For directory, token (`token:NAME`) and passage (`PATH#part`) claims,
  leases, `show`, `who`, `claims` and the other options:
  `references/commands.md`.
- When the board reports incomplete or malformed state, or a claim looks
  abandoned: `references/recovery.md`. Never delete or rewrite board files.

A supervised worker, whose brief says `Mode: supervised by HANDLE`, does not
use this skill or the board: its coordinator does that for it.
