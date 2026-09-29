#!/usr/bin/env bash
# Claude Code hook for SessionStart and UserPromptSubmit: print what is new
# on the repository's board since this session last looked; stdout becomes
# context for the agent. A project installs it as .agent-board/claude-hook.sh.
# It never blocks a prompt: without a resolvable agent-board, outside a Git
# repository or without a board it prints nothing. It always exits 0 and
# never writes to stderr.
set -uo pipefail
exec 2>/dev/null
# The executable: AGENT_BOARD_COMMAND (one absolute path), else PATH.
cmd="${AGENT_BOARD_COMMAND:-}"
if [[ -n "$cmd" ]]; then
  [[ "$cmd" == /* && -f "$cmd" && -x "$cmd" ]] || exit 0
else
  cmd="$(command -v agent-board)" || exit 0
  [[ "$cmd" == /* ]] || exit 0
fi
input="$(cat || true)"
# Hook input is JSON on stdin: session_id, cwd, hook_event_name, and more.
fields="$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
for key in ("session_id", "cwd", "hook_event_name"):
    print(str(data.get(key, "")).replace("\n", " "))
' || printf '\n\n\n')"
session="$(printf '%s\n' "$fields" | sed -n '1p' | tr -cd 'A-Za-z0-9._-')"
cwd="$(printf '%s\n' "$fields" | sed -n '2p')"
event="$(printf '%s\n' "$fields" | sed -n '3p')"
[[ -d "$cwd" ]] || cwd="$PWD"
args=(--cursor "session-${session:-unknown}" --mark)
# A session that starts, resumes or compacts has no memory of earlier posts:
# show the board state, not only the delta.
[[ "$event" != "SessionStart" ]] || args+=(--full)
# The board's lock can be held for a while; stay well inside the hook's 15 s.
(cd "$cwd" && timeout 5 "$cmd" digest "${args[@]}") || true
exit 0
