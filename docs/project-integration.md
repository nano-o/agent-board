# Project integration: contracts

Copied on 2026-09-28 from `docs/delivery-contracts.md` in the Isabelle
tooling repository at `aa46d0c`: its "Shared rules" and "agent-board"
sections, unchanged below. Step 2 of that repository's delivery plan moved
them here, and this repository now owns the agent-board part. Each
repository keeps its own copy of the shared rules, and a change to them is
made in both.

**Status.** Everything below is implemented: the storage and its
incomplete-state rule, the CLI without `migrate`, `version` with the
capabilities `bounded-digest`, `doctor` and `project`, the Git guards, the
Claude hook, the project operations with their inventory, descriptor,
settings entries and instruction block, and `doctor`. The shared rules are
implemented in `src/project_files.py`, kept identical to the Isabelle
tooling's `scripts/project_files.py`. Where the implementation settles a
detail the text left open, "Decisions made in implementation" at the end
records it.

## Shared rules

**Terms.** The *checkout root* is `git rev-parse --show-toplevel` of the
checkout a command runs in, or of `--project-root DIR`. A *component* is
`isabelle-tooling` or `agent-board`. Its *runtime checkout* is the clone
that runs its code: the directory `ISABELLE_TOOLING_ROOT` names, or the
checkout containing the resolved `agent-board` executable. A *managed
path* is one a component's inventory records.

**Descriptors.** One per component at the checkout root:
`isabelle-tooling.conf` (its existing format, `format_version=1`, with the
keys the Isabelle tooling defines) and `agent-board.conf`:

```text
# agent-board project descriptor. Data only: key=value, never sourced.
format_version=1
board_revision=<40-hex commit>
```

Both are data only: UTF-8, one `key=value` per line, `#` comment lines and
blank lines allowed, never sourced. Lines end at LF alone, blank means
spaces and tabs, and a carriage return or NUL byte is refused, so every
parser of a descriptor, in Python or the shell, reads the same keys and
values. The revision is always a full 40-hex commit, never a ref.
`agent-board.conf` rejects unknown keys. A descriptor's presence is what
"this project uses the component" means: nothing else, such as a board
directory or a skill, turns a component on.

**Inventory.** `.isabelle-tooling/inventory.json` and
`.agent-board/inventory.json`, written with `json.dumps(indent=2,
sort_keys=True)` and a final newline:

```json
{
  "format": 1,
  "component": "agent-board",
  "revision": "<40-hex commit>",
  "mode": "copy",
  "link_source": null,
  "claude_skills": "aliases",
  "directories": [".agents/skills/agent-coordination"],
  "files": {
    ".agent-board/claude-hook.sh":
      {"sha256": "<hex>", "executable": true},
    ".agents/skills/agent-coordination/SKILL.md":
      {"sha256": "<hex>", "executable": false}
  },
  "symlinks": {
    ".claude/skills/agent-coordination":
      "../../.agents/skills/agent-coordination"
  },
  "json_entries": [
    {"file": ".claude/settings.json", "pointer": "/hooks/SessionStart",
     "element": true, "sha256": "<hex>"}
  ],
  "blocks": [
    {"file": "AGENTS.md", "syntax": "markdown", "sha256": "<hex>"}
  ]
}
```

- `mode` is `copy` or `link`; `link_source` is the absolute source checkout
  in link mode and `null` otherwise.
- `claude_skills` is `aliases` (per-skill symlinks under `.claude/skills/`)
  or `shared` (see "Skill directories").
- `directories` are wholly owned: any entry inside one that `files` does
  not list is drift.
- Paths are relative to the checkout root, with `/` separators.
- The inventory never records itself or the descriptor.

**Owned JSON entries.** An entry is either an object member, recorded by
the JSON Pointer of the member (`/mcpServers/iq`), or one element of an
array, recorded by the array's pointer with `"element": true`. The hash is
SHA-256 of `json.dumps(value, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)` encoded as UTF-8.

- Array elements are identified by content, not index: each component
  names a marker string that appears in its element (the board's hook
  elements contain `.agent-board/claude-hook.sh`). Exactly one element per
  recorded array may contain the marker; its hash must match.
