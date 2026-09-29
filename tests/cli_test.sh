#!/usr/bin/env bash
set -euo pipefail

# board.sh on a temporary repository with a linked worktree: resolution,
# presence, claims, the guard, the pre-commit hook, staleness, posts and the
# digest cursor. Real git, no Isabelle.

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=test_lib.sh disable=SC1091
source "$TEST_DIR/test_lib.sh"

BOARD="$TEST_DIR/../scripts/board.sh"
unset ISABELLE_BOARD_DIR ISABELLE_BOARD_AGENT ISABELLE_BOARD_STALE_MINUTES
export HOME="$TEST_TMP_DIR/home"
export GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
mkdir -p "$HOME"

REPO="$PROJECT"
git -C "$REPO" init -q -b main
git -C "$REPO" add -A
git -C "$REPO" commit -q -m "descriptor and session"
printf 'plan\n' >"$REPO/PLAN.md"
git -C "$REPO" add PLAN.md
git -C "$REPO" commit -q -m plan
WT="$TEST_TMP_DIR/wt"
git -C "$REPO" worktree add -q -b feature "$WT"

board_main() { (cd "$REPO" && "$BOARD" "$@"); }
board_wt() { (cd "$WT" && "$BOARD" "$@"); }
board_main_env() { (cd "$REPO" && ISABELLE_BOARD_AGENT="$1" "$BOARD" "${@:2}"); }
post_stdin() { printf 'line one\nline two\n' | (cd "$WT" && "$BOARD" --as wt post -); }
assert_silent() {
  [[ ! -s "$TEST_TMP_DIR/$1.out" ]] || { echo "FAIL: $1: expected no output" >&2; cat "$TEST_TMP_DIR/$1.out" >&2; exit 1; }
}
assert_output_lacks() {
  if grep -Fq -- "$2" "$TEST_TMP_DIR/$1.out"; then
    echo "FAIL: $1: output must not contain: $2" >&2
    exit 1
  fi
}

# --- resolution ---------------------------------------------------------------------

BOARD_DIR="$REPO/.git/isabelle-tooling/board"
[[ "$(board_main path)" == "$BOARD_DIR" ]] || fail "path from main: $(board_main path)"
[[ "$(board_wt path)" == "$BOARD_DIR" ]] || fail "path from worktree differs: $(board_wt path)"
[[ "$("$BOARD" --project-root "$WT" path)" == "$BOARD_DIR" ]] || fail "path with --project-root"
run_and_capture 2 nogit "$BOARD" --project-root "$HOME" path
assert_output_contains nogit "not inside a Git worktree"
run_and_capture 0 envdir env ISABELLE_BOARD_DIR="$TEST_TMP_DIR/board2" "$BOARD" --project-root "$HOME" --as x hello --task t
[[ -f "$TEST_TMP_DIR/board2/agents/x" ]] || fail "ISABELLE_BOARD_DIR not honoured"

# --- no board yet: read-only actions and the guard are silent successes ------------

run_and_capture 0 who_empty board_main who
assert_output_contains who_empty "no board yet"
run_and_capture 0 digest_empty board_main digest --cursor c0
assert_silent digest_empty
run_and_capture 0 guard_empty board_wt guard --staged
assert_silent guard_empty
run_and_capture 0 ifboard board_main --if-board --as ic2 post --kind server "server starting"
assert_silent ifboard
[[ ! -d "$BOARD_DIR" ]] || fail "--if-board must not create the board"

# --- presence -----------------------------------------------------------------------

run_and_capture 2 hello_nohandle board_main hello --task x
assert_output_contains hello_nohandle "needs an agent handle"
run_and_capture 2 hello_badhandle board_main --as 'Bad Name' hello --task x
assert_output_contains hello_badhandle "invalid handle"
run_and_capture 2 hello_notask board_main --as main hello
assert_output_contains hello_notask "needs --task"
run_and_capture 0 hello_main board_main --as main hello --task "editing PLAN.md"
run_and_capture 0 hello_wt board_wt --as wt hello --task "feature work"
run_and_capture 0 who board_main who
assert_output_contains who "main  $REPO (main)"
assert_output_contains who "wt  $WT (feature)"
assert_output_contains who "feature work"

# --- claims -------------------------------------------------------------------------

