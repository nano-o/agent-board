# agent-board: design

`bin/agent-board` is the public CLI; `src/agent_board.py` implements it with
Python 3.9+ standard-library facilities and Git. No daemon, external Python
packages, or files under `$HOME` are needed. The design dates from the
board's September 2026 review in the Isabelle tooling (see
[history](history/implementation-handoff-2026-09.md)); how a project
installs and checks the board is in
[project-integration.md](project-integration.md).

The board lets agents in a repository's main and linked worktrees announce
presence, claim resources, and post handoffs. Project decisions and milestones
still belong in the project's plan. The board is local coordination, not a
cross-machine service or an access-control boundary against uncooperative
programs.

## Location and format

The default location is `<git common dir>/agent-board`, shared by all
linked worktrees and outside their tracked files. `AGENT_BOARD_DIR`
overrides it. Run inside a worktree or pass `--project-root DIR`. Without a
Git repository, an explicit board directory permits standalone use; Git
hooks still require a repository.

Format 2 contains:

- `.lock`: a stable inode locked with kernel `flock`. Never unlink or replace
  it while clients might be running. Kernel ownership disappears on process
  death; the remaining inode is harmless, not a stale held lock.
- `format`: the format marker, `2`.
- `state.json`: the format version, complete typed claim list, and last
  reserved post sequence number. Each update writes and fsyncs a temporary
  file, replaces the snapshot, then fsyncs its directory.
- `agents/<handle>`: presence records; modification times are activity times.
- `messages/<20-digit sequence>.md`: immutable published posts, with
  `time`, `from`, `kind`, and `re` headers followed by a blank line and body.
- `cursors-v2/<name>`: the greatest successfully emitted post number.
- `posts/`: an empty directory kept from the first format's layout; nothing
  reads it.

Authoritative reads, ownership checks, presence renewal, and mutations use
one lock. Readers see a complete claim snapshot. Input is collected before
locking; output is delivered after unlocking. No external Git mutation or
foreign hook runs while the board lock is held. Malformed authoritative
state fails closed with an error; it is never interpreted as an empty board.
Repair corrupt state from a known backup with clients stopped (see
"Incomplete or corrupt state"). Hidden unpublished temporary files left by
killed processes are ignored; remove those only during stopped-client
maintenance if desired.

## Identity and presence

Writing actions use `--as HANDLE` or `AGENT_BOARD_AGENT`. Handles match
`[a-z0-9][a-z0-9._-]{0,63}`; examples are `main` and `tx-layer`. A handle
belongs to one session. Do not borrow another session's identity.

`hello --task TEXT` records worktree, branch, task and start time. `claim`
also registers minimal presence if necessary. Every valid board action
carrying an explicit handle renews existing presence, including `path`,
`guard`, `who`, `claims`, and checks rejected for an ownership conflict.
Argument-validation failures do not renew. `post` alone does not register
presence (a tool posting notes under its own handle needs none). Hook
installation and removal are maintenance actions, not heartbeats. `bye`
releases all owned claims and removes presence; later reads do not recreate
it.

Anonymous observations do not renew anyone. Guards, including Git hooks,
infer identity only when exactly one **active** agent names the current
worktree, and renew that agent. They never infer a stale owner. Ambiguous or
absent identity treats every active claim as foreign. In a shared worktree,
run Git as `AGENT_BOARD_AGENT=<your-handle> git ...`.

A claim is stale when its owner's presence is absent or older than
`AGENT_BOARD_STALE_MINUTES` (default 180). Stale claims remain visible,
but guards warn and allow the operation. Use occasional board activity to
retain leases during long jobs; a running process alone is not a heartbeat.

## Resources and atomic ownership

Paths resolve lexically relative to the invocation directory, or to
`--project-root` when supplied, whether they exist or not. They are stored
relative to the worktree root, so they refer to the same tracked name across
worktrees. Examples:

- From the root, `PLAN.md` claims that file; from `src/`, `new.c` claims
  `src/new.c` even before creation.
- An existing directory, or an explicit trailing slash such as `future/`,
  covers its descendants. `.` from the root claims the whole worktree;
  `.` from a nested directory claims that subtree. Absolute paths are accepted
  within the worktree. Escaping it is an error.
- `refs/heads/main` claims a ref; normalized ref spelling is used.
- `token:NAME` claims a token, for shared things that are not files; a
  project's instructions name its tokens. A bare name is always a path, and
  `path:NAME` forces one: `path:refs/heads/main` means a file, not a ref.
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
It never acknowledges anything.

