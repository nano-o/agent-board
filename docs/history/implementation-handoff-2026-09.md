# Coordination board: implementation handoff

Status: implementation completed on 2026-09-17. The implementation and
validation record are included in this commit. No live board was migrated,
and no branch was merged or published.

## Implementation result

All eight review findings are addressed. The existing `board.sh` CLI now
calls `board.py`, using Python's standard-library kernel `flock`, complete
claim snapshots, typed resources, ordered publication and post-delivery
cursor updates. Both Git guards, format-2 migration, presence renewal,
doctor diagnostics and delegated ownership instructions are implemented.
Both host worker profiles have been regenerated.

Validation: `make validate` passes, including 31 new behavioral regressions
with controlled concurrency and real process termination, the original shell
suites, ShellCheck, manifests and rendered-profile checks. Both changed
skills pass the skill validator. Diff whitespace checks pass.

The [current design and rollout instructions](coordination-board.md) document
the tested boundaries: Git 2.43 branch rename destinations still require
explicit cooperative guards; rejected ref updates may leave index/worktree
changes. Live rollout requires stopping all old writers, explicit migration,
and reinstalling both hooks. The original review evidence and implementation
requirements below are retained for review.

## Task and checkout

Fix the eight findings from the coordination-board review using the plan
below. Use an ordinary coding agent; this is shell tooling and documentation
work, not an `ic2-prover` proof assignment.

- Worktree: `/home/nano/Documents/isabelle-formal-modeling-tooling-board`
- Branch: `coordination-board`
- Reviewed commit: `7849d3d7f2f60efa656501c7d0aaa005bbf526ae`
- Base branch: `main`, at `8650e8b` when reviewed
- Original design: `docs/coordination-board.md`
- Main implementation: `scripts/board.sh`
- Main tests: `tests/board_test.sh`

Check the current status before editing. Preserve changes made after the
review. Keep implementation changes in this worktree. Use disposable
repositories for tests; do not migrate or mutate a user's live board while
developing. Do not merge or publish as part of this handoff.

## Review evidence

The existing `make validate` and `git diff --check main...HEAD` passed at the
reviewed commit. Focused tests in temporary repositories nevertheless
reproduced these failures:

1. Eight concurrent non-forced takeovers of one stale claim all succeeded.
   Initial `mkdir` acquisition is atomic; the stale-owner check and field
   replacement are not.
2. `digest --mark` permanently skipped a post published between its two
   directory scans. It also skipped a post whose timestamped filename was
   allocated earlier but whose final rename occurred after a newer post had
   been consumed. Fixing the second scan alone is insufficient.
3. From `formal/`, claiming nonexistent `New.thy` stored `New.thy`, which did
   not cover staged `formal/New.thy`. Claiming `.` stored `/`, which covered
   no root-relative staged paths.
4. Killing a claimant immediately after claim-directory creation left the
   resource permanently `CONTESTED`. Neither `claim --force` nor
   `release --all` recovered it.
5. Separate agents successfully claimed `formal/` and `formal/X.thy`.
   Thereafter each agent's guard rejected its attempt to commit the file.
6. The proving skill tells the coordinator to claim the delegated branch;
   the worker profile tells the worker to claim the same branch. Following
   both instructions blocks the worker's claim and commit.
7. `guard`, `who`, and `claims` do not renew presence even with `--as`.
   An owner running its own guard after its presence became stale remained
   stale, and a foreign guard ignored its claim.
8. A foreign agent's `git merge --ff-only feature` moved a claimed branch
   and changed a claimed file despite the installed pre-commit hook.

## Implementation sequence

### 1. Add regression tests

Encode all eight reproductions as behavioral tests. Use barriers or
controlled command wrappers to pause a process at the relevant boundary,
not sleeps that merely make a race likely. Confirm the tests expose the
current failures before fixing them.

Cover both initial acquisition and stale takeover, and use a real process
termination for interrupted-write recovery. Keep tests isolated from the
user's Git configuration, hooks, home directory, and repositories.

### 2. Synchronize ownership and publish complete claim state

Keep the CLI and local, daemon-free board. Introduce one shared kernel-managed
`flock` for claim checks and mutations, with automatic release on process
death. Describe the guarantee as no stale lock ownership; an inert lock inode
is not a stale held lock. Use a stable lock target that is not replaced during
state publication.

Publish complete claim-state snapshots atomically instead of editing visible
owner/reason/since fields in place. Readers must observe a consistent state.
Check all overlapping resources under the same lock as the update. Make a
multi-resource acquisition all-or-nothing, including conflict checks.

Serialize acquisition, takeover, release, and presence-dependent decisions.
Ensure an old owner's release cannot remove a successor's claim. Retire
displaced stale claims so a later heartbeat cannot revive them. Preserve
the existing requirement for an explicit reason on forced takeover.

Support recovery of incomplete legacy claim records under the lock, reporting
what was recovered. Do not silently ignore malformed authoritative state.
Avoid holding the global lock while waiting on stdin, writing potentially
blocked consumer output, or executing an external Git mutation.

### 3. Unify resource normalization

