---
name: isabelle-coordination
description: Coordinate several agents (Claude Code or Codex CLI sessions) working at once on one repository and its Git worktrees through the shared coordination board (scripts/board.sh) — presence, posts, claims on files, refs and the jEdit worktree, handoffs, and the commit/ref guards. Use when more than one agent shares a repository, when asked to "post to the board", "claim", "hand off", or before editing a shared file such as the project plan.
---

# Coordinating through the board

Several agents may work on one repository at the same time: one in the main
checkout that jEdit owns (Isabelle/IQ), others in linked worktrees with their
own ic2 servers, on either host. The board is where they see each other. It
is a directory of plain files in the repository's Git common directory,
shared by every worktree, driven by one script:

```bash
"$ISABELLE_TOOLING_ROOT/scripts/board.sh" --as <handle> ACTION ...
"$ISABELLE_TOOLING_ROOT/scripts/board.sh" --help
```

Run it from inside the checkout or worktree you work in, or pass
`--project-root DIR`. Read-only actions (`who`, `claims`, `show`, `digest`,
`guard`, `path`) need no handle. Nothing it writes lands in a working tree or
under `$HOME`; the board and its posts stay local to the machine.

## Your handle

Pick a short lowercase handle for the session, by convention the worktree or
the topic (`main`, `tx-layer`, `env-repair`), and pass it as `--as` on every
action that should renew your presence. `ISABELLE_BOARD_AGENT` does the same
where the host keeps environment across commands; Claude Code's shell does not, so `--as` is the
reliable form. The Git guards infer the agent from the worktree;
two agents sharing one worktree therefore commit as
`ISABELLE_BOARD_AGENT=<handle> git commit ...`.

## When to read the board

- At the start of a task: `digest --cursor <handle> --mark`, or `show` for
  the whole picture. Claude Code sessions get the digest injected by the
  extension's hooks at session start and before each prompt; Codex CLI
  sessions run it themselves at these moments.
- Before editing a file another agent might edit (the project plan, a shared
  theory, a README): `guard PATH` says whether it is claimed and by whom.
- Before moving a ref (rebase, history rewrite, merge into `main`) and
  before merging a branch.
- Before returning or ending, once more, so a request addressed to you does
  not go unanswered.

## When to write

1. `hello --task "..."` when you begin: one line saying what you do. It
   records your worktree and branch so others can find your work.
2. `claim --reason "..." RESOURCE...` before you edit a shared file,
   rewrite a branch, or take over the jEdit worktree. Resources are paths
   relative to the invocation directory (or `--project-root`). `formal/`
   covers a directory, even before creation; the worktree root itself covers
   the whole worktree. Paths cannot escape that root. Other resources are
   refs (`refs/heads/main`), the token `jedit` for the I/Q
   editing session in the main worktree, or `path#passage` for one part of
   a file, which is shown to others but never enforced, so two agents can
   hold different passages of one theory and both commit.
3. `post` for what others need to know. `--kind handoff` when a branch is
   ready: branch, commit, what changed, what the merge does to files open in
   jEdit. `--kind request` when you need something from a named agent.
   `--kind done` when a milestone lands. `--re RESOURCE` ties the post to a
   file or ref; a plain `post` is a note; `post -` reads a longer body from
   stdin.
4. `release RESOURCE...` or `release --all` as soon as you are done;
   `bye "..."` when you leave, which releases everything you hold.

Keep posts short and specific: what, where (path, branch, commit), and what
the reader should do. The board is not the decision log. Decisions and
milestones still go to the project's plan file, and the agent who posts
"ready to merge" carries them over.

## The guard

`install-hook`, once per repository, installs shared `pre-commit` and
`reference-transaction` hooks (`--force` preserves and chains existing hooks).
The commit hook checks staged paths and the current branch. The ref hook
checks every ref Git reports during a prepared transaction, including
fast-forward merges, resets, rebases, and `update-ref`. They reject foreign
active claims and name the owner and reason. Wait for release or coordinate
with a post. Do not bypass or force an active claim unless the human asked.
`git commit --no-verify` skips the commit hook only; it does not bypass the
ref guard. Never bypass silently.

Before edits, merges, resets or rebases, explicitly `guard` the affected
paths and refs. Rejection of a ref transaction can leave changes in the
index or working tree; inspect them and coordinate recovery, without
resetting, cleaning or discarding somebody else's edits. On Git 2.43, branch
rename does not report the destination to the ref hook: guard **both** names
before renaming. The `jedit` token and fragments require cooperation; hooks
do not protect editor buffers. Direct ref rewrites still use an expected old
value. Guards check current ownership; they do not lock an entire Git or
editing operation.

Claims are acquired as one atomic batch. An active directory claim conflicts
with a file beneath it. Stale overlapping claims are retired on takeover, so
later activity by their old owners cannot revive them. Fragment claims remain
advisory. `token:NAME` names other tokens; `path:jedit` claims a file literally
named `jedit`.

A claim goes stale after `ISABELLE_BOARD_STALE_MINUTES` (default 180)
without board activity by its owner. `hello` and `claim` establish presence;
other valid actions with `--as` renew existing presence, including `guard`,
`who`, `claims` and failed conflict checks. Invalid arguments do not renew.
Anonymous reads do not renew anyone. A Git guard renews only its uniquely
inferred active agent; it cannot infer a stale owner. `bye` removes presence
and releases claims. Hook installation and migration are maintenance, not
heartbeats. Post occasionally during long proof jobs to retain your leases.

`digest --mark` emits all unread posts and advances only after successful
output; interrupted delivery can repeat posts. `--full` replays all posts.
If the script reports a legacy board, follow the stopped-writer migration in
`$ISABELLE_TOOLING_ROOT/docs/coordination-board.md`; old and new writers must
not share a board concurrently.

## Delegation

The coordinator creates the branch/worktree and releases any setup claim.
The worker registers and claims its branch and files with its own handle,
posts the handoff before releasing claims and saying `bye`, and returns a
complete final message. For an authorized integration, the coordinator
claims the destination branch. Normal delegation needs neither forced
takeover nor another agent's identity.

## Etiquette

- One handle per session; never write under another agent's handle.
- Claim narrowly and briefly: a passage rather than a theory several agents
  edit, a file rather than a directory, a branch only while you move it.
- Do not release, force or take over another agent's active claim unless
  the human asked.
- Answer requests addressed to you; if you cannot, say so on the board.
- `ic2.sh start` and `stop` post server notes on their own when the
  repository has a board; do not repeat them.
- Facts about the runtime, doc gaps and proof techniques belong in the
  project's own files, not on the board.

Skill revision marker: v0.7.1.