`digest --cursor NAME` captures the board in one snapshot and is bounded:
it prints at most `--limit` posts (default 20; `0` removes the bound).

- For an existing cursor it prints the oldest unread posts, up to the
  limit, and says how many more are unread.
- With `--full`, or for a cursor that does not exist yet, it prints who
  holds what (agents and claims) and the latest posts: the recovery view
  after a session starts, resumes or compacts. A new reader starts there;
  older history is `show --all`'s, not unread mail. For an existing cursor
  with unread posts older than that window, it says how many it did not
  show.

`--mark` updates the cursor only after the complete output has been
written and flushed successfully, and only through what was printed: it
never moves past an unread post that the limit left out. So a batch view
advances to its last printed post; the recovery view advances to the
latest post for a new cursor, or when it printed every unread post, and
otherwise leaves the cursor alone. It acknowledges posts of that snapshot,
never a newly arriving post. Concurrent updates take the maximum cursor, so
a delayed reader cannot move it backwards. Interrupted or failed delivery
can repeat messages and does not acknowledge an omitted batch. Successful
delivery means the output stream accepted the bytes, not that a human or
model read them. The capability `bounded-digest` in `version --json` says a
board bounds its digests this way.

A repository without a board: `guard` and `digest` are silent successes;
`who`, `claims` and `show` report “no board yet”; `path` reports its location.
`hello`, `post` and `claim` initialize it. `--if-board` suppresses creation,
which lets a tool post notes only when a board exists.

Claude Code's `SessionStart` and `UserPromptSubmit` hooks call
`integrations/claude/hook.sh`, which a project installs as
`.agent-board/claude-hook.sh`. It finds the executable as every caller
does, from `AGENT_BOARD_COMMAND` (one absolute path) or else `PATH`, and
does nothing when neither resolves. It uses a session cursor and `--mark`;
`SessionStart` adds `--full`, the bounded recovery view, for a fresh,
resumed or compacted context. The digest
runs under a five-second timeout, well inside the hook's 15 seconds, and
the hook exits successfully, with nothing on stderr, even when the board
reports an error. Codex agents read at task start, before shared edits/ref
moves, and on handoff.

## Git enforcement and its limits

`install-hook` installs two shared hooks, each calling the executable's
resolved real path, recorded at installation:

- `pre-commit`: `guard --staged` checks all staged names (including both sides
  of a rename) and the current branch.
- `reference-transaction`: reads the full input and checks every reported
  shared ref in the `prepared` phase. This covers ordinary fast-forward
  merges, resets, rebases, direct `update-ref`, branch creation/deletion, and
  multiple-ref transactions. The same owner, inference and staleness rules
  apply. Per-worktree `HEAD` is not a shared branch claim.