- Writes load the file with the standard library, change only the owned
  entry, keep member and array order, and write `json.dumps(indent=2,
  ensure_ascii=False)` plus a newline. Adding a new member or element
  appends it.
- `remove` deletes the owned member or element, then prunes containers left
  empty along its pointer, and deletes the file when `{}` remains.
- An unmanaged member with the owned name, or an unmanaged element carrying
  the marker, is a collision: install refuses.

**Owned TOML block.** Only `.codex/config.toml`, owned by
`isabelle-tooling`. The block is the text between the whole lines
`# BEGIN isabelle-tooling` and `# END isabelle-tooling`, both included; the
file may contain exactly one of each, in that order. The first install
appends the block at the end of the file (creating the file when absent,
separated from existing text by one blank line); later writes replace it in
place, and all text outside it is kept byte for byte. `check` parses the
whole file with `tomllib` (Python 3.11 or later) and requires the parsed
`mcp_servers.iq` to equal the block parsed alone. That catches
duplicate tables, and bare keys after `# END` that TOML would attach to the
block's last table. An `[mcp_servers.iq]` table outside the block is a
collision.

**Owned Markdown blocks.** In root instruction files, the text between the
whole lines `<!-- BEGIN NAME -->` and `<!-- END NAME -->` (`NAME` is the
component), at most one pair per component per file. The first install
appends the block, separated by one blank line; later writes replace it in
place. The hash covers the lines strictly between the markers. The block's
first line is a comment naming the command that changes it.

**Root instruction files.** Only `AGENTS.md` and `CLAUDE.md` at the checkout
root. Before writing, resolve each existing one through symlinks; a target
that is not a regular file inside the checkout is refused. The block goes
into the set of resolved files:

- neither exists: create `AGENTS.md`, and `CLAUDE.md` as the relative
  symlink `CLAUDE.md -> AGENTS.md`; the set is `{AGENTS.md}`;
- both resolve to the same file: that file, recorded once;
- only `AGENTS.md` exists: it, and create the `CLAUDE.md` symlink;
- only `CLAUDE.md` exists: its target, and create `AGENTS.md` as a regular
  file holding only the block;
- both exist and resolve differently: both.

A file or symlink an installer creates here belongs to the project, not
the component: the inventory records only blocks, and `remove` leaves the
files in place, even empty.

**Skill directories.** Skill content lives in `.agents/skills/NAME/`, which
Codex discovers; Claude Code sees it through a relative symlink
`.claude/skills/NAME -> ../../.agents/skills/NAME`. When `.claude/skills`
itself resolves to the same directory as `.agents/skills` (a project-wide
alias), no per-skill symlinks are made and the inventory records
`claude_skills: shared`. When `.agents`, `.claude`, `.agents/skills` or
`.claude/skills` is a symlink resolving outside the checkout, install
refuses. `isabelle-tooling` owns the `isabelle-*` names its manifest
lists and `agent-board` owns `agent-coordination`; any other directory
there is the project's and untouched. An existing unmanaged `NAME` is a
collision.

**Project kinds.** A component may divide its projects into kinds, named
in its descriptor; the Isabelle tooling does, agent-board does not. A
manifest's top-level `"kinds"` lists the kinds its revision supports. A
skill entry may carry `"kinds"`, the kinds it is installed for; a skill
entry without it is installed for every kind, and no other entry may carry
it. A project that names no kind gets every skill, whatever the lists say.
For a project that names a kind, `init`, `sync`, `update` and `check`
refuse a revision whose top-level list lacks it, including every revision
from before kinds, and install only the skills for that kind. In
`project_files.py` the kind comes from the spec's optional `kind(values)`
hook, given the parsed descriptor.

**Link mode.** `sync --link [--source DIR]` replaces each managed skill
directory in `.agents/skills/` with an absolute symlink to
`DIR/<source skill path>`, records `mode: link` and `link_source`, and
leaves the Claude aliases, which still resolve through `.agents/skills`.
`DIR` defaults to the runtime checkout, but is normally a development
worktree, so the runtime checkout stays clean at its pin. Only skills are
linked; configuration entries and worker profiles stay copies. Link mode
is development state: the working tree is dirty on purpose, `check`
reports it, and doctor fails on it unless given `--allow-dirty`. Plain
`sync` restores the copies. Nothing in link mode may be committed.

