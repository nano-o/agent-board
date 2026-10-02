# Validation records

`stable` names the commit that appended the newest entry below. Each entry
is a validation record commit (see "`stable` refs and the validation
record" in [project-integration.md](project-integration.md)): its parent is
the validated commit, and its only change is the entry. The mechanical
checks are rerun at exactly that commit before `stable` moves to it.

## 2026-09-29: the first `stable`

Candidates
: agent-board `6a467ea`, this commit's parent. The fixtures ran at
  `c6b934c`, which differs from it only in the README's status. Board CLI
  interface 1, state format 2, capabilities `bounded-digest`, `doctor` and
  `project`.
: isabelle-formal-modeling-tooling `2d10fe9`. The fixtures ran at its
  candidates up to `e25d1ef`; see that repository's `docs/validation.md`.

Hosts
: Claude Code 2.1.284 with claude-opus-5-5, in auto permission mode.
: Codex CLI 0.155.1 with gpt-6-astra at medium reasoning effort, in the
  workspace-write sandbox with automatic review.
: Git 2.43, Python 3.12.3, on Linux.

Setup
: Fresh host configuration roots; the board executable from this
  checkout, on `PATH` and in `AGENT_BOARD_COMMAND`.
: Fixtures: a bare `git init` repository and a stellar-core clone, with
  and without the Isabelle tooling.

Passed
: The board alone (init, guards, doctor, `sync --check`, hello, claim,
  digest, release, bye, remove), and with the tooling in each installation
  order.
: A pin mismatch on each side, failing only its own doctor, and the
  tooling's doctor relaying board doctor's check ids.
: Removing either component while keeping the other.
: One copy of the coordination skill on both hosts, from the root and from
  a subdirectory.
: Digests: on Claude Code the hook shows the board at session start and a
  new post exactly once on the next prompt; on Codex the agent runs the
  digest itself.
: Once per host: an ordinary task, claiming before editing; the same after
  resume; an independent worker; a supervised worker, the coordinator
  claiming first and committing; two supervised workers with disjoint
  scopes; a scope-expansion request (Claude Code; the Codex coordinator
  took the extra files itself); an unreachable coordinator, the worker
  pausing; a transfer to independent mode; and a non-worker brief, which
  coordinated normally.
: The ic2 notifier posting from a proof worker's ic2 server.

Observations
: Codex encrypts spawn messages in its rollouts, so worker briefs were
  judged by behaviour; it may also give a worker the parent's whole
  conversation.
: Codex's sandbox protects `.git`: board writes and commits went through
  its automatic review.
: Each behavioural scenario ran once per host, not three times.

## 2026-09-29: unchanged refs pass the ref guard

Candidates
: agent-board `7dae935`, this commit's parent: the ref guard skips an
  update whose old and new values are equal, so `git worktree add -b` of
  a branch its creator claimed passes in the new, unregistered worktree.
: isabelle-formal-modeling-tooling `245550f`; see that repository's
  `docs/validation.md`.

Hosts
: Claude Code 2.1.285 with claude-opus-5-5, in auto permission mode.
: Codex CLI 0.159.2 with gpt-6-astra at medium reasoning effort, in the
  workspace-write sandbox with automatic review.
: Isabelle2025-2, Git 2.43, Python 3.12.3, on Linux.

Setup
: The tooling's fixture environment, with this checkout's executable in
  `AGENT_BOARD_COMMAND` and on `PATH`, and the stellar-core clone pinned
  at both candidates.

Passed
: `make validate`, including a regression test in which the owner of a
  claimed branch runs `git worktree add -b` for it without
  `AGENT_BOARD_AGENT`, another agent is still refused, an unchanged
  `update-ref` passes and an unverified deletion is refused.
: The tooling's mechanical checks: 76 on the bare repository and 75 on
  the stellar-core clone, all but the check of `init` at the previous
  `stable`, whose doctors fail while the runtimes are at the candidates.
: On both hosts: the worker smoke proof through ic2 and the board, with
  no guard refusal.

Not rerun
: The behavioural scenarios and link mode: the skill, the hook and the
  link code are as validated for the first `stable`.

Observations
: Neither smoke coordinator took the path refused in the tooling's step
  6. Claude Code's created the worktree before the branch was claimed,
  and its worker then claimed it; Codex's set `AGENT_BOARD_AGENT` on
  `git worktree add`. The regression test covers that path.

## 2026-10-01: the Apache 2.0 license, and no machine paths in the docs

Candidates
: agent-board `bc84193`, this commit's parent: the Apache License 2.0 as
  `LICENSE`, a `NOTICE` naming Giuliano Losa as copyright holder, a quick
  start that clones from GitHub, and the docs without local paths or the
  names of private projects.
: isabelle-formal-modeling-tooling `cabf405`, the same license and
  documentation change, with its own copyright holder; see that
  repository's `docs/validation.md`.

Hosts
: None: no host session ran, because nothing a host loads changed.
: Isabelle2025-2, Git 2.43, Python 3.12.3, on Linux.

Setup
: The tooling's fixture environment, with this checkout's executable in
  `AGENT_BOARD_COMMAND` and on `PATH`. The stellar-core clone is now a
  clone of public stellar-core `release/v29.0.0`, pinned at both
  candidates.

Passed
: `make validate`.
: The tooling's mechanical checks: 76 on the bare repository and 75 on
  the stellar-core clone, all but the check of `init` at the previous
  `stable`, whose doctors fail while the runtimes are at the candidates.

Not rerun
: The behavioural scenarios, link mode and the worker smoke proof: only
  `LICENSE`, `NOTICE`, the README, `PROVENANCE.md` and the docs changed,
  and not the skill, the hook, the guards or the link code.
