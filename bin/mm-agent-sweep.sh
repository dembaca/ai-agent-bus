#!/usr/bin/env bash
# Clean up agents a failed teardown left behind.
#
#   mm-agent-sweep.sh            # dry run: list what would be removed
#   mm-agent-sweep.sh --apply    # actually unregister
#   mm-agent-sweep.sh --apply --force   # include sessions that still look live
#
# A bot an agent registered itself (mcp-*.env) outlives its process for
# `claude --continue` or its workspace's next server; it counts as abandoned
# only once its file is untouched for CB_SESSION_KEEP_SEC (default 24 h).
# So does a corral session's bot that postEnd kept for `claude --continue`
# (RT-6.18 step 9): its file carries CB_SERVER_ENDED, and the grace period
# counts from that mark. --force includes them sooner.
#
# A session file exists for the whole run of a session, so it is NOT evidence
# that a bot is abandoned. Each file records CB_SESSION_PID, the supervising
# corral process; while that process is alive the session is in use and is
# skipped. Without that check a sweep unregisters the bot of the session it is
# running alongside, killing a live agent's credentials mid-task.
#
# Two sources, in this order:
#   1. Session files whose process is gone. Each holds the bot's own token,
#      which the registrar accepts for deleting that bot — so this needs no
#      MM_REG_SECRET, the normal case on a gopass setup.
#   2. With MM_REG_SECRET set, agent-* accounts (the registrar's prefix) with
#      no session file at all. Required rather than convenient: a bot token
#      targeting another agent is answered 401.
#
# Per-bot state files next to them (asks-, coord-, gate-, last-, listen-, presence-, turn-, update-<uid>.env,
# stats-<uid>.jsonl, permission-<uid>.log) hold no token and are not sessions. Last, the sweep removes
# those whose bot has no session file left and that were untouched for a day: a
# live session keeps writing them, and one whose token was exported by hand has
# no session file but would lose its crown lease if they went sooner.
#
# Since plan B piece 2b (RT-6.2 step 9) a session keys its kept state by its NATS
# name (25 lower-case base32 characters) instead of the bot id: any
# <kind>-<name>.env/.jsonl/.log/.lock, by that pattern, whatever the kind, so
# files that move to the name later need no change here (RT-6.1 step 7). A
# running MCP server names its key in its endpoint file (CB_STATE_KEY); the
# marker copied-<name>.env ties a name to its bots (CB_COPIED_FROM=<bot id>, one
# line per bot that used it). A name's files go when no running server names it,
# its marker names no bot with a session file or a running MCP server and none
# of them was touched for a day; without a marker nothing ties the name to a
# session, so they stay (--force includes them by the day rule). A name whose
# marker says a later start of its session took it over (CB_TAKEN_BY=<name>,
# RT-6.2 step 9) is tied to no bot: its files go by the day rule.
#
# A keyless corral session (RT-6.18) has its MCP server running on the host:
# its session file records CB_BUS_HOST_PID, and the process keeps its files in
# <sid>.host.json, <sid>.host.log and hosted/<sid>/. While that process runs,
# its session is left alone (it ends with its watchdog and unregisters the bot
# itself); the files go only once the session file is gone: the readiness
# file at once, the log after a day, the state directory after an hour.
#
# Runs on the HOST: session files never enter a sandbox.
set -uo pipefail

here="${0%/*}"
[[ "$here" != "$0" ]] || here=.
BIN="$(cd -P "$here" && pwd -P)"
LIB="$BIN/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"
# Commands only from PATH entries no sandbox can write, and from / (arc42 §7.3).
mm_safe_path
cd /
# No credential in any process environment (arc42 §7.3): the operator's token
# and secret stay in this shell; helpers get them on fd 3 (mm_with_token,
# mm_with_secret), curl on stdin.
export -n CB_MM_BOT_TOKEN MM_BOT_TOKEN MATTERMOST_TOKEN MM_REG_SECRET 2>/dev/null || true

