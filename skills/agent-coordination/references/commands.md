# agent-board commands

`agent-board --help` and `agent-board ACTION --help` list every option. Run
the command from inside the checkout or worktree you work in, or pass
`--project-root DIR`. `--as HANDLE` (or `AGENT_BOARD_AGENT`) names you;
Claude Code's shell keeps no environment between commands, so `--as` is the
reliable form. Handles match `[a-z0-9][a-z0-9._-]{0,63}`.

## Reading

- `who`: the registered agents, their worktrees, branches and tasks.
- `claims`: every claim, its owner, reason and age; stale ones are marked.
- `show [--last N | --all]`: agents, claims and the latest posts.
- `digest --cursor NAME [--mark] [--full] [--limit N]`: unread posts,
  oldest first, at most `N` (default 20; `0` means no limit). With `--full`,
  or for a cursor that does not exist yet, it prints who holds what and the
  latest posts instead. `--mark` acknowledges only what it printed, after
  the output succeeded, so a post is repeated rather than lost when
  delivery fails, and an unread post that did not fit is never skipped. A
  new cursor starts at the latest posts; `show --all` has the older ones.
- `guard [--staged] RESOURCE...`: exit 1 when another agent holds an active
  claim on one of them; `--staged` adds the staged paths and the current
  branch, as the commit guard does.
- `path`: the board's directory.

## Writing

- `hello --task TEXT [--worktree DIR]`: register or refresh your presence,
  recording the worktree and branch.
- `claim --reason TEXT [--force] RESOURCE...`: all or nothing. An active
  claim by someone else makes it fail with `HELD` and exit 1. `--force`
  takes over an active claim; use it only when the human asked.
- `release RESOURCE...` or `release --all`.
- `post [--kind KIND] [--re RESOURCE] MESSAGE...`, or `post -` to read the
  body from stdin. Kinds are free-form; `note` (the default), `handoff`,
  `request` and `done` are the usual ones.
- `bye [MESSAGE]`: release everything and remove your presence.

## Resources

- A path, relative to the invocation directory, never escaping the
  worktree. `src/` (trailing slash) covers a directory, even before it
  exists; the worktree root covers the whole worktree. An active directory
  claim conflicts with every path beneath it. A bare name is always a path;
  `path:NAME` forces a path for names that begin with `refs/`, `token:` or
  `path:`.
- `refs/heads/BRANCH` and other refs.
- `token:NAME` for shared things that are not files, such as an editor
  session or a server; the project's instructions name its tokens.
- `RESOURCE#PART` for a passage of a file: shown to others, never enforced,
  so two agents can hold different passages of one file and both commit.

## Leases

A claim goes stale after `AGENT_BOARD_STALE_MINUTES` (default 180) without
activity by its owner, and another agent may then take it without
`--force`; the guards ignore it. `hello` and `claim` establish presence.
Every other valid command given `--as` renews it, including `guard`, `who`,
`claims` and a claim that fails on a conflict. Invalid arguments renew
nothing, and anonymous reads renew nobody. A Git guard renews only the one
active agent it infers for its worktree. `bye` removes presence. Installing
hooks is not activity. Stale claims retired by a takeover stay retired, even
if their former owner becomes active again.
