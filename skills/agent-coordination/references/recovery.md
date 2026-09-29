# When something is wrong

## Incomplete or malformed board state

If a command reports "incomplete board state", "malformed authoritative
board state" or a bad format marker, stop and tell the human, quoting the
message. Never delete, rewrite, repair or recreate board files yourself:
what remains may be the only record of other agents' claims. The board's
directory is `agent-board path`; restoring it is the human's decision.

## A claim that looks abandoned

A stale claim (marked `[stale]` in `claims`) may be taken with a plain
`claim`; the takeover is posted. An active claim whose owner seems gone is
not yours to take: post a request to the owner and tell the human. Use
`--force` only when the human asked.

## Your own mistakes

- Lost track of your handle after compaction: `who` lists agents with
  their worktrees and tasks; pick yours by worktree and task, not a new one.
- Claimed too much: release what you do not need now.
- A digest repeated posts: delivery was interrupted, which repeats rather
  than loses posts.

## The setup

`agent-board doctor` checks the executable, the project files, the board's
storage and the Git guards, read-only, and says what to run. Report its
failures to the human rather than editing hooks or project files yourself.
