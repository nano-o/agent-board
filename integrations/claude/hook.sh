#!/usr/bin/env bash
# Claude Code hook: print what is new on the repository's coordination board
# since this session last looked. Declared in hooks/board-hooks.json for
# SessionStart and UserPromptSubmit; its stdout becomes context for the agent.
# It never blocks a prompt: without ISABELLE_TOOLING_ROOT, outside a Git
# repository, or without a board it prints nothing and exits 0.
set -uo pipefail
root="${ISABELLE_TOOLING_ROOT:-}"
board="$root/scripts/board.sh"
[[ -n "$root" && -x "$board" ]] || exit 0
input="$(cat 2>/dev/null || true)"
# Hook input is JSON on stdin: session_id, cwd, hook_event_name, and more.
fields="$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
for key in ("session_id", "cwd", "hook_event_name"):
    print(str(data.get(key, "")).replace("\n", " "))
' 2>/dev/null || printf '\n\n\n')"
session="$(printf '%s\n' "$fields" | sed -n '1p' | tr -cd 'A-Za-z0-9._-')"
cwd="$(printf '%s\n' "$fields" | sed -n '2p')"
event="$(printf '%s\n' "$fields" | sed -n '3p')"
[[ -d "$cwd" ]] || cwd="$PWD"
args=(--cursor "session-${session:-unknown}" --mark)
# A session that starts, resumes or compacts has no memory of earlier posts:
# show the board state, not only the delta.
[[ "$event" != "SessionStart" ]] || args+=(--full)
(cd "$cwd" && "$board" digest "${args[@]}") 2>/dev/null || true
exit 0