run_and_capture 0 claim_main board_main --as main claim --reason "milestone rewrite" PLAN.md formal refs/heads/feature 'PLAN.md#status'
assert_output_contains claim_main "claimed PLAN.md"
assert_output_contains claim_main "claimed formal/"
assert_output_contains claim_main "claimed refs/heads/feature"
assert_output_contains claim_main "claimed PLAN.md#status"
run_and_capture 1 claim_wt board_wt --as wt claim --reason mine PLAN.md
assert_output_contains claim_wt "HELD: PLAN.md is held by main"
run_and_capture 0 claim_again board_main --as main claim --reason "still on it" PLAN.md
assert_output_contains claim_again "already held by you"
run_and_capture 0 claims board_main claims
assert_output_contains claims "PLAN.md  held by main"
assert_output_contains claims "still on it"
run_and_capture 2 claim_outside board_main --as main claim --reason x /
assert_output_contains claim_outside "outside the worktree"
run_and_capture 2 claim_noreason board_main --as main claim PLAN.md
assert_output_contains claim_noreason "needs --reason"
run_and_capture 2 release_foreign board_wt --as wt release PLAN.md
assert_output_contains release_foreign "held by main, not by you"

# --- guard on explicit paths --------------------------------------------------------

run_and_capture 1 guard_nested board_wt --as wt guard formal/Test/X.thy
assert_output_contains guard_nested "refusing: formal/ is claimed by main"
run_and_capture 0 guard_own board_main --as main guard PLAN.md formal/Test/X.thy
assert_silent guard_own
run_and_capture 0 guard_unrelated board_wt --as wt guard README.md
assert_silent guard_unrelated

# --- guard on the staged commit, identity inference ---------------------------------

printf 'change\n' >>"$WT/PLAN.md"
git -C "$WT" add PLAN.md
run_and_capture 1 guard_staged_wt board_wt --as wt guard --staged
assert_output_contains guard_staged_wt "refusing: PLAN.md is claimed by main"
assert_output_contains guard_staged_wt "refusing: refs/heads/feature is claimed by main"
run_and_capture 1 guard_inferred_wt board_wt guard --staged
assert_output_contains guard_inferred_wt "you are taken to be wt"
printf 'change\n' >>"$REPO/PLAN.md"
git -C "$REPO" add PLAN.md
run_and_capture 0 guard_inferred_main board_main guard --staged
assert_silent guard_inferred_main
run_and_capture 0 hello_main2 board_main --as main2 hello --task "also in the main worktree"
run_and_capture 1 guard_ambiguous board_main guard --staged
assert_output_contains guard_ambiguous "your handle is unknown"
run_and_capture 0 guard_env board_main_env main guard --staged
assert_silent guard_env
run_and_capture 0 bye_main2 board_main --as main2 bye
run_and_capture 0 guard_after_bye board_main guard --staged
assert_silent guard_after_bye

# --- the pre-commit hook ------------------------------------------------------------

HOOK="$REPO/.git/hooks/pre-commit"
run_and_capture 0 install board_main install-hook
assert_output_contains install "installed $HOOK"
run_and_capture 1 commit_blocked git -C "$WT" commit -q -m "change"
assert_output_contains commit_blocked "refusing: PLAN.md is claimed by main"
run_and_capture 0 commit_main git -C "$REPO" commit -q -m "main change"
run_and_capture 0 release_main board_main --as main release PLAN.md refs/heads/feature
assert_output_contains release_main "released PLAN.md"
assert_output_contains release_main "released refs/heads/feature"
run_and_capture 0 release_absent board_main --as main release PLAN.md
assert_output_contains release_absent "not claimed: PLAN.md"
run_and_capture 0 guard_advisory board_wt --as wt guard PLAN.md
assert_output_contains guard_advisory "holds a passage of PLAN.md (status)"
run_and_capture 0 commit_wt git -C "$WT" commit -q -m "feature change"
run_and_capture 0 reinstall board_main install-hook
run_and_capture 0 uninstall board_main uninstall-hook
assert_output_contains uninstall "removed $HOOK"
[[ ! -e "$HOOK" ]] || fail "hook not removed"
printf '#!/usr/bin/env bash\ntouch "%s/foreign-ran"\n' "$TEST_TMP_DIR" >"$HOOK"
chmod +x "$HOOK"
run_and_capture 2 install_conflict board_main install-hook
assert_output_contains install_conflict "already exists"
run_and_capture 0 install_force board_main install-hook --force
assert_output_contains install_force "chained the previous hook"
printf 'more\n' >>"$WT/PLAN.md"
git -C "$WT" add PLAN.md
run_and_capture 0 commit_chain git -C "$WT" commit -q -m more
[[ -f "$TEST_TMP_DIR/foreign-ran" ]] || fail "chained foreign hook did not run"
run_and_capture 0 uninstall2 board_main uninstall-hook
assert_output_contains uninstall2 "restored the previous"
grep -q foreign-ran "$HOOK" || fail "foreign hook not restored"
run_and_capture 0 uninstall3 board_main uninstall-hook
assert_output_contains uninstall3 "no board hook installed"