Each guard's second line is exactly `# agent-board guard: remove with
agent-board uninstall-hook.`; a hook without that line is foreign, including
one that carries the Isabelle tooling's old `isabelle-tooling board guard`
marker. Existing foreign hooks require `install-hook --force`, are retained
as `<hook>.pre-board`, and run first with their arguments, environment and
original stdin. The ref hook independently replays the same input to both
consumers; a foreign failure is preserved. Reinstallation preserves the
backups; `uninstall-hook` restores them. Both hooks are preflighted before
installation changes either one, and installation writes both or neither:
if the second write fails, the first hook is put back as it was.

The hooks directory is `git rev-parse --git-path hooks`, which follows
`core.hooksPath`. Both `install-hook` and `uninstall-hook` refuse a
directory outside the Git common directory, so a shared or global hooks
directory, or a tracked one such as `.githooks`, is never modified. Linked
worktrees share the common directory's hooks.

These are checks at particular boundaries, not locks spanning the whole Git
operation. **A rejected ref update may already have changed the index or
working tree.** Inspect and coordinate recovery; never automatically reset,
clean or discard changes in response. Editing and tokens require explicit
pre-operation `guard` calls. File claims alone do not prevent a
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

A brief states its mode on its first line: `Mode: independent. Handle:
HANDLE.` or `Mode: supervised by HANDLE.`; without one, a worker in a
project with `agent-board.conf` is independent.

An independent worker registers with its own handle and claims its branch
and files. The coordinator creates the branch and worktree and releases any
setup claim first. The worker commits within its authority, posts a handoff
with branch, commit and validation results, then releases claims and says
`bye`, and returns a complete final message. The coordinator reviews the
handoff and, only for an authorized integration, claims the destination
branch and guards the affected resources. Normal delegation needs no forced
takeover or identity sharing.

A supervised worker does not use the board. Its coordinator claims first
and keeps the claims until the worker has stopped writing, gives its
workers disjoint scopes, relays what matters, and commits under its own
handle. The skill's `references/delegation.md` has both procedures.

## Project files and doctor

`init`, `sync`, `update` and `remove` install the board's project files, as
[project-integration.md](project-integration.md) specifies: the descriptor
`agent-board.conf`, the inventory, the skill and its Claude alias, the
digest-hook launcher, the hook entries in `.claude/settings.json` and the
instruction block. The rules shared with the Isabelle tooling live in
`src/project_files.py`, which that repository keeps an identical copy of.
Installing never registers an agent and never installs the Git guards.

`doctor` is read-only. It checks that the resolved executable is the one
running, its checkout is at the pin and clean, the descriptor, the project
files (`sync --check`), the storage, and both guards, and prints `[OK]`,
`[NOTE]` and `[FAIL]` lines or, with `--json`, one object with stable check
ids. It opens the board's lock only for reading, never creates it, and runs
Git without optional locks, so a status check does not rewrite an index.

## Incomplete or corrupt state

A board exists when its directory holds any entry besides `.lock`. One
without `state.json` is incomplete: every verb that reads or writes board
state fails with "incomplete board state", including the Git guards, which
then block commits and ref updates. Nothing migrates it or silently starts
a new board in its place, since what remains may be real state. A
malformed `state.json` or `format` fails the same way.

Repair by hand with all clients stopped: restore `state.json` from a
backup, or, when the board held nothing worth keeping, remove the
directory; the next `hello`, `post` or `claim` starts a new board. The same
applies to a board left incomplete by a crash during its very first write.

The board this code replaced lived under the Isabelle tooling, at
`<git common dir>/isabelle-tooling/board`, driven by `scripts/board.sh`.
agent-board neither reads nor migrates it, and ignores the old
`ISABELLE_BOARD_*` variables. Delete such state, and hooks carrying the old
marker, by hand.

Development and validation use disposable repositories and never touch a
user's live board.

## Validation checklist

`make validate` runs the CLI suite `tests/cli_test.sh`, the regressions in
`tests/regression_test.py` and the Claude hook test
`tests/claude_hook_test.sh`, plus shell lint. The regression suite uses pipes
and intercepted IO boundaries for deterministic scheduling, real process
termination for crash recovery, and isolated Git/home configuration. It
checks:

- One winner for initial, stale, and directory/file concurrent acquisition;
  failed batches acquire nothing; killed writers publish no partial claims.
- Stale ancestor retirement, successor-safe release, presence renewal and
  invalid/anonymous/inferred identity behavior.
- Nested and nonexistent paths, roots, absolute paths, directory deletion,
  escapes, symlinks, tokens and advisory fragments.
- Delayed/killed publishers, sequence gaps, arrivals during output, concurrent
  readers, failed and interrupted delivery; bounded digests that deliver
  every unread post once and in order, a recovery view that never skips an
  unread post, a new reader starting at the latest posts, and a 600-post
  history whose digest stays under 4 KB.
- Real owner/foreign merge, reset, rebase, update-ref, branch creation/deletion
  and multi-ref transactions in main and linked worktrees; rename limitations,
  unrelated refs, absent boards, and the `--no-verify` boundary.
- Hook chaining with stdin consumers, foreign exit status, reinstall/restore;
  worker commit and coordinator integration under separate handles.
- Incomplete state for every top-level entry and for a used board that
  lost its snapshot, malformed snapshots and format markers, and the former
  names and location.
- `version` for a plain copy, a clean and a modified checkout, a copy
  inside another repository, a symlink on `PATH` and a caller's `GIT_DIR`.
- The hooks-directory restriction, the exact marker line, the recorded
  real path, and both-or-neither installation under injected failures.
- The Claude hook: resolution, session cursors, `SessionStart` replay,
  malformed input, and the five-second bound with the lock held.

`tests/project_test.py` covers the project files and doctor against a
scratch runtime checkout built from the working tree: `init` in a new
repository and next to the project's own settings, instruction files and
skills; collisions, edited files, entries and blocks, unmanaged files in the
skill directory; the index preflight (unstaged, untracked, ignored); a
symlinked skill parent outside the checkout; malformed descriptors, a
missing inventory and adoption through `update`; revisions without a
manifest; `sync` keeping the pin and `update` moving it, with obsolete files
removed; link mode; two installers at once; publication with `TMPDIR` on
another filesystem; a failure injected after every publication step of
`init`, `update` and `remove`, whose printed commands restore the tree
exactly; every doctor check id and status, a doctor that cannot run, and
doctor leaving the project, its Git directory and the runtime checkout
unchanged.
