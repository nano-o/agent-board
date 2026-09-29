# Coordination board for concurrent agents

Implemented on `coordination-board`, including the September 2026 review
fixes. `scripts/board.sh` is the public CLI; `scripts/board.py` implements it
with Python 3.9+ standard-library facilities and Git. No daemon, external
Python packages, Isabelle process, or files under `$HOME` are needed.

The board lets agents in a repository's main and linked worktrees announce
presence, claim resources, and post handoffs. Project decisions and milestones
still belong in the project's plan. The board is local coordination, not a
cross-machine service or an access-control boundary against uncooperative
programs.

## Location and format

The default location is `<git common dir>/isabelle-tooling/board`, shared by
all linked worktrees and outside their tracked files. `ISABELLE_BOARD_DIR`
overrides it. Run inside a worktree or pass `--project-root DIR`. Without a
Git repository, an explicit board directory permits standalone use; Git
hooks still require a repository.

Format 2 contains:

- `.lock`: a stable inode locked with kernel `flock`. Never unlink or replace
  it while clients might be running. Kernel ownership disappears on process
  death; the remaining inode is harmless, not a stale held lock.
- `format`: the activated format marker. A missing snapshot after activation
  is an error, not a legacy board eligible for migration.
- `state.json`: the format version, complete typed claim list, and last
  reserved post sequence number. Each update writes and fsyncs a temporary
  file, replaces the snapshot, then fsyncs its directory.
- `agents/<handle>`: presence records; modification times are activity times.
- `messages/<20-digit sequence>.md`: immutable published posts, with
  `time`, `from`, `kind`, and `re` headers followed by a blank line and body.
- `cursors-v2/<name>`: the greatest successfully emitted post number.
- `posts/`: the legacy location, retained as a board marker and migration
  archive. Migrated `claims/` and `cursors/` also remain for inspection.

Authoritative reads, ownership checks, presence renewal, and mutations use
one lock. Readers see a complete claim snapshot. Input is collected before
locking; output is delivered after unlocking. No external Git mutation or
foreign hook runs while the board lock is held. Malformed authoritative
state fails closed with an error; it is never interpreted as an empty board.
Repair corrupt state from a known backup with clients stopped. Hidden
unpublished temporary files left by killed processes are ignored; remove
those only during stopped-client maintenance if desired.

## Identity and presence

Writing actions use `--as HANDLE` or `ISABELLE_BOARD_AGENT`. Handles match
`[a-z0-9][a-z0-9._-]{0,63}`; examples are `main` and `tx-layer`. A handle
belongs to one session. Do not borrow another session's identity.

`hello --task TEXT` records worktree, branch, task and start time. `claim`
also registers minimal presence if necessary. Every valid board action
carrying an explicit handle renews existing presence, including `path`,
`guard`, `who`, `claims`, and checks rejected for an ownership conflict.
Argument-validation failures do not renew. `post` alone does not register
presence (the system `ic2` poster needs none). Hook installation/removal and
migration are maintenance actions, not heartbeats. `bye` releases all owned
claims and removes presence; later reads do not recreate it.

Anonymous observations do not renew anyone. Guards, including Git hooks,
infer identity only when exactly one **active** agent names the current
worktree, and renew that agent. They never infer a stale owner. Ambiguous or
absent identity treats every active claim as foreign. In a shared worktree,
run Git as `ISABELLE_BOARD_AGENT=<your-handle> git ...`.

A claim is stale when its owner's presence is absent or older than
`ISABELLE_BOARD_STALE_MINUTES` (default 180). Stale claims remain visible,
but guards warn and allow the operation. Use occasional board activity to
retain leases during long jobs; a proof process alone is not a heartbeat.

## Resources and atomic ownership

Paths resolve lexically relative to the invocation directory, or to
`--project-root` when supplied, whether they exist or not. They are stored
relative to the worktree root, so they refer to the same tracked name across
worktrees. Examples:

- From the root, `PLAN.md` claims that file; from `formal/`, `New.thy` claims
  `formal/New.thy` even before creation.
- An existing directory, or an explicit trailing slash such as `future/`,
  covers its descendants. `.` from the root claims the whole worktree;
  `.` from a nested directory claims that subtree. Absolute paths are accepted
  within the worktree. Escaping it is an error.