# --- staleness and takeover ---------------------------------------------------------

touch -d '-4 hours' "$BOARD_DIR/agents/main"
run_and_capture 0 claims_stale board_main claims
assert_output_contains claims_stale "formal/  held by main"
assert_output_contains claims_stale "[stale]"
run_and_capture 0 guard_stale board_wt --as wt guard formal/Test/X.thy
assert_output_contains guard_stale "ignoring stale claim on formal/ by main"
run_and_capture 0 takeover board_wt --as wt claim --reason "taking over" formal
assert_output_contains takeover "took over formal/ from main (stale"
run_and_capture 1 claim_active board_main --as main claim --reason back formal
assert_output_contains claim_active "HELD: formal/ is held by wt"
run_and_capture 0 claim_force board_main --as main claim --force --reason back formal
assert_output_contains claim_force "took over formal/ from wt (forced)"
run_and_capture 0 claims_fresh board_main claims
assert_output_lacks claims_fresh "[stale]"

# --- posts, show, digest ------------------------------------------------------------

run_and_capture 0 post board_wt --as wt post --kind handoff --re PLAN.md "branch feature ready"
assert_output_contains post "posted "
run_and_capture 2 post_badkind board_wt --as wt post --kind 'Bad Kind' x
assert_output_contains post_badkind "invalid kind"
run_and_capture 2 post_empty board_wt --as wt post " "
assert_output_contains post_empty "needs a message"
run_and_capture 0 post_stdin post_stdin
run_and_capture 0 show board_main show --last 5
assert_output_contains show "Posts (5 of"
assert_output_contains show "[handoff]  re: PLAN.md"
assert_output_contains show "    branch feature ready"
assert_output_contains show "    line two"
run_and_capture 0 show_all board_main show --all
assert_output_contains show_all "[hello]"
run_and_capture 0 digest_first board_main digest --cursor c1 --mark
assert_output_contains digest_first "new post(s)"
assert_output_contains digest_first "Claims"
run_and_capture 0 digest_quiet board_main digest --cursor c1 --mark
assert_silent digest_quiet
run_and_capture 0 post2 board_wt --as wt post "another note"
run_and_capture 0 digest_new board_main digest --cursor c1
assert_output_contains digest_new ": 1 new post(s)"
assert_output_contains digest_new "    another note"
assert_output_lacks digest_new "branch feature ready"
run_and_capture 0 digest_full board_main digest --cursor c1 --full
assert_output_contains digest_full "branch feature ready"
run_and_capture 2 digest_badcursor board_main digest --cursor 'a b'
assert_output_contains digest_badcursor "invalid cursor"

# --- bye ----------------------------------------------------------------------------

run_and_capture 0 bye_main board_main --as main bye "done for today"
assert_output_contains bye_main "formal/"
assert_output_contains bye_main "PLAN.md#status"
run_and_capture 0 who_after board_main who
assert_output_lacks who_after "  main  "
assert_output_contains who_after "  wt  "
run_and_capture 0 claims_after board_main claims
assert_output_lacks claims_after "held by main"

# --- nothing written under HOME, nothing into a working tree ------------------------

[[ -z "$(find "$HOME" -mindepth 1)" ]] || fail "board wrote under HOME: $(find "$HOME" -mindepth 1)"
[[ -z "$(git -C "$REPO" status --porcelain --untracked-files=all | grep -v '^ M PLAN.md$' || true)" ]] || fail "unexpected files in the main worktree: $(git -C "$REPO" status --porcelain)"
[[ -z "$(git -C "$WT" status --porcelain --untracked-files=all)" ]] || fail "unexpected files in the linked worktree: $(git -C "$WT" status --porcelain)"

echo "board.sh tests passed"
