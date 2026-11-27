#!/usr/bin/env bash
# SessionStart hook (matcher "startup|resume|clear"): the NATS login approval that is
# waiting right now, as one line in the agent's terminal (#198).
#
# The MCP server holds the device grant while the human has to approve the login
# (decision 10, RT-6.34). It reaches the log and the human's DM, but not the TUI the
# person works in, so this hook asks for it (GET /v1/login) and shows the link and
# the code — never a token — as a systemMessage, then marks it shown, so the next
# turn's notice does not show it again (POST /v1/login/shown, once per login).
#
# A grant may only start after this hook ran (a later re-login): then there is nothing
# here, and the turn's end shows the same one line.
#
# Fails open: no server, no answer, no jq, an API error — exit 0 without output.
set -uo pipefail

# shellcheck source=../bin/mm-agent-lib.sh
CB_ENV_QUIET=1 CB_ENV_DEFER=1 source "$(cd "$(dirname "$0")/../bin" && pwd)/mm-agent-lib.sh"
mm_read_hook_input            # drains the payload; only its session id is used
[[ -z "$CB_ENV_ERR" ]] || exit 0   # names that differ (#169): logged, the hook stays out

# The hook has 5 s (hooks.json); its two calls share 3, so a slow server never holds
# the session's start.
mm_env_set CB_BUS_DEADLINE MM_BUS_DEADLINE "$(( $(date +%s) + 3 ))"
mm_bus GET '/v1/login' || exit 0
[[ "$CB_BUS_STATUS" == 200 ]] || exit 0

# The server's own wording when it gives one (#198), so every harness shows the same
# line; an older server's answer carries only the link and the code.
text="$(jq -r '.line // empty | strings' <<<"$CB_BUS_BODY" 2>/dev/null)" || exit 0
if [[ -z "$text" ]]; then
  link="$(jq -r '.link // empty | strings' <<<"$CB_BUS_BODY" 2>/dev/null)" || exit 0
  code="$(jq -r '.user_code // empty | strings' <<<"$CB_BUS_BODY" 2>/dev/null)" || exit 0
  [[ -n "$link" ]] || exit 0
  if [[ -n "$code" ]]; then
    text="Bus login: open ${link} — the page shows the code ${code}. Approve only if it matches."
  else
    text="Bus login: open ${link}. Approve only if it is your agent's login."
  fi
fi
# The line first, so a failed jq never marks a line that was not shown.
out="$(jq -nc --arg m "$text" '{systemMessage: $m, suppressOutput: true}')" || exit 0
mm_stat post chars="${#text}" w="other" 2>/dev/null || true
mm_bus POST '/v1/login/shown' '{}' >/dev/null 2>&1 || true
printf '%s\n' "$out"
exit 0