**Installer lock.** Every writing verb of either component (`init`,
`sync`, `update`, `remove`) holds an exclusive `flock` on
`$(git rev-parse --git-path project-files.lock)` from preflight until the
descriptor is written. The lock is per worktree, like the files, and lies
outside the working tree. It serializes the two installers, which share
the root instruction files and the skill parents. `check` and `doctor` take
no lock, and report whatever state they see.

**Preflight: the index is the checkpoint.** Before writing, an installer
lists every path it would create, modify or delete, and refuses if any of
them has unstaged changes (the working tree differs from the index),
exists untracked, or is ignored by Git. The one exception: in link mode,
`sync` may replace the symlinks and the inventory that `link` wrote. Staged
changes are accepted. So a new repository runs `init` for one component,
`git add` of the paths it printed, then `init` for the other, with no
commit in between. Every successful install prints its touched paths and
the `git add -- …` command. An installer never stages or commits.

**Writing and failure.** The complete result is first built in a
temporary directory and validated: JSON and TOML parse, block markers are
unique, and the inventory and descriptor agree. Each destination is then
rechecked against its preflight state and published by writing a
temporary file in the destination's own directory, `fsync`ing it and
renaming it over the destination, so no rename crosses filesystems. The
order is managed files, then shared entries and blocks, then the
inventory, then the descriptor last. A descriptor write changes only its
revision line, keeping the other keys and comments byte for byte.

If any write fails, the installer stops. It prints the leaf paths it
touched, each shell-quoted, with the commands that restore them:
`git restore -- PATHS` for paths the index knows, `rm -f -- PATHS` for new
files and symlinks, then `rmdir` for the directories it created, deepest
first. It then exits 2. Until the tree is restored, `check` reports the
disagreement between files, inventory and descriptor, and doctor fails.

**Project operations.** Both components offer the same verbs, run inside
the project:

`init [--revision REV]`
: Refuse if the descriptor exists. Resolve `REV` (default `stable`) in the
  runtime checkout to a full commit, and install exactly that revision:
  files, entries, the root instruction block, inventory, then the
  descriptor. `isabelle-tooling init` also renders the session scaffold
  that `new-project.sh` writes today, from the same Git object, and
  publishes it with the integration under the same rules, descriptor last.
  `new-project.sh` becomes that renderer, taking the resolved commit and
  reading its templates with `git show`; its options pass through `init`.

`sync`
: Install exactly the descriptor's revision in copy mode, reading every
  file from that Git object (`git show REV:PATH`), never a working tree.
  Idempotent. Refuses drift in managed content rather than overwriting it.
  Without an inventory it refuses and names `update`.

`sync --link [--source DIR]`, `sync --check`
: Link mode, above; and the read-only check, which exits 0 when files,
  inventory and descriptor agree with the pinned object and 1 otherwise.

`update REV`
: The only verb that changes the pin. Stage the target revision's files,
  merge the owned entries and blocks into the staged copies, validate,
  install, write the descriptor last. `update stable` moves to the current
  validated commit. Without an inventory, `update` is the adoption path
  for a project whose descriptor predates project delivery, such as the
  offer-exchange checkout. It validates the descriptor, requires every
  managed target to be absent (anything present, including an existing
  block, is a collision), and installs; the inventory is written, and then
  the new revision line last.

`remove`
: Delete the component's unchanged managed files, entries and blocks, its
  inventory directory and its descriptor. Refuse and list anything edited.
  Project content is never removed: not the Isabelle session under
  `formal_rel`, not the root instruction files, and not the board's runtime
  data or Git guards, which `remove` reports when present.

`doctor [--allow-dirty] [--json]`
: The component's full read-only check. See each component below.

