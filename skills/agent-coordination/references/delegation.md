# Delegating

Choose the worker's mode explicitly, and state it on the first line of the
brief, exactly one of:

```text
Mode: independent. Handle: HANDLE.
Mode: supervised by HANDLE.
```

A worker in a project with `agent-board.conf` whose brief has no mode line
follows the independent protocol. Being a subagent never implies
supervision, and a worker does not inherit your conversation: its brief
must say everything it needs.

## Independent workers

For autonomous or long-lived work, self-directed scope changes, further
delegation, or workers that make Git changes themselves. The brief gives the
repository, the worktree and the board to use, a distinct handle, and
requires reading `.agents/skills/agent-coordination/SKILL.md` first. The
worker registers, claims its own files and branch under its own handle,
posts a handoff before `bye`, and returns a complete final message.

Create the branch or worktree first, and release any claim you took to set
it up, so the worker can claim it. Normal delegation needs neither a forced
takeover nor another agent's handle.

## Supervised workers

For narrowly scoped analysis or edits whose coordination you manage
yourself. The worker does not load this skill or use the board. Its brief
gives:

- the assigned checkout;
- the allowed paths and resources;
- the permitted actions;
- the report or check-in boundary;
- the sentence "Stay within this scope; ask the coordinator before
  expanding it."

The worker makes no board calls, commits, ref or index changes, or further
delegations, and uses no unassigned shared resource. It returns its edits
and validation results. Read-only workers need no claims, but their brief
still names the source or snapshot and forbids unassigned changes.

Your duties as its coordinator:

- Claim what it will change before launching it, and keep the claims.
  Record which worker and worktree uses each scope.
- Give your workers non-overlapping scopes: the board sees only your claims
  and cannot detect conflicts between them.
- Keep your presence current, read the board and relay what matters. A
  coordinator blocked on a foreground worker cannot renew its presence, and
  its claims go stale after 180 minutes: run long supervised work in the
  background, or keep it within the lease.
- Collect the result, confirm the worker has stopped writing, then commit
  under your own handle (`AGENT_BOARD_AGENT=HANDLE git -C WORKTREE commit
  ...`) and integrate. Never release a claim while the worker may still
  write.
- A Claude Code subagent inherits your environment, so an exported
  `AGENT_BOARD_AGENT` lends it your handle; only the brief stops a
  supervised worker from committing as you.

A worker that needs broader scope, reaches its check-in boundary without new
direction, or loses contact with you pauses shared writes and reports back.
You may reissue a bounded assignment, or transfer ownership explicitly and
switch the worker to independent mode, which requires it to read this
skill. Tool notices that a tool posts under its own handle, such as `ic2`,
are allowed from a supervised worker.