- `refs/heads/main` claims a ref; normalized ref spelling is used.
- `jedit` and `token:NAME` claim tokens. `path:jedit` explicitly means a file
  named `jedit`; `path:refs/heads/main` means a file, not a ref.
- `path#fragment` is an advisory passage. It is displayed but does not block
  acquisition or guards, even when another claim covers the whole file.

Symlink leaves are claimed as Git's symlink paths, not as their targets.
Traversing a symlink directory is rejected because Git does not track those
child paths. `#` is reserved for fragments; resource names containing newlines
or carriage returns are rejected. Paths, refs and tokens have distinct types.

`claim --reason TEXT RESOURCE...` checks every overlap and publishes the
whole batch under the same lock. A foreign active directory claim conflicts
with a child file claim and vice versa. Incompatible concurrent claimants
have one winner; a failed batch acquires none of its resources. Reclaiming
an exact owned resource updates its reason.

A normal takeover may displace stale claims. `--force` may displace active
ones and still requires a reason; skill instructions reserve this for human
authorization. All displaced overlapping predecessors are retired, including
ancestor directories, so a later heartbeat cannot revive them. The takeover
post records displaced owners and the reason. State publication precedes its
audit post; interruption can leave a valid claim without that notification.

`release RESOURCE...` uses the same normalization and ownership checks,
including a directory's exact path after it has been deleted. An old owner's
release cannot remove a successor's claim. `release --all` and `bye` release
only that handle's claims. Explicit release batches fail before changing any
claim if a selected resource belongs to someone else.

## Post ordering and delivery

`post [--kind KIND] [--re RESOURCE] MESSAGE...` publishes a note; `post -`
reads stdin. Sequence reservation and final publication occur under the same
lock. The sequence is persisted before publication, so a killed publisher
may leave a gap but cannot reuse a number or publish behind a later post.
Timestamps are display metadata, not ordering keys.

`show` displays the most recent 20 posts by default (`--last N` or `--all`).
It never acknowledges anything. `digest --cursor NAME` captures all unread
posts in one snapshot; a first read and `--full` include **all** published
posts. There is no implicit ten-post truncation. `--mark` updates the cursor
only after the complete output has been written and flushed successfully.
It acknowledges the last post in that snapshot, never a newly arriving post.
Concurrent updates take the maximum cursor, so a delayed reader cannot move
it backwards. Interrupted or failed delivery can repeat messages and does
not acknowledge an omitted batch. Successful delivery means the output stream
accepted the bytes, not that a human or model read them.

A repository without a board: `guard` and `digest` are silent successes;
`who`, `claims` and `show` report “no board yet”; `path` reports its location.
`hello`, `post` and `claim` initialize it. `--if-board` suppresses creation,
which lets `ic2.sh start`/`stop` post server notes only when a board exists.

Claude Code's `SessionStart` and `UserPromptSubmit` hooks call
`extension/bin/board-hook.sh`, which uses a session cursor and `--mark`;
`SessionStart` adds `--full` for a fresh or compacted context. The host wrapper
exits successfully even when the board reports an error. Its declaration is
`extension/hooks/board-hooks.json`, explicitly named by the manifest.
Codex agents read at task start, before shared edits/ref moves, and on handoff.

## Git enforcement and its limits

`install-hook` installs two shared hooks using the tooling clone's absolute
`board.sh` path:

- `pre-commit`: `guard --staged` checks all staged names (including both sides
  of a rename) and the current branch.
- `reference-transaction`: reads the full input and checks every reported
  shared ref in the `prepared` phase. This covers ordinary fast-forward
  merges, resets, rebases, direct `update-ref`, branch creation/deletion, and
  multiple-ref transactions. The same owner, inference and staleness rules
  apply. Per-worktree `HEAD` is not a shared branch claim.

Existing foreign hooks require `install-hook --force`, are retained as
`<hook>.pre-board`, and run first with their arguments, environment and
original stdin. The ref hook independently replays the same input to both
consumers; a foreign failure is preserved. Reinstallation preserves the
backups; `uninstall-hook` restores them. Both hooks are preflighted before
installation changes either one. Custom `core.hooksPath` is respected; a
shared repository hooks path is needed for all worktrees to use the hooks.
`doctor.sh` checks board readability/version and both executable hook paths.