All of these exit 0 on success, 1 when they refuse or a check finds a
problem, and 2 on a usage error, a missing prerequisite or a failed write.
The installable set is read from a manifest at the target revision
(`extension/project/manifest.json` for the tooling,
`integrations/project/manifest.json` for the board); a revision without
one cannot be installed.

**`stable` refs and the validation record.** Each repository keeps a local
branch `stable`. It moves only by fast-forward, and only to a validation
record commit: a commit whose parent is the validated commit, and whose
only change appends an entry to `docs/validation.md`. The entry records
the date, the validated commit, the other repository's revision when both
were exercised, the host versions, and the new-project fixture outcomes,
including the behavioural observations, and names the candidate commits
exercised in both repositories. The record commit is made on `main`, the
mechanical checks are rerun at exactly that commit (the repository's
tests, the installer tests, and doctor on a fixture project pinned to it),
and only then does `stable` fast-forward to it. `init` and `update stable`
pin that record commit. The runtime checkout has `stable` checked out, and
development happens on `main` in a linked worktree. The runtime moves with
`git -C RUNTIME merge --ff-only COMMIT`, followed for the tooling by
`git submodule update` and, if the AutoCorrode pin changed, the ic2 build
and I/Q plugin installation.

**Machine paths.** No committed file contains an absolute path or
credential. Machine paths come from the environment (`ISABELLE_TOOLING_ROOT`,
`AGENT_BOARD_COMMAND`, `PATH`) or from host-local configuration. Link-mode
symlinks and Git hooks are the only files that record absolute paths, and
neither is committed.

## agent-board

**Executable.** `bin/agent-board` in the board repository, a Bash wrapper
that runs `python3 src/agent_board.py`. It needs Python 3.9 or later and
Git, and it finds its own checkout through its resolved path.

**Resolution.** Every caller other than a Git guard finds the executable
the same way:

1. `AGENT_BOARD_COMMAND`, when set and non-empty, must be the absolute path
   of an executable file. It is one path, not shell text, and it takes no
   arguments.
2. Otherwise, `agent-board` on `PATH`.
3. Otherwise the executable is unresolved.

Identity is the resolved real path. The Claude hook launcher and the ic2
notifier do nothing when the executable is unresolved; board doctor and
Isabelle doctor fail when a descriptor exists. Using the board therefore
requires the executable to resolve, and the board's quick start puts
`agent-board` on `PATH` or sets `AGENT_BOARD_COMMAND`. The skill invokes
`"${AGENT_BOARD_COMMAND:-agent-board}"`, and Git guards call their
recorded path.

**Environment.**

- `AGENT_BOARD_AGENT` is the default `--as` handle.
- `AGENT_BOARD_DIR` overrides the storage directory, for tests.
- `AGENT_BOARD_STALE_MINUTES` is the lease, default 180.
- `AGENT_BOARD_COMMAND` is described under "Resolution".

No `ISABELLE_BOARD_*` name is read.

**Storage.** The board lives in `<git common dir>/agent-board/`, shared by
all linked worktrees and outside every working tree:

- `.lock` is the stable flock inode;
- `format` contains `2`;
- `state.json` has `version: 2`;
- `agents/`, `posts/`, `messages/` and `cursors-v2/` complete it.

This is today's on-disk format, only relocated. A board exists when the
directory holds any entry other than `.lock`. Today only `posts`,
`state.json` or `format` count. A board that exists without `state.json`
is incomplete: every verb that reads or writes board state fails with
"incomplete board state", and doctor fails. It is never migrated and never
silently started afresh. Corruption regressions replace the deleted
migration tests: one per top-level entry present without `state.json`,
plus a malformed `state.json` and a wrong `format`.

**CLI.** The global options `--project-root DIR`, `--as HANDLE` and
`--if-board` are kept.

- Kept verbs: `path`, `who`, `claims`, `hello`, `bye`, `post`, `show`,
  `digest`, `claim`, `release`, `guard`, `guard-refs` (internal),
  `install-hook`, `uninstall-hook` and `help`. Their arguments and exit
  semantics are unchanged.
- Removed: `migrate` and the legacy reader behind it.
- New: `version` and the project verbs above.