Resolve CLI paths relative to the invocation directory (or `--project-root`
when supplied), then store worktree-relative paths regardless of existence.
Represent the whole worktree explicitly. Keep paths, refs, tokens such as
`jedit`, and advisory fragments distinguishable.

Use the same canonicalization and overlap rules in claim, release, and guard.
Retain advisory fragment behavior. Make symlink handling agree with Git's
tracked path names rather than accidentally claiming a different file.
Reject paths outside the worktree.

Test existing and nonexistent files, absolute paths, nested invocation
directories, `.` and `..`, whole-worktree coverage, directory prefixes,
deleted directories, symlinks, fragments, and escape attempts.

### 4. Order publication and acknowledge only delivered posts

Allocate monotonically increasing post numbers and complete final publication
under the same board lock. Timestamps remain display metadata. A failed
publication may leave a sequence gap, but an older sequence must never be
published after a newer sequence becomes consumable. Update any sequence
metadata in an order that cannot reuse a published number after a crash.

A digest captures one published snapshot, emits it outside the global lock,
and advances its cursor only through the successfully emitted batch.
Concurrent cursor updates must not move backward. Interrupted delivery may
repeat messages; it must not acknowledge omitted messages. The guarantee is
successful output delivery, not proof that a human or model read the output.

Define first-read and `--full` behavior explicitly. Remove implicit truncation
that advances a cursor beyond undisplayed posts; any bounded batch must leave
the remainder unread. Test concurrent readers, failed output, a delayed
publisher, a post arriving during a digest, and sequence gaps after crashes.

### 5. Centralize renewal and fix delegated ownership

Centralize presence renewal for commands carrying a validated handle,
including `guard`, `who`, and `claims`. Anonymous reads must not renew an
agent. Keep `bye` terminal rather than recreating the departing presence.
Specify renewal behavior for failed commands and inferred identities, and
test the chosen rules consistently.

The coordinator creates the branch/worktree; the worker registers and owns
its branch and files. Remove the instruction for the coordinator to retain
the delegated branch claim. The worker posts its handoff before releasing
claims and leaving. The coordinator claims the destination branch when
performing an authorized integration operation. No sharing another agent's
handle or forced takeover should be needed for this normal workflow.

Update the shared worker instructions and regenerate both host profiles.
Test the workflow with a real worker commit under the installed hook.

### 6. Guard ref updates and state enforcement limits accurately

Retain `pre-commit` for staged-file checks. Add a `reference-transaction`
hook that validates affected refs during `prepared` and rejects foreign
active claims. Read the transaction's full stdin and reject the transaction
when any affected ref conflicts. Handle creation, deletion, and multiple-ref
transactions, using the same identity and staleness rules as other guards.

Preserve existing foreign hooks, their arguments, stdin, and exit behavior.
Make installation, reinstallation, and uninstallation work for both hooks.
Update doctor checks and the bypass instructions: `--no-verify` must not be
described as a general bypass for the new ref hook.

Test foreign and owner operations for fast-forward merge, rebase, reset,
direct `update-ref`, and branch creation/deletion, from main and linked
worktrees. Test rename source and destination behavior on the supported Git
version; do not assume every operation exposes identical hook events.
Include an unrelated update and the repository-without-a-board case.

Document the precise boundary: rejecting a ref update does not guarantee Git
has left the index or working tree unchanged. File editing, the `jedit` token,
and fragment cooperation still require explicit pre-operation checks. Do not
automatically reset, clean, or discard changes after a rejected operation.
Retain expected-old-value checks for direct ref rewrites where applicable.

Reference: https://git-scm.com/docs/githooks#_reference_transaction
The local Git version checked during planning was 2.43.0; use its supported
`prepared` event rather than relying on newer events.

### 7. Update documentation and migration

Update `docs/coordination-board.md`, `docs/architecture.md`, `README.md`, the
coordination/proving skills, CLI help, and `scripts/doctor.sh`. Make the review
checklist testable and replace the claim that `mkdir` alone supplies the
required concurrency guarantee.

Introduce a board-format version and a controlled migration for existing
boards. Preserve posts and valid ownership state. Conservatively replay old
cursors because the old implementation may already have skipped posts.
Report incomplete legacy records and their recovery. Require old writers to
stop before migration; old clients do not understand the new format and must
not run concurrently with new clients. Update tooling paths and reinstall
hooks as part of documented rollout, not during development on a live board.

## Completion criteria and report

- Every reproduction above has a passing regression test.
- Controlled concurrency tests establish one winner for incompatible claims.
- Crash tests leave no permanently contested resource or unread-post loss.
- Foreign operations are rejected and owner/unrelated operations pass at the
  documented enforcement boundaries.
- Hook chaining and restoration work with stdin-consuming foreign hooks.
- A coordinator-to-worker-to-handoff scenario completes without forced
  takeover or an identity workaround.
- `make validate`, generated-profile checks, and diff whitespace checks pass.
- Migration and unsupported-operation limits are documented and tested.

Return a concise report of changed behavior, tests run, residual limitations,
and worktree/branch/commit information when applicable. Do not mark the work
complete on the basis of the old sequential test suite alone.
