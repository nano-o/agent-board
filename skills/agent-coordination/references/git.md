# Git and the board

## The guards

`agent-board install-hook`, once per repository, installs a `pre-commit`
and a `reference-transaction` guard in the repository's own hooks
directory (`--force` keeps an existing hook and runs it first). The commit
guard checks the staged paths and the current branch; the ref guard checks
every ref Git reports during a prepared transaction, including fast-forward
merges, resets, rebases and `update-ref`. Both refuse a foreign active claim
and name its owner and reason. `agent-board doctor` reports whether both are
installed and current.

A guard takes the committer to be the one active agent registered for that
worktree. With two agents in one worktree, or when you commit in a worktree
where another agent is registered, pass your handle:
`AGENT_BOARD_AGENT=HANDLE git -C WORKTREE commit ...`.

## When a guard refuses

Wait for the release, or post a request to the owner. Do not force or take
over an active claim unless the human asked, and never bypass a guard
silently. `git commit --no-verify` skips the commit guard only, never the
ref guard.

A refused ref transaction can leave changes in the index or the working
tree. Inspect them and coordinate the recovery; never reset, clean or
discard somebody else's edits.

## Before moving refs

Before a merge, reset, rebase or history rewrite, run `guard` on the
affected paths and refs, and claim the branch you move. On Git 2.43 a branch
rename does not report its destination to the ref guard: guard and claim
**both** names before renaming. Direct ref rewrites still use an expected
old value. The guards check ownership at that moment; they do not lock a
whole Git or editing operation.

## What the guards do not cover

File edits, tokens and passages depend on agents claiming before they act.
Nothing stops an agent that ignores the board from editing a file, and the
guards do not protect editor buffers or other tools.
