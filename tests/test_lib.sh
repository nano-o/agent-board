#!/usr/bin/env bash
# Shared setup for the shell tests: a disposable directory and an isolated
# Git configuration and HOME.

TEST_TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agent-board-test.XXXXXX")"
trap 'rm -rf -- "$TEST_TMP_DIR"' EXIT
TEST_TMP_DIR="$(cd "$TEST_TMP_DIR" && pwd -P)"

while IFS= read -r name; do
  unset "$name"
done < <(env | sed -n 's/^\(GIT_[A-Za-z0-9_]*\)=.*/\1/p')
export HOME="$TEST_TMP_DIR/home"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
mkdir -p "$HOME"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

run_and_capture() {
  local expected_rc="$1"
  local name="$2"
  shift 2
  local output_file="$TEST_TMP_DIR/$name.out"
  local actual_rc
  if "$@" >"$output_file" 2>&1; then
    actual_rc=0
  else
    actual_rc=$?
  fi
  if [[ "$actual_rc" -ne "$expected_rc" ]]; then
    echo "FAIL: $name: expected rc=$expected_rc, found rc=$actual_rc" >&2
    sed -n '1,120p' "$output_file" >&2
    return 1
  fi
}

assert_output_contains() {
  local name="$1"
  local expected="$2"
  if ! grep -Fq -- "$expected" "$TEST_TMP_DIR/$name.out"; then
    echo "FAIL: $name: output did not contain: $expected" >&2
    sed -n '1,120p' "$TEST_TMP_DIR/$name.out" >&2
    return 1
  fi
}