Resources are a path (relative to the invocation directory), `path:NAME`,
`refs/...`, `token:NAME`, each optionally followed by `#FRAGMENT`. A bare
`jedit` is now an ordinary path. Handles match `[a-z0-9][a-z0-9._-]{0,63}`.
Exit codes: 0 success; 1 a conflict (`claim`, `guard` report `HELD`), or
doctor or check findings; 2 an error. Error messages go to stderr prefixed
`agent-board: `.

**Version and capabilities.** `agent-board version --json` prints one
object:

```json
{
  "interface": 1,
  "state_format": 2,
  "capabilities": ["doctor", "project"],
  "commit": "<40-hex commit, or null outside a Git checkout>",
  "clean": true,
  "executable": "/absolute/real/path/bin/agent-board"
}
```

- `interface` goes up by one on any incompatible change to verbs, options,
  exit codes, environment names or machine-readable output.
- An additive change adds a capability name instead; step 3's bounded
  digests add `bounded-digest`.
- `state_format` is the on-disk version.
- Without `--json`, `version` prints one human-readable line with the same
  facts.

**Doctor.** `agent-board doctor [--project-root DIR] [--allow-dirty]
[--json]` reads only; it needs no Isabelle installation or descriptor.

- The executable's checkout is at `board_revision`, and clean. A dirty
  checkout fails, or is a note with `--allow-dirty`. The executable also
  equals the current resolution, since the digest hook and the ic2 notifier
  use the resolution while the guards use their recorded path.
- `agent-board.conf` exists and is valid; otherwise "not set up" is a
  failure.
- `sync --check` passes; link mode is a note under `--allow-dirty` and
  fails otherwise.
- Storage: none yet is fine (it is created by the first `hello`, `post` or
  `claim`); otherwise it is format 2 and valid, with counts of agents,
  claims and stale claims.
- Git guards, as "Git guards" below describes.

Human output uses `[OK]`, `[NOTE]` and `[FAIL]` lines, as Isabelle doctor
does. `--json` prints:

```json
{"interface": 1, "ok": false,
 "checks": [{"id": "guard.pre-commit", "status": "fail", "message": "…"}]}
```

`status` is `ok`, `note` or `fail`. The check `id`s are stable:

- `executable.resolved`, `executable.revision` and `executable.clean`;
- `descriptor` and `files`;
- `storage`;
- `guard.pre-commit` and `guard.reference-transaction`.

New checks add new ids. With `--json`, stdout carries only that object and
diagnostics go to stderr. The exit status is 0 without a failure, 1 with
one, and 2 when doctor cannot run; it then prints, when it can, an object
with `ok: false` and a single failed check `doctor`. A caller treats exit 2
or unparsable output as a failure. Doctor is strictly read-only: it creates
no file, even temporarily, in the project or the board checkout.

**Git guards.** `install-hook` writes `pre-commit` and
`reference-transaction` into `git rev-parse --git-path hooks`, which follows
`core.hooksPath` (checked with Git 2.43). It refuses unless that directory
lies inside the Git common directory, so a shared or global hooks
directory, or a tracked one such as `.githooks`, is never modified. It
chains an existing foreign hook as `NAME.pre-board`, as today. It installs
both guards or neither: if the second write fails, it restores the first.
Each guard's second line is exactly
`# agent-board guard: remove with agent-board uninstall-hook.`, and each
calls the executable's resolved real path, recorded at installation. A
guard must equal, byte for byte, the wrapper the running executable would
write for that path, so a board update that changes the wrapper asks for
`install-hook` again. A hook without the marker line is foreign; the old
`isabelle-tooling board guard` marker is not recognized.

Doctor's guard checks:

- both guards present, current and calling the resolved executable: pass;
- neither present: a note naming `install-hook`, and likewise an unmarked
  foreign hook with no guard;
- anything else fails: one guard without the other, an edited or outdated
  wrapper, a wrapper calling another executable, or a `.pre-board` backup
  without its guard.

**Project files.**