These are checks at particular boundaries, not locks spanning the whole Git
operation. **A rejected ref update may already have changed the index or
working tree.** Inspect and coordinate recovery; never automatically reset,
clean or discard changes in response. Editing and the `jedit` token require
explicit pre-operation `guard` calls. File claims alone do not prevent a
fast-forward merge: guard affected paths and claim/guard the destination ref.
Fragments are always advisory.

On the tested Git 2.43 files backend, branch rename reports the source deletion
but bypasses the destination ref transaction. Source claims block renaming;
destination claims alone do not. Explicitly guard both source and destination
before `git branch -m/-M`. Other ref backends or Git versions may expose
different events; the tests exercise the observed boundary. Symbolic-ref
changes are not generally guarded by Git 2.43 either. Retain expected-old-value
checks for direct ref rewrites. `git commit --no-verify` bypasses only the
commit hook, not the ref hook. Deliberately disabling hooks bypasses protection
and needs explicit authorization and an explanatory post.

Reference: [Git reference-transaction hook documentation](https://git-scm.com/docs/githooks#_reference_transaction).

## Delegation

The coordinator registers, creates the branch and worktree, and releases any
setup claim before delegation. The worker registers with its own handle and
claims its branch and files. It commits within its authority, posts a handoff
with branch, commit and validation results, then releases claims and says
`bye`. It also returns a complete final message. The coordinator reviews the
handoff and, only for an authorized integration, claims the destination
branch and guards the affected resources. Normal delegation needs no forced
takeover or identity sharing.

## Migration and rollout

Do this only when intentionally upgrading a repository's live board:

1. Stop all old board writers: agent sessions, digest hooks, and ic2 commands
   that post server notes. Old clients do not understand format versions and
   **must not run concurrently** with new clients. Back up the board while
   stopped; no automatic migration happens on a normal command.
2. Update all tooling paths, `ISABELLE_TOOLING_ROOT` settings, and installed
   host extensions to the new implementation. From the desired worktree run
   `board.sh migrate --writers-stopped`. This flag confirms the operator has
   stopped old clients; the tool cannot detect them reliably.
3. Migration preserves valid legacy ownership/presence and copies every
   published legacy post, in legacy filename order, into numbered messages.
   Legacy cursors are retained but ignored: all posts replay conservatively,
   since an old cursor might have skipped messages. Incomplete legacy claim
   records are retired and individually reported, with originals retained
   for inspection. The migration report is retained in `state.json`.
   Historical overlapping claims are preserved for owners to resolve.
4. Migration activates with one final `state.json` replacement. If interrupted
   before activation, repeat it with old writers still stopped. If already
   activated, rerunning reports the existing state without resetting cursors
   or duplicating posts. A corrupt format-2 snapshot requires repair, not
   legacy migration.
5. Run `show --all`, resolve reported incomplete/overlapping legacy claims,
   and reinstall with `install-hook` (or `--force` when chaining foreign hooks).
   Check both paths with doctor, then resume only new clients. Do not restart
   old code against the upgraded board or attempt an in-place downgrade.

Development and validation use disposable repositories and never migrate a
user's live board.

## Validation checklist

`make validate` runs the original CLI suite and
`tests/board_regression_test.py`, plus shell lint, manifests and generated
profile checks. The regression suite uses pipes and intercepted IO boundaries
for deterministic scheduling, real process termination for crash recovery,
and isolated Git/home configuration. It checks:

- One winner for initial, stale, and directory/file concurrent acquisition;
  failed batches acquire nothing; killed writers publish no partial claims.
- Stale ancestor retirement, successor-safe release, presence renewal and
  invalid/anonymous/inferred identity behavior.
- Nested and nonexistent paths, roots, absolute paths, directory deletion,
  escapes, symlinks, tokens and advisory fragments.
- Delayed/killed publishers, sequence gaps, arrivals during output, concurrent
  readers, failed and interrupted delivery, and full first-read replay.
- Real owner/foreign merge, reset, rebase, update-ref, branch creation/deletion
  and multi-ref transactions in main and linked worktrees; rename limitations,
  unrelated refs, absent boards, and the `--no-verify` boundary.
- Hook chaining with stdin consumers, foreign exit status, reinstall/restore;
  worker commit and coordinator integration under separate handles.
- Legacy ownership/post retention, incomplete-record recovery, cursor replay,
  interrupted/repeated migration, and malformed authoritative state.
