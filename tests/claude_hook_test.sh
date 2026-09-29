#!/usr/bin/env bash
set -euo pipefail

# integrations/claude/hook.sh against a real board: executable resolution,
# session cursors, SessionStart replay, malformed input, and the five-second
# bound when the board's lock is held. It always exits 0 without stderr.

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=test_lib.sh disable=SC1091
source "$TEST_DIR/test_lib.sh"

HOOK="$TEST_DIR/../integrations/claude/hook.sh"
BOARD="$(readlink -f "$TEST_DIR/../bin/agent-board")"
unset AGENT_BOARD_DIR AGENT_BOARD_AGENT AGENT_BOARD_STALE_MINUTES AGENT_BOARD_COMMAND

REPO="$TEST_TMP_DIR/project"
mkdir -p "$REPO"
git -C "$REPO" init -q -b main
git -C "$REPO" commit -q --allow-empty -m initial
BOARD_DIR="$REPO/.git/agent-board"
# A PATH with the usual tools but no agent-board.
BASE_PATH="$(printf '%s' "$PATH" | tr ':' '\n' | while IFS= read -r dir; do
  [[ -x "$dir/agent-board" ]] || printf '%s:' "$dir"; done)"
BASE_PATH="${BASE_PATH%:}"

# hook NAME EVENT SESSION [ENV...]: run the hook from HOME with JSON input
# naming the repository; stdout and stderr land in NAME.out and NAME.err.
hook() {
  local name="$1" event="$2" session="$3"
  shift 3
  printf '{"session_id": "%s", "cwd": "%s", "hook_event_name": "%s"}' \
    "$session" "$REPO" "$event" |
    (cd "$HOME" && env PATH="$BASE_PATH" "$@" "$HOOK") \
      >"$TEST_TMP_DIR/$name.out" 2>"$TEST_TMP_DIR/$name.err" ||
    fail "$name: the hook exited $?"
  [[ ! -s "$TEST_TMP_DIR/$name.err" ]] || fail "$name: stderr: $(cat "$TEST_TMP_DIR/$name.err")"
}
silent() { [[ ! -s "$TEST_TMP_DIR/$1.out" ]] || fail "$1: expected no output: $(cat "$TEST_TMP_DIR/$1.out")"; }

# --- resolution ---------------------------------------------------------------------

hook unresolved SessionStart s1
silent unresolved
hook relative SessionStart s1 AGENT_BOARD_COMMAND=bin/agent-board
silent relative
hook missing SessionStart s1 AGENT_BOARD_COMMAND="$TEST_TMP_DIR/nothing"
silent missing
hook no_board SessionStart s1 AGENT_BOARD_COMMAND="$BOARD"
silent no_board
[[ ! -e "$BOARD_DIR" ]] || fail "the hook created a board"

# --- digests ------------------------------------------------------------------------

"$BOARD" --project-root "$REPO" --as alice post "first note" >/dev/null
hook start SessionStart s1 AGENT_BOARD_COMMAND="$BOARD"
assert_output_contains start "first note"
hook prompt UserPromptSubmit s1 AGENT_BOARD_COMMAND="$BOARD"
silent prompt
"$BOARD" --project-root "$REPO" --as alice post "second note" >/dev/null
hook prompt2 UserPromptSubmit s1 AGENT_BOARD_COMMAND="$BOARD"
assert_output_contains prompt2 "second note"
if grep -Fq "first note" "$TEST_TMP_DIR/prompt2.out"; then fail "prompt2 repeated a read post"; fi
hook restart SessionStart s1 AGENT_BOARD_COMMAND="$BOARD"
assert_output_contains restart "first note"
mkdir -p "$TEST_TMP_DIR/path-bin"
ln -s "$BOARD" "$TEST_TMP_DIR/path-bin/agent-board"
printf '{"session_id": "s2", "cwd": "%s", "hook_event_name": "SessionStart"}' "$REPO" |
  PATH="$TEST_TMP_DIR/path-bin:$BASE_PATH" "$HOOK" >"$TEST_TMP_DIR/on_path.out" 2>"$TEST_TMP_DIR/on_path.err"
assert_output_contains on_path "second note"
[[ ! -s "$TEST_TMP_DIR/on_path.err" ]] || fail "on_path: stderr"

# --- malformed input: the hook's own directory and an unknown session ---------------

for input in 'not json' '[1, 2]' ''; do
  printf '%s' "$input" | (cd "$REPO" && AGENT_BOARD_COMMAND="$BOARD" "$HOOK") \
    >"$TEST_TMP_DIR/garbage.out" 2>"$TEST_TMP_DIR/garbage.err" || fail "garbage: exit $?"
  [[ ! -s "$TEST_TMP_DIR/garbage.err" ]] || fail "garbage: stderr"
done
[[ -f "$BOARD_DIR/cursors-v2/session-unknown" ]] || fail "no session-unknown cursor"

# --- a held lock: bounded, silent, nothing acknowledged -----------------------------

"$BOARD" --project-root "$REPO" --as alice post "third note" >/dev/null
mkfifo "$TEST_TMP_DIR/locked"
python3 -c '
import fcntl, sys, time
lock = open(sys.argv[1], "a")
fcntl.flock(lock, fcntl.LOCK_EX)
with open(sys.argv[2], "w") as ready:
    ready.write("locked\n")
time.sleep(60)
' "$BOARD_DIR/.lock" "$TEST_TMP_DIR/locked" &
holder=$!
read -r _ <"$TEST_TMP_DIR/locked"
started=$(date +%s%N)
hook held UserPromptSubmit s1 AGENT_BOARD_COMMAND="$BOARD"
elapsed_ms=$((($(date +%s%N) - started) / 1000000))
kill "$holder"
wait "$holder" 2>/dev/null || true
silent held
((elapsed_ms >= 4500 && elapsed_ms < 8000)) || fail "held: the hook took ${elapsed_ms} ms"
hook after_held UserPromptSubmit s1 AGENT_BOARD_COMMAND="$BOARD"
assert_output_contains after_held "third note"

echo "Claude hook tests passed"