APPLY=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,54p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
removed=0
kept=0
skipped=0
ACTIVE_NAMES=()
KEEP_SEC="$(mm_keep_sec)"

note() { echo "$*" >&2; }

# field FILE KEY [OLD_KEY]: the new key first, else the old one (#169).
field() { mm_kv "$@"; }

# A session is live when its supervising process still exists. Only `kill -0`, no
# start-time check: a recycled pid keeps a gone session's files (bot-id and NATS
# name alike) while the foreign process runs, which is the safe direction.
is_live() {
  local pid="$1"
  [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

# Per-bot state, not a session: the uid in its name, never a token.
STATE_RE='^(asks|coord|gate|last|listen|nonce|presence|turn|update)-[a-z0-9]{26}\.env$|^stats-[a-z0-9]{26}\.jsonl$|^(permission|crown)-[a-z0-9]{26}\.log$'
# The MCP server's hook API endpoint (RT-6.17, endpoint-[sid-]<key>.env): no
# session and no token. A server removes its own at exit; one left by a crash
# names a port nobody listens on, and the hooks stay quiet. Section 4 removes
# those whose server is gone.
# `.env.tmp` too: a signal between the temp file and its `os.replace`
# leaves one, and nothing else would ever remove it (review of #34).
ENDPOINT_RE='^endpoint-(sid-)?[0-9a-f]{16}\.env(\.tmp)?$'
is_state_file() { [[ "$(basename "$1")" =~ $STATE_RE ]]; }
# Per-agent state keyed by the NATS name (RT-6.1 step 7, RT-6.2 step 9): any kind, by
# pattern (natsbus.NAME_HASH_LEN characters of lower-case base32, no prefix since #274),
# so the kinds that move to the name later are covered without a list. A file holding
# a bot's token or registration name is a session, whatever its name.
# A counter move a crash interrupted leaves <kind>-<name>.jsonl.moving-<pid> (RT-6.2 step 9):
# the name's file too.
NAME_STATE_RE='^[a-z]+-[a-z2-7]{25}\.(env|jsonl|log|lock)(\.moving-[0-9]+)?$'
# Whether a process holds the flock on $1 (macOS has no flock(1): perl's flock, the same
# BSD lock the MCP server takes). Without perl a lock file counts as held: kept.
lock_held() {
  [[ -f "$1" && ! -L "$1" ]] || return 1
  command -v perl >/dev/null 2>&1 || return 0
  perl -e 'use Fcntl qw(:flock); open(my $f, "<", $ARGV[0]) or exit 1; exit(flock($f, LOCK_EX | LOCK_NB) ? 1 : 0)' "$1"
}
is_name_state_file() {
  [[ "$(basename "$1")" =~ $NAME_STATE_RE ]] || return 1
  ! grep -Eq '^(export )?(CB_MM_BOT_TOKEN|MM_BOT_TOKEN|MM_REG_BOT_NAME|MM_BOT_NAME)=' "$1" 2>/dev/null
}
not_a_session() {
  is_state_file "$1" || [[ "$(basename "$1")" =~ $ENDPOINT_RE ]] || is_name_state_file "$1"
}

# The registrar's username prefix (arc42 §7.1): this shell's own bot tells it,
# else the first session file that does, else the default "agent-".
PREFIX="$(mm_agent_prefix)"
if [[ -z "${MM_AGENT_PREFIX:-}" && -z "${MM_REG_BOT_NAME:-}" ]]; then
  shopt -s nullglob
  for f in "$STATE_DIR"/*.env; do
    not_a_session "$f" && continue
    [[ -n "$(field "$f" MM_AGENT_PREFIX)$(field "$f" MM_REG_BOT_NAME)" ]] || continue
    PREFIX="$(MM_AGENT_PREFIX="$(field "$f" MM_AGENT_PREFIX)" \
      CB_MM_BOT_USERNAME="$(field "$f" CB_MM_BOT_USERNAME MM_BOT_USERNAME)" \
      MM_REG_BOT_NAME="$(field "$f" MM_REG_BOT_NAME)" mm_agent_prefix)"
    break
  done
  shopt -u nullglob
fi

# --- 1. session files --------------------------------------------------------
# A corral start that died while taking a kept bot over left its claim: back
# under its name first, so it is seen below (RT-6.18 step 10).
[[ "$APPLY" -eq 0 ]] || mm_restore_claims "$STATE_DIR"
shopt -s nullglob
for f in "$STATE_DIR"/*.env; do
  not_a_session "$f" && continue
  name="$(field "$f" MM_REG_BOT_NAME)"
  # Files from before MM_REG_BOT_NAME carry only the username.
  [[ -n "$name" ]] || { name="$(field "$f" CB_MM_BOT_USERNAME MM_BOT_USERNAME)"; name="${name#"$PREFIX"}"; }
  [[ -n "$name" ]] || { note "skip $(basename "$f") (no bot name)"; continue; }

  pid="$(field "$f" CB_SESSION_PID MM_SESSION_PID)"
  hpid="$(field "$f" CB_BUS_HOST_PID MM_BUS_HOST_PID)"
  # A server without a session id that ended marked its file and kept the bot
  # for the workspace's next server (RT-6.10): its harness (CB_SESSION_PID)
  # may live on, so only the grace period below counts.
  # So does a corral session's bot that postEnd kept for `claude --continue`
  # (RT-6.18 step 9).
  ended=0
  ended_at="$(field "$f" CB_SERVER_ENDED MM_SERVER_ENDED)"
  if [[ -n "$ended_at" ]]; then
    pid="" hpid="" ended=1
  fi
  if [[ "$FORCE" -eq 0 ]] && ! is_live "$pid" && is_live "$hpid"; then
    note "HOSTED  ${name} (its MCP server on the host, pid ${hpid}, still runs and ends with its watchdog) — leaving it alone"
    ACTIVE_NAMES+=("$name")
    skipped=$((skipped + 1))
    continue
  fi
  if [[ "$FORCE" -eq 0 ]] && is_live "$pid"; then
    note "ACTIVE  ${name} (pid ${pid} alive) — leaving it alone"
    ACTIVE_NAMES+=("$name")
    skipped=$((skipped + 1))
    continue
  fi
  if [[ "$FORCE" -eq 0 && -z "$pid" && "$ended" -eq 0 ]]; then
    # Written before this guard existed: cannot prove it is dead.
    note "UNKNOWN ${name} (no CB_SESSION_PID) — skipping; use --force to include"
    ACTIVE_NAMES+=("$name")
    skipped=$((skipped + 1))
    continue
  fi
  # A kept bot (an MCP-registered one, RT-6.10, or a corral session's,
  # RT-6.18 step 9) waits out the grace period: counted from its end mark,
  # else from the file's mtime, which the MCP server's exit touches.
  if [[ "$FORCE" -eq 0 && ( "$ended" -eq 1 || "$(basename "$f")" == mcp-* ) ]]; then
    if [[ "$ended_at" =~ ^[0-9]+$ ]]; then
      age=$(( $(date +%s) - ended_at ))
    else
      age=$(( $(date +%s) - $(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f") ))
    fi
    if (( age < KEEP_SEC )); then
      note "KEPT    ${name} (ended $((age / 3600)) h ago, kept for a restart until $((KEEP_SEC / 3600)) h) — use --force to include"
      ACTIVE_NAMES+=("$name")
      skipped=$((skipped + 1))
      continue
    fi
  fi

  # A session started without a bot (DEFERRED=1, plan B piece 14a, RT-6.18 step 13) whose host
  # process registered none: no token, nothing to unregister. One that registered a bot wrote
  # its token here, and is unregistered with it below.
  if [[ "$(field "$f" DEFERRED)" == 1 && -z "$(field "$f" CB_MM_BOT_TOKEN MM_BOT_TOKEN)" ]]; then
    if [[ "$APPLY" -eq 0 ]]; then
      echo "would remove $(basename "$f")  (${name}: started without a bot, none registered)"
    else
      rm -f "$f"
      note "no bot  ${name} (started without one, none registered) — removed $(basename "$f")"
    fi
    continue
  fi
  if [[ "$APPLY" -eq 0 ]]; then
    echo "would unregister ${name}  (from $(basename "$f"))"
    continue
  fi
  # With the file's own token; the same as preStart's reaping of kept bots
  # (mm_unregister_session_file in mm-agent-lib.sh).
  rc=0
  err="$(mm_unregister_session_file "$f" "$name")" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    note "unregistered ${name}"
    removed=$((removed + 1))
  elif [[ "$rc" -eq 2 ]]; then
    # The bot's own token is rejected: the bot is gone already (its teardown
    # deleted it, or the server revoked the token). Only the file is left.
    note "gone    ${name} (its token is no longer accepted) — removed $(basename "$f")"
    removed=$((removed + 1))
  else
    note "FAILED ${name}: ${err} — keeping $(basename "$f")"
    kept=$((kept + 1))
  fi
done
shopt -u nullglob

# --- 2. orphans with no session file ----------------------------------------
if [[ -n "${MM_REG_SECRET:-}" && -n "${CB_MM_BOT_TOKEN:-}" ]]; then
  team="${CB_MM_TEAM_ID:-}"
  if [[ -z "$team" ]]; then
    mm_api GET "/api/v4/teams/name/$(mm_team)" &&
      [[ "$CB_MM_API_STATUS" == "200" ]] &&
      team="$(jq -r '.id' <<<"$CB_MM_API_BODY")"
  fi
  if [[ -n "$team" ]] && mm_api GET "/api/v4/users?in_team=${team}&per_page=200" &&
     [[ "$CB_MM_API_STATUS" == "200" ]]; then
    while read -r uname; do
      [[ -n "$uname" ]] || continue
      short="${uname#"$PREFIX"}"
      # Never touch a session we just classified as live, nor ourselves.
      skip=0
      for a in ${ACTIVE_NAMES+"${ACTIVE_NAMES[@]}"}; do
        [[ "$short" == "$a" ]] && skip=1 && break
      done
      [[ "$short" == "${MM_REG_BOT_NAME:-}" ]] && skip=1
      [[ "$uname" == "${CB_MM_BOT_USERNAME:-}" ]] && skip=1
      [[ "$skip" -eq 1 ]] && continue

      if [[ "$APPLY" -eq 0 ]]; then
        echo "would unregister ${uname}  (orphan, no session file)"
        continue
      fi
      if MM_AGENT_PREFIX="$PREFIX" mm_with_secret "$BIN/mm-agent-unregister.sh" "$short" >/dev/null 2>&1; then
        note "unregistered ${uname}"
        removed=$((removed + 1))
      else
        note "FAILED ${uname}"
        kept=$((kept + 1))
      fi
    done < <(jq -r --arg p "$PREFIX" '.[] | select(.username | startswith($p)) | select(.delete_at == 0) | .username' <<<"$CB_MM_API_BODY")
  fi
elif [[ "$APPLY" -eq 1 ]]; then
  note "(set MM_REG_SECRET to also sweep orphans that have no session file)"
fi

# --- 3. state files of bots without a session ---------------------------------
# Bots that still have a session file (live, or kept after a failed unregister)
# keep their state; so does this shell's own bot.
KEEP_UIDS=" ${CB_MM_BOT_USER_ID:-} "
shopt -s nullglob
for f in "$STATE_DIR"/*.env; do
  not_a_session "$f" || KEEP_UIDS+="$(field "$f" CB_MM_BOT_USER_ID MM_BOT_USER_ID) "
done
# So do the bots of MCP servers that still run (their endpoint file, RT-6.17),
# and the NATS names they key their state by (CB_STATE_KEY, section 3b).
KEEP_NAMES=" "
for f in "$STATE_DIR"/endpoint-*.env; do
  [[ "$(basename "$f")" =~ $ENDPOINT_RE ]] || continue
  is_live "$(field "$f" CB_BUS_PID MM_BUS_PID)" || continue
  KEEP_UIDS+="$(field "$f" CB_MM_BOT_USER_ID MM_BOT_USER_ID) "
  KEEP_NAMES+="$(field "$f" CB_STATE_KEY) "
done
stale=0
for f in "$STATE_DIR"/*-*.env "$STATE_DIR"/stats-*.jsonl "$STATE_DIR"/permission-*.log "$STATE_DIR"/crown-*.log; do
  is_state_file "$f" || continue
  uid="$(basename "$f")"; uid="${uid#*-}"; uid="${uid%%.*}"
  [[ "$KEEP_UIDS" == *" $uid "* ]] && continue
  [[ -n "$(find "$f" -mtime +0 2>/dev/null)" ]] || continue   # touched within a day
  stale=$((stale + 1))
  [[ "$APPLY" -eq 1 ]] && rm -f "$f"
done
shopt -u nullglob
if (( stale > 0 )); then
  if [[ "$APPLY" -eq 1 ]]; then
    note "removed ${stale} state file(s) of bots without a session"
  else
    echo "would remove ${stale} state file(s) of bots without a session"
  fi
fi

# --- 3b. state files keyed by a NATS name (RT-6.1 step 7, RT-6.2 step 9) ------
# A name is in use while a running MCP server keys its state by it (CB_STATE_KEY
# in its endpoint file), while its marker copied-<name>.env names a bot kept
# above, or while any of its files was touched within a day. Without a marker nothing ties
# the name to a session: its files stay, unless --force.
shopt -s nullglob
NAMES=" "
for f in "$STATE_DIR"/*-*.env "$STATE_DIR"/*-*.jsonl "$STATE_DIR"/*-*.log "$STATE_DIR"/*-*.lock \
         "$STATE_DIR"/*-*.jsonl.moving-*; do
  is_name_state_file "$f" || continue
  n="$(basename "$f")"; n="${n#*-}"; n="${n%%.*}"
  [[ "$NAMES" == *" $n "* ]] || NAMES+="$n "
done
named=0
untied=0
for n in $NAMES; do
  [[ "$KEEP_NAMES" == *" $n "* ]] && continue
  # A process that holds the name's lock (key-<name>.lock, RT-6.2 step 9) runs: its name
  # is in use, whatever its endpoint file says.
  lock_held "$STATE_DIR/key-$n.lock" && continue
  files=()
  for f in "$STATE_DIR"/*-"$n".env "$STATE_DIR"/*-"$n".jsonl "$STATE_DIR"/*-"$n".log "$STATE_DIR"/*-"$n".lock \
           "$STATE_DIR"/*-"$n".jsonl.moving-*; do
    is_name_state_file "$f" && files+=("$f")
  done
  (( ${#files[@]} )) || continue   # bash 3.2 with set -u: no empty "${files[@]}"
  marker="$STATE_DIR/copied-$n.env"
  if [[ -f "$marker" && ! -L "$marker" ]]; then
    tied=0
    taken=0
    while IFS='=' read -r k v; do
      [[ "$k" == CB_COPIED_FROM && "$v" =~ ^[a-z0-9]{26}$ && "$KEEP_UIDS" == *" $v "* ]] && tied=1
      [[ "$k" == CB_TAKEN_BY ]] && taken=1
    done <"$marker"
    # A name a later start of its session took over (CB_TAKEN_BY) is tied to no bot:
    # the day rule alone decides.
    [[ "$tied" -eq 1 && "$taken" -eq 0 ]] && continue
  elif [[ "$FORCE" -eq 0 ]]; then
    untied=$((untied + ${#files[@]}))
    continue
  fi
  fresh=0
  for f in "${files[@]}"; do
    [[ -n "$(find "$f" -mtime +0 2>/dev/null)" ]] || fresh=1   # touched within a day
  done
  [[ "$fresh" -eq 1 ]] && continue
  # A start may have written the marker since the check above.
  [[ -f "$marker" && -z "$(find "$marker" -mtime +0 2>/dev/null)" ]] && continue
  named=$((named + ${#files[@]}))
  [[ "$APPLY" -eq 1 ]] && rm -f "${files[@]}"
done
shopt -u nullglob
if (( named > 0 )); then
  if [[ "$APPLY" -eq 1 ]]; then
    note "removed ${named} state file(s) keyed by a NATS name no session uses"
  else
    echo "would remove ${named} state file(s) keyed by a NATS name no session uses"
  fi
fi
if (( untied > 0 )); then
  note "left ${untied} state file(s) keyed by a NATS name without copied-<name>.env (no session tied to them) — use --force to include those untouched for a day"
fi

# --- 4. endpoint files of servers that are gone ------------------------------
# A crashed MCP server leaves its endpoint file (RT-6.17). Removed once its
# CB_BUS_PID is no longer alive and the file is an hour old: a server starting
# right now may not have been seen alive yet.
gone=0
for f in "$STATE_DIR"/endpoint-*.env "$STATE_DIR"/endpoint-*.env.tmp; do
  [[ "$(basename "$f")" =~ $ENDPOINT_RE ]] || continue
  is_live "$(field "$f" CB_BUS_PID MM_BUS_PID)" && continue
  [[ -n "$(find "$f" -mmin +60 2>/dev/null)" ]] || continue
  gone=$((gone + 1))
  [[ "$APPLY" -eq 1 ]] && rm -f "$f"
done
if (( gone > 0 )); then
  if [[ "$APPLY" -eq 1 ]]; then
    note "removed ${gone} endpoint file(s) of MCP servers that are gone"
  else
    echo "would remove ${gone} endpoint file(s) of MCP servers that are gone"
  fi
fi

# --- 5. the host process's files of ended sessions (RT-6.18) -------------------
hostfiles=0
shopt -s nullglob
for f in "$STATE_DIR"/*.host.json "$STATE_DIR"/*.host.log; do
  [[ -f "${f%.host.*}.env" ]] && continue
  [[ "$f" == *.json || -n "$(find "$f" -mmin +1440 2>/dev/null)" ]] || continue
  hostfiles=$((hostfiles + 1))
  [[ "$APPLY" -eq 1 ]] && rm -f "$f"
done
for d in "$STATE_DIR"/hosted/*; do
  [[ -d "$d" && ! -L "$d" ]] || continue
  [[ -f "$STATE_DIR/$(basename "$d").env" ]] && continue
  # A start is taking the kept bot over (RT-6.18 step 10): the directory is its.
  compgen -G "$STATE_DIR/.$(basename "$d").env.taken-*" >/dev/null && continue
  [[ -n "$(find "$d" -maxdepth 0 -mmin +60 2>/dev/null)" ]] || continue
  hostfiles=$((hostfiles + 1))
  [[ "$APPLY" -eq 1 ]] && rm -rf "$d"
done
shopt -u nullglob
if (( hostfiles > 0 )); then
  if [[ "$APPLY" -eq 1 ]]; then
    note "removed ${hostfiles} file(s) of host processes whose session ended"
  else
    echo "would remove ${hostfiles} file(s) of host processes whose session ended"
  fi
fi

if [[ "$APPLY" -eq 1 ]]; then
  note "sweep done: ${removed} removed, ${kept} failed, ${skipped} left alone as active"
else
  note "dry run — re-run with --apply to remove"
fi