- `agent-board.conf`.
- `.agent-board/inventory.json`.
- `.agent-board/claude-hook.sh`, mode 755, from `integrations/claude/hook.sh`
  at the pin. It reads the hook JSON on stdin, resolves the executable, runs
  `digest --cursor session-<session id> --mark` (adding `--full` on
  `SessionStart`) from the hook's `cwd` under a five-second timeout, well
  inside the hook's 15 seconds, and always exits 0 with no stderr noise.
- `.agents/skills/agent-coordination/`, the whole directory from
  `skills/agent-coordination/` at the pin, and its Claude alias.
- In `.claude/settings.json`, one element in each of `/hooks/SessionStart`
  and `/hooks/UserPromptSubmit`, with marker `.agent-board/claude-hook.sh`:

  ```json
  {"hooks": [{"type": "command",
              "command": "\"$CLAUDE_PROJECT_DIR/.agent-board/claude-hook.sh\"",
              "timeout": 15}]}
  ```

- The `agent-board` block in the root instruction files:

  ```markdown
  <!-- BEGIN agent-board -->
  <!-- Managed by agent-board; change it with `agent-board update`. -->
  ## Coordination board

  Several agents may work in this repository at once and coordinate
  through `agent-board`. Unless your brief makes you a supervised worker,
  read `.agents/skills/agent-coordination/SKILL.md` before your first task
  here, and again after compaction or when resuming without it. Follow it
  before changing shared files or refs and before delegating.

  A supervised worker's brief says so and names its coordinator. It does
  not use the board or the skill, stays within its assigned scope, and
  makes no commits or ref or index changes; the coordinator coordinates.
  <!-- END agent-board -->
  ```

Codex has no digest hook: agents there run `digest --cursor HANDLE --mark`
as the skill says. `init` neither registers an agent nor
installs guards.

**Renames and removals at extraction.**

- `scripts/board.sh` becomes `bin/agent-board`, and `scripts/board.py`
  becomes `src/agent_board.py`.
- The storage directory `<common>/isabelle-tooling/board` becomes
  `<common>/agent-board`.
- `ISABELLE_BOARD_AGENT`, `_DIR` and `_STALE_MINUTES` become the
  `AGENT_BOARD_*` names.
- The marker `isabelle-tooling board guard` becomes `agent-board guard`.
- The error prefix `board:` becomes `agent-board:`.
- The bare-`jedit` token shorthand and the `label()` special case that
  prints `path:jedit` both go.
- `migrate`, `migrate_legacy` and the legacy detection in `locked()` go.
- `isabelle-coordination` becomes the generic `agent-coordination`.
- `extension/bin/board-hook.sh` becomes `integrations/claude/hook.sh`.
- `extension/hooks/board-hooks.json` and the manifest's `hooks` entry are
  deleted.
- The `.pre-board` chaining suffix, the state format and the other verbs
  keep their names.

## Decisions made in implementation

Recorded on 2026-09-28, with step 3 of the tooling's delivery plan; both
repositories' copies of the shared rules carry the same list.

- `sync --check` exits 1 in link mode, since the files are not the pinned
  ones; `sync --check --allow-dirty` makes link mode a note, which is what
  both doctors use under `--allow-dirty`. It also fails when the runtime
  checkout is not at the pin, as "the running scripts come from that
  revision" requires; board doctor reports that as `executable.revision`
  and folds only the file findings into `files`.
- The inventory records a `marker` with each array-element entry, so an
  entry that a later manifest drops can still be found and removed.
- Installed files get Git's checkout modes, 0777 or 0666 less the umask, so
  a `git restore` after a failure gives the same modes.
- Restore commands are printed as `rm -f`, then `rmdir`, then `git
  restore`, run from the checkout root, so that `git restore` never writes
  through a symlink an install created. A link-mode symlink removed by a
  failed install is not recreated by them; the message says to rerun
  `sync --link`.
- `update` refuses in link mode; `sync` first restores the copies.
- `remove` without an inventory refuses, and `remove` also refuses when
  the inventory directory holds files it did not install.
- An emptied JSON file is deleted, and so is a TOML file left empty by
  removing its block; directories left empty by deletions are removed,
  never the checkout root.
- There is no gitignored local state yet, so neither inventory directory
  has its own `.gitignore`.
