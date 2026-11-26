# Environment variable names: the `CB_` prefix

## Purpose and status

Every environment variable this project defines gets the `CB_` prefix
(choirbus). A variable that exists only because of Mattermost gets `CB_MM_`.
The owner decided this on 2026-11-26 ([#169](https://github.com/dembaca/ai-agent-bus/issues/169)).

**Not built yet.** This page is the mapping and the rules. The code still reads
the old names. The renames come in one pull request per component and ship
together as release 0.103.0. Until then the old names in the
[README configuration table](../README.md#configuration), the
[arc42 docs](arc42) and the adapter docs stay right.

## The rules

### Which prefix

- An `MM_` variable that exists only because of Mattermost (URLs, tokens, the
  team, channel names, bots, the registrar that makes the bots, features only
  Mattermost has) becomes `CB_MM_<rest>`.
- Every other `MM_` variable becomes `CB_<rest>`: NATS, the Keycloak login,
  the host, the bus itself, release keys, timings.
- `AUTH_X` (the login service) becomes `CB_AUTH_X`, `AUTHD_X` (its command
  line tool `authd`) becomes `CB_AUTHD_X`, `BRIDGE_X` becomes `CB_BRIDGE_X`,
  `CONSOLE_X` becomes `CB_CONSOLE_X`. A Mattermost part inside keeps its `MM`:
  `BRIDGE_MM_URL` becomes `CB_BRIDGE_MM_URL`.
- Names of other products keep their names: `HOME`, `PATH`, `CLAUDE_*`,
  `CORRAL_*`, `OPENCODE_*` and the like (see [Excluded names](#excluded-names)).

### The reading rule

Each component reads its variables like this from 0.103.0 on:

1. It reads the new name first.
2. If the new name is unset, it reads the old name. If the old name is set,
   it logs one deprecation warning per process. The warning names the new
   name.
3. If both names are set to different values, the component refuses to
   start and names both.
4. The fallbacks go in the release after 0.103.0. From then on only the new
   names count.

A shell script that cannot log a warning (a hook that must stay quiet) writes
it to its log file instead. Values the bus sets for its own children (marked
*internal* in the tables) are written under the new name only. The reader
still accepts the old name, because a host process of the old version may
still start a new one during the update.

### Collision check

The tables below map 169 old names (each name counted once) to new names.
The check for this page found:

- No new name is in use in the repository today: there is no `CB_` name
  anywhere yet.
- No two old names map to one new name, with two deliberate exceptions.
  `MATTERMOST_URL` is today an alias of `MM_CHAT_URL`, and `MATTERMOST_TOKEN`
  an alias of `MM_BOT_TOKEN`. Each pair is one setting already, so each maps
  to one new name, and the alias is dropped with the fallbacks.
- No new `CB_X` equals a new `CB_MM_X`, `CB_AUTH_X`, `CB_BRIDGE_X` or
  `CB_CONSOLE_X` name.

## The tables

Columns: the old name, the new name, whether the variable is
Mattermost-specific, the files that read it, and a note. Paths without a
directory in the first two tables are in `mcp/src/mattermost_ai_agent_bus/`.
A name read by several components appears in each component's table, since
each component's pull request renames its own reads.

### The MCP server and the plugin (58)

| Old name | New name | Mattermost | Where it is read | Note |
|---|---|---|---|---|
| `MM_ACK_WAIT_SEC` | `CB_ACK_WAIT_SEC` | no | `askroute.py`, `lease.py`, `life.py` | |
| `MM_AGENT_KIND` | `CB_AGENT_KIND` | no | `attest.py`, `client.py`, `server.py`, `stats.py`; `bin/mm-agent-register.sh` | session or autonomous |
| `MM_AGENT_PREFIX` | `CB_MM_AGENT_PREFIX` | yes | `session_file.py`; `bin/mm-agent-lib.sh` | the bot username prefix the registrar hands out |
| `MM_ASK_REMIND_SEC` | `CB_ASK_REMIND_SEC` | no | `remind.py` | |
| `MM_ASK_ROUTE` | `CB_ASK_ROUTE` | no | `askroute.py` | |
| `MM_ASK_ROUTE_WAIT_MIN` | `CB_ASK_ROUTE_WAIT_MIN` | no | `askroute.py` | |
| `MM_BOT_ATTESTATION` | `CB_MM_BOT_ATTESTATION` | yes | `attest.py`; `bin/mm-agent-register.sh` | the registrar's signed record of the bot |
| `MM_BOT_TOKEN` | `CB_MM_BOT_TOKEN` | yes | `client.py`, `server.py`, `hosted.py`; `bin/mm-agent-lib.sh` | |
| `MATTERMOST_TOKEN` | `CB_MM_BOT_TOKEN` | yes | `client.py`, `server.py`, `hosted.py`; `bin/mm-agent-lib.sh` | alias of `MM_BOT_TOKEN` today; dropped with the fallbacks |
| `MM_BOT_USER_ID` | `CB_MM_BOT_USER_ID` | yes | `client.py`, `lease.py`, `nats_port.py`, `owners.py`, `stats.py`, `server.py`; `bin/mm-agent-lib.sh` | |
| `MM_BOT_USERNAME` | `CB_MM_BOT_USERNAME` | yes | `nats_port.py`, `session_file.py`; `bin/mm-agent-lib.sh` | |
| `MM_BRIDGE_BOT` | `CB_MM_BRIDGE_BOT` | yes | `compose.py` | the bridge's Mattermost bot account |
| `MM_BUS_ENDPOINT` | `CB_BUS_ENDPOINT` | no | `hosted.py`, `sandbox.py`; `bin/mm-agent-lib.sh` | internal: the hook API, set by the host hook |
| `MM_BUS_KEPT_SID` | `CB_BUS_KEPT_SID` | no | `hosted.py`; `adapters/corral/10-mm-session-start.sh` | internal |
| `MM_BUS_KEY` | `CB_BUS_KEY` | no | `hookapi.py`, `hosted.py`; `bin/mm-agent-lib.sh` | internal |
| `MM_BUS_MCP_ENDPOINT` | `CB_BUS_MCP_ENDPOINT` | no | `hosted.py`, `sandbox.py`; `bin/mm-agent-lib.sh` | internal |
| `MM_BUS_UNAVAILABLE` | `CB_BUS_UNAVAILABLE` | no | `server.py`, `hosted.py`; `adapters/corral/mm-hook-dispatch.sh` | internal |
| `MM_CHANNEL` | `CB_MM_CHANNEL` | yes | `client.py`; `bin/mm-agent-channels.sh` | the lobby's channel name |
| `MM_CHANNEL_ID` | `CB_MM_CHANNEL_ID` | yes | `client.py`, `homes.py`; `bin/mm-agent-channels.sh` | |
| `MM_CHANNEL_PUSH` | `CB_CHANNEL_PUSH` | no | `channel.py`, `hosted.py` | Claude Code's channel mode, not a Mattermost channel |
| `MM_CHAT_URL` | `CB_MM_CHAT_URL` | yes | `client.py`, `selfupdate.py`, `release.py`; `bin/mm-agent-lib.sh` | |
| `MATTERMOST_URL` | `CB_MM_CHAT_URL` | yes | `client.py`, `selfupdate.py`; `bin/mm-agent-lib.sh` | alias of `MM_CHAT_URL` today; dropped with the fallbacks |
| `MM_CROWN_BEAT_SEC` | `CB_CROWN_BEAT_SEC` | no | `lease.py` | |
| `MM_CROWN_LEASE_SEC` | `CB_CROWN_LEASE_SEC` | no | `lease.py`, `life.py` | |
| `MM_HEARTBEAT_SEC` | `CB_HEARTBEAT_SEC` | no | `lease.py` | |
| `MM_LEASE_SEC` | `CB_LEASE_SEC` | no | `life.py`, `departed.py`; `bin/mm-agent-status.sh` | |
| `MM_LISTEN_COORD_SEC` | `CB_LISTEN_COORD_SEC` | no | `turnend.py`; `hooks/stop-inbox-check.sh` | |
| `MM_LISTEN_DIGEST_SEC` | `CB_LISTEN_DIGEST_SEC` | no | `channel.py`, `server.py`; `bin/mm-agent-listen.sh` | |
| `MM_LISTEN_SEC` | `CB_LISTEN_SEC` | no | `turnend.py`; `hooks/stop-inbox-check.sh` | |
| `MM_LOGIN` | `CB_LOGIN` | no | `login.py`, `compose.py`, `hosted.py` | the Keycloak login |
| `MM_LOGIN_CLIENT_ID` | `CB_LOGIN_CLIENT_ID` | no | `login.py`, `compose.py` | |
| `MM_LOGIN_ISSUER` | `CB_LOGIN_ISSUER` | no | `login.py`, `compose.py` | |
| `MM_LOGIN_NKEY` | `CB_LOGIN_NKEY` | no | `login.py` | |
| `MM_NATS_CA_FILE` | `CB_NATS_CA_FILE` | no | `compose.py`, `natsbus.py` | |
| `MM_NATS_HOMES` | `CB_NATS_HOMES` | no | `compose.py`, `hosted.py`, `lease.py` | |
| `MM_NATS_MEMORY` | `CB_NATS_MEMORY` | no | `compose.py` | |
| `MM_NATS_URL` | `CB_NATS_URL` | no | `compose.py` | |
| `MM_OWNER` | `CB_OWNER` | no | `owners.py`, `client.py`, `attest.py`, `permission.py`; `bin/mm-agent-lib.sh` | human names; the NATS side reads them too |
| `MM_OWNER_USER_ID` | `CB_MM_OWNER_USER_ID` | yes | `owners.py`, `client.py`, `permission.py` | a Mattermost user id |
| `MM_PROJECT_CHANNEL` | `CB_MM_PROJECT_CHANNEL` | yes | `client.py`, `config.py`; `bin/mm-agent-channels.sh` | |
| `MM_PROJECT_CHANNEL_ID` | `CB_MM_PROJECT_CHANNEL_ID` | yes | `client.py`, `hookapi.py`; `bin/mm-agent-channels.sh` | |
| `MM_PROPS_PREFIX` | `CB_PROPS_PREFIX` | no | `a2a.py`, `compose.py`, `hosted.py` | one setting of the whole bus: the NATS messages carry the same keys |
| `MM_REG_BOT_NAME` | `CB_MM_REG_BOT_NAME` | yes | `client.py`, `server.py`, `session_file.py`; `bin/mm-agent-lib.sh` | |
| `MM_REG_SECRET` | `CB_MM_REG_SECRET` | yes | `client.py`, `hosted.py`; `bin/mm-agent-register.sh` | the registrar exists to make Mattermost bots |
| `MM_REGISTER_URL` | `CB_MM_REGISTER_URL` | yes | `client.py`, `selfupdate.py`, `release.py`; `bin/mm-agent-lib.sh` | |
| `MM_REGISTRAR_KEYS` | `CB_MM_REGISTRAR_KEYS` | yes | `attest.py`, `release.py`; `bin/mm-agent-lib.sh`, `bin/mm-agent-host-install.sh` | also checks the registrar's release record |
| `MM_REMIND_MAX_SEC` | `CB_REMIND_MAX_SEC` | no | `remind.py`, `stall.py` | |
| `MM_RESPONSIBLE_HUMAN` | `CB_RESPONSIBLE_HUMAN` | no | `owners.py`, `attest.py`, `server.py`; `bin/mm-agent-lib.sh` | |
| `MM_SESSION_KEEP_SEC` | `CB_SESSION_KEEP_SEC` | no | `session_file.py`; `bin/mm-agent-sweep.sh` | |
| `MM_STALL_SEC` | `CB_STALL_SEC` | no | `stall.py` | |
| `MM_STATS_CHANNEL` | `CB_MM_STATS_CHANNEL` | yes | `stats.py`; `bin/mm-agent-stats.sh` | the Mattermost channel; over NATS the login service's stats prefix counts |
| `MM_TEAM` | `CB_MM_TEAM` | yes | `client.py`; `bin/mm-agent-channels.sh` | |
| `MM_TRUSTED_HUMAN_USER_IDS` | `CB_MM_TRUSTED_HUMAN_USER_IDS` | yes | `owners.py`, `client.py`, `permission.py` | Mattermost user ids |
| `MM_TRUSTED_HUMANS` | `CB_TRUSTED_HUMANS` | no | `owners.py`, `client.py`, `attest.py`, `compose.py`; `bin/mm-agent-lib.sh` | human names; the NATS side reads them too |
| `MM_TYPING_SEC` | `CB_MM_TYPING_SEC` | yes | `server.py` | Mattermost's typing indicator |
| `MM_WORKSPACE` | `CB_WORKSPACE` | no | `hosted.py`, `hookapi.py`, `session_file.py` | |
| `MM_RELEASE_SIGNERS` | `CB_RELEASE_SIGNERS` | no | `release.py` (handed to the host script) | also in the hooks table |
| `MM_RELEASE_URL` | `CB_RELEASE_URL` | no | `release.py` (handed to the host script) | also in the hooks table |

### Hooks and scripts (16)

These live in `bin/`, `hooks/` and `adapters/corral/`. The scripts also read
most names of the table above; those are renamed there.

| Old name | New name | Mattermost | Where it is read | Note |
|---|---|---|---|---|
| `MM_AGENT_SESSION_FILE` | `CB_AGENT_SESSION_FILE` | no | `bin/mm-agent-lib.sh`, `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh`; `hosted.py` | |
| `MM_BOT_TOKEN_FD` | `CB_MM_BOT_TOKEN_FD` | yes | `bin/mm-agent-lib.sh`, `bin/mm-agent-status.sh` | internal: hands the token to a helper on a file descriptor |
| `MM_BUS_DEADLINE` | `CB_BUS_DEADLINE` | no | `bin/mm-agent-lib.sh`; set by `hooks/stop-inbox-check.sh`, `hooks/after-compact.sh` | internal |
| `MM_BUS_HOOK_DISPATCH` | `CB_BUS_HOOK_DISPATCH` | no | `adapters/corral/10-mm-session-start.sh`, `adapters/corral/mm-hook-dispatch.sh` | |
| `MM_BUS_HOST_DIR` | `CB_BUS_HOST_DIR` | no | `bin/mm-agent-host-install.sh`, `adapters/corral/*.sh`; `release.py` | |
| `MM_BUS_MAX_TIME` | `CB_BUS_MAX_TIME` | no | `bin/mm-agent-lib.sh` | |
| `MM_BUS_UPLOAD` | `CB_BUS_UPLOAD` | no | `bin/mm-agent-lib.sh`; set by `bin/mm-agent-post.sh` | internal |
| `MM_LISTEN_POLL_SEC` | `CB_LISTEN_POLL_SEC` | no | `bin/mm-agent-listen.sh` | |
| `MM_REG_SECRET_FD` | `CB_MM_REG_SECRET_FD` | yes | `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh` | internal: like the token's descriptor |
| `MM_REG_SECRET_GOPASS` | `CB_MM_REG_SECRET_GOPASS` | yes | `adapters/corral/10-mm-session-start.sh`; `hosted.py` | |
| `MM_REGISTER` | `CB_MM_REGISTER` | yes | `bin/mm-agent-lib.sh` | `always` registers a bot in every session |
| `MM_RELEASE_SIGNERS` | `CB_RELEASE_SIGNERS` | no | `bin/mm-agent-lib.sh`, `bin/mm-agent-host-install.sh`, `adapters/corral/90-mm-session-stop.sh` | |
| `MM_RELEASE_URL` | `CB_RELEASE_URL` | no | `bin/mm-agent-lib.sh`, `bin/mm-agent-host-install.sh`, `adapters/corral/90-mm-session-stop.sh` | |
| `MM_TEAM_ID` | `CB_MM_TEAM_ID` | yes | `bin/mm-agent-channels.sh`, `bin/mm-agent-lib.sh` | |
| `MM_REL_GIT_DIR` | `CB_REL_GIT_DIR` | no | `bin/mm-agent-lib.sh`; exported by `bin/mm-agent-release-check.sh` | internal |
| `MM_REL_TMP` | `CB_REL_TMP` | no | `bin/mm-agent-lib.sh`; exported by `bin/mm-agent-release-check.sh` | internal |

#### Keys in the session files (9)

These are keys in the files the bus writes into the host's state directory
(`KEY=value` lines), not variables a person sets. A kept session file lives
up to a day, so the reader takes both keys for one release, like the
fallbacks.

| Old key | New key | Mattermost | Where it is read | Note |
|---|---|---|---|---|
| `MM_BUS_CLIENT` | `CB_BUS_CLIENT` | no | `hookapi.py` | |
| `MM_BUS_HOST_PID` | `CB_BUS_HOST_PID` | no | `adapters/corral/10-mm-session-start.sh` | |
| `MM_BUS_HOST_START` | `CB_BUS_HOST_START` | no | `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh` | |
| `MM_BUS_PID` | `CB_BUS_PID` | no | `hookapi.py`; `bin/mm-agent-sweep.sh` | |
| `MM_CLAUDE_SID` | `CB_CLAUDE_SID` | no | `adapters/corral/90-mm-session-stop.sh` | in `claude-sid.env` |
| `MM_KEPT_CLAUDE_SID` | `CB_KEPT_CLAUDE_SID` | no | `adapters/corral/10-mm-session-start.sh`; `bin/mm-agent-lib.sh` | |
| `MM_KEPT_WORKDIR` | `CB_KEPT_WORKDIR` | no | `bin/mm-agent-lib.sh` | |
| `MM_SERVER_ENDED` | `CB_SERVER_ENDED` | no | `session_file.py`; `bin/mm-agent-lib.sh`, `bin/mm-agent-sweep.sh` | |
| `MM_SESSION_PID` | `CB_SESSION_PID` | no | `client.py`; `bin/mm-agent-sweep.sh`, `bin/mm-agent-host-install.sh` | |

The session files also carry variables from the first table (`MM_BOT_TOKEN`,
`MM_REG_BOT_NAME`, `MM_BUS_KEY` and others). Those keys follow the same
mapping.

#### Shell variables that are not environment

These names look like settings but are globals inside the scripts: function
results, file names, the release check's state and the emoji constants of
`bin/mm-agent-symbols.sh`. Nobody sets them from outside. The hooks pull
request renames them by the same rule, without a fallback:

- to `CB_MM_…`: `MM_API_BODY`, `MM_API_STATUS`, `MM_UNREG_HTTP`,
  `MM_UNREG_TOKEN_HTTP`, `MM_UNREG_WHY`;
- to `CB_…`: `MM_ANSWER`, `MM_AUTO_FILE`, `MM_AUTO_LOG`, `MM_BUS_BODY`,
  `MM_BUS_CHANNEL`, `MM_BUS_FROM`, `MM_BUS_PROJECT`, `MM_BUS_STATUS`,
  `MM_CREDS_FROM_FD`, `MM_CROWN`, `MM_CROWN_UNTIL`, `MM_HOLD_FILE`,
  `MM_HOOK_INPUT`, `MM_INBOX`, `MM_LISTENING`, `MM_NEEDS_RE`,
  `MM_REL_ALLOWED`, `MM_REL_COMMIT`, `MM_REL_ENFORCE`, `MM_REL_ERR`,
  `MM_REL_FROM`, `MM_REL_FROM_TEXT`, `MM_REL_KEYS`, `MM_REL_PARSED`,
  `MM_REL_PINNED`, `MM_REL_REPLACE`, `MM_REL_SIGNER`, `MM_REL_URL`,
  `MM_RR_KEYS`, `MM_RR_KID`, `MM_RR_STATUS`, `MM_RR_VERSION`, `MM_RR_WHY`,
  `MM_SIGNERS_FILE`, `MM_SIGNERS_MAX`, `MM_SIGNOFF`, `MM_UPDATE`,
  `MM_WITHDRAWN_FILE`.

### The login service and authd (44)

All in `services/auth/`. Its binary `authd` runs the service; its directory
commands read the two `AUTHD_` names and two of the service's names (noted).

| Old name | New name | Mattermost | Where it is read | Note |
|---|---|---|---|---|
| `AUTH_ACCOUNT` | `CB_AUTH_ACCOUNT` | no | `internal/app/callout.go` | |
| `AUTH_ADMIN_LISTEN_ADDR` | `CB_AUTH_ADMIN_LISTEN_ADDR` | no | `internal/app/callout.go` | |
| `AUTH_ADMIN_TLS_CERT_FILE` | `CB_AUTH_ADMIN_TLS_CERT_FILE` | no | `internal/app/callout.go` | |
| `AUTH_ADMIN_TLS_KEY_FILE` | `CB_AUTH_ADMIN_TLS_KEY_FILE` | no | `internal/app/callout.go` | |
| `AUTH_APPROVER_CLAIM` | `CB_AUTH_APPROVER_CLAIM` | no | `internal/app/callout.go` | |
| `AUTH_APPROVER_GROUP` | `CB_AUTH_APPROVER_GROUP` | no | `internal/app/callout.go` | |
| `AUTH_BRAKE_FILE` | `CB_AUTH_BRAKE_FILE` | no | `internal/app/callout.go` | |
| `AUTH_CONSOLE_CLIENT_ID` | `CB_AUTH_CONSOLE_CLIENT_ID` | no | `internal/app/callout.go` | |
| `AUTH_DIRECTORY_ADMIN_GROUP` | `CB_AUTH_DIRECTORY_ADMIN_GROUP` | no | `internal/app/callout.go` | |
| `AUTH_DIRECTORY_BUCKET` | `CB_AUTH_DIRECTORY_BUCKET` | no | `internal/app/callout.go` | |
| `AUTH_DIRECTORY_IMPORTED` | `CB_AUTH_DIRECTORY_IMPORTED` | no | `internal/app/callout.go` | guards the directory on NATS after the import |
| `AUTH_ISSUER_SEED_FILE` | `CB_AUTH_ISSUER_SEED_FILE` | no | `internal/app/callout.go` | |
| `AUTH_JS_SEED_FILE` | `CB_AUTH_JS_SEED_FILE` | no | `internal/app/callout.go` | |
| `AUTH_JS_URL` | `CB_AUTH_JS_URL` | no | `internal/app/callout.go` | |
| `AUTH_KEYCLOAK_AUDIENCE` | `CB_AUTH_KEYCLOAK_AUDIENCE` | no | `internal/app/callout.go` | |
| `AUTH_KEYCLOAK_CLIENT_ID` | `CB_AUTH_KEYCLOAK_CLIENT_ID` | no | `internal/app/callout.go`, `internal/dircli/dircli.go` | also `authd` |
| `AUTH_KEYCLOAK_ISSUER` | `CB_AUTH_KEYCLOAK_ISSUER` | no | `internal/app/callout.go`, `internal/dircli/dircli.go` | also `authd` |
| `AUTH_KEYCLOAK_JWKS_URL` | `CB_AUTH_KEYCLOAK_JWKS_URL` | no | `internal/app/callout.go` | |
| `AUTH_LISTEN_ADDR` | `CB_AUTH_LISTEN_ADDR` | no | `internal/config/config.go` | |
| `AUTH_LOBBY` | `CB_AUTH_LOBBY` | no | `internal/app/callout.go` | the lobby on NATS |
| `AUTH_LOG_LEVEL` | `CB_AUTH_LOG_LEVEL` | no | `internal/config/config.go` | |
| `AUTH_MATTERMOST_TEAM` | `CB_AUTH_MM_TEAM` | yes | `internal/app/callout.go` | `MATTERMOST` shortened to `MM`, as in the bridge |
| `AUTH_MATTERMOST_TOKEN_FILE` | `CB_AUTH_MM_TOKEN_FILE` | yes | `internal/app/callout.go` | as above |
| `AUTH_MATTERMOST_URL` | `CB_AUTH_MM_URL` | yes | `internal/app/callout.go` | as above |
| `AUTH_NAME_BINDING` | `CB_AUTH_NAME_BINDING` | no | `internal/app/callout.go` | decides about Mattermost password accounts at the import, but binds names on NATS |
| `AUTH_NAME_PREFIX` | `CB_AUTH_NAME_PREFIX` | no | `internal/app/callout.go` | |
| `AUTH_NATS_CA_FILE` | `CB_AUTH_NATS_CA_FILE` | no | `internal/app/callout.go` | |
| `AUTH_NATS_CERT_FILE` | `CB_AUTH_NATS_CERT_FILE` | no | `internal/app/callout.go` | |
| `AUTH_NATS_KEY_FILE` | `CB_AUTH_NATS_KEY_FILE` | no | `internal/app/callout.go` | |
| `AUTH_NATS_SEED_FILE` | `CB_AUTH_NATS_SEED_FILE` | no | `internal/app/callout.go` | |
| `AUTH_NATS_URL` | `CB_AUTH_NATS_URL` | no | `internal/app/callout.go`, `cmd/authd/main.go` | |
| `AUTH_OPERATOR_GROUP` | `CB_AUTH_OPERATOR_GROUP` | no | `internal/app/callout.go` | |
| `AUTH_PERSON_MAX_LOGINS` | `CB_AUTH_PERSON_MAX_LOGINS` | no | `internal/app/callout.go` | |
| `AUTH_PERSON_PREFIX` | `CB_AUTH_PERSON_PREFIX` | no | `internal/app/callout.go` | |
| `AUTH_PLAIN_REQUESTS` | `CB_AUTH_PLAIN_REQUESTS` | no | `internal/app/callout.go` | |
| `AUTH_PRESENCE_PREFIX` | `CB_AUTH_PRESENCE_PREFIX` | no | `internal/app/callout.go` | |
| `AUTH_REQUEST_TIMEOUT` | `CB_AUTH_REQUEST_TIMEOUT` | no | `internal/app/callout.go` | |
| `AUTH_SHUTDOWN_TIMEOUT` | `CB_AUTH_SHUTDOWN_TIMEOUT` | no | `internal/config/config.go` | |
| `AUTH_STATS_PREFIX` | `CB_AUTH_STATS_PREFIX` | no | `internal/app/callout.go` | |
| `AUTH_SUBJECT_PREFIX` | `CB_AUTH_SUBJECT_PREFIX` | no | `internal/app/callout.go` | |
| `AUTH_TOKEN_LEEWAY` | `CB_AUTH_TOKEN_LEEWAY` | no | `internal/app/callout.go` | |
| `AUTH_XKEY_SEED_FILE` | `CB_AUTH_XKEY_SEED_FILE` | no | `internal/app/callout.go` | |
| `AUTHD_DIR_API` | `CB_AUTHD_DIR_API` | no | `internal/dircli/dircli.go` | `authd` only |
| `AUTHD_DIR_CA_FILE` | `CB_AUTHD_DIR_CA_FILE` | no | `internal/dircli/dircli.go` | `authd` only |

`services/auth/nats-server.example.conf` names several of them in comments;
it changes with the service.

### The bridge (19)

All in `services/bridge/`, read in `internal/config/config.go`. The last three
come with the bridge's agent bots
([#166](https://github.com/dembaca/ai-agent-bus/pull/166), not merged when
this was written). The bridge as a whole exists for Mattermost; the column
says whether the setting itself is about Mattermost.

| Old name | New name | Mattermost | Where it is read | Note |
|---|---|---|---|---|
| `BRIDGE_BUS_PREFIX` | `CB_BRIDGE_BUS_PREFIX` | no | `internal/config/config.go` | |
| `BRIDGE_CA_FILE` | `CB_BRIDGE_CA_FILE` | no | `internal/config/config.go`, `cmd/bridged/main.go` | |
| `BRIDGE_DELIVER` | `CB_BRIDGE_DELIVER` | no | `internal/config/config.go` | |
| `BRIDGE_HOMES` | `CB_BRIDGE_HOMES` | no | `internal/config/config.go` | the homes' names on both sides |
| `BRIDGE_INBOX` | `CB_BRIDGE_INBOX` | no | `internal/config/config.go` | |
| `BRIDGE_KV_BUCKET` | `CB_BRIDGE_KV_BUCKET` | no | `internal/config/config.go` | |
| `BRIDGE_LISTEN_ADDR` | `CB_BRIDGE_LISTEN_ADDR` | no | `internal/config/config.go` | |
| `BRIDGE_MM_ALLOW_HTTP` | `CB_BRIDGE_MM_ALLOW_HTTP` | yes | `internal/config/config.go`, `cmd/bridged/main.go` | |
| `BRIDGE_MM_TEAM` | `CB_BRIDGE_MM_TEAM` | yes | `internal/config/config.go` | |
| `BRIDGE_MM_TOKEN_FILE` | `CB_BRIDGE_MM_TOKEN_FILE` | yes | `internal/config/config.go` | |
| `BRIDGE_MM_URL` | `CB_BRIDGE_MM_URL` | yes | `internal/config/config.go`, `cmd/bridged/main.go` | |
| `BRIDGE_NATS_SEED_FILE` | `CB_BRIDGE_NATS_SEED_FILE` | no | `internal/config/config.go` | |
| `BRIDGE_NATS_URL` | `CB_BRIDGE_NATS_URL` | no | `internal/config/config.go` | |
| `BRIDGE_PROPS_PREFIX` | `CB_BRIDGE_PROPS_PREFIX` | no | `internal/config/config.go` | must equal the homes' `CB_PROPS_PREFIX` |
| `BRIDGE_SEEN_BUCKET` | `CB_BRIDGE_SEEN_BUCKET` | no | `internal/config/config.go` | |
| `BRIDGE_STREAM` | `CB_BRIDGE_STREAM` | no | `internal/config/config.go` | |
| `BRIDGE_MM_ADMIN_TOKEN_FILE` | `CB_BRIDGE_MM_ADMIN_TOKEN_FILE` | yes | `internal/config/config.go` | new with #166 |
| `BRIDGE_BOT_PREFIX` | `CB_BRIDGE_MM_BOT_PREFIX` | yes | `internal/config/config.go` | new with #166; the Mattermost bots' username prefix |
| `BRIDGE_HIDE_BOT_DM` | `CB_BRIDGE_MM_HIDE_BOT_DM` | yes | `internal/config/config.go` | new with #166; a Mattermost direct message |

`BRIDGE_BOT_PREFIX` and `BRIDGE_HIDE_BOT_DM` (#166) are about Mattermost but
have no `MM` in their names; the rename gives them one, as the rule says:
`CB_BRIDGE_MM_BOT_PREFIX` and `CB_BRIDGE_MM_HIDE_BOT_DM`.

### The console (17)

| Old name | New name | Mattermost | Where it is read | Note |
|---|---|---|---|---|
| `CONSOLE_ADDR` | `CB_CONSOLE_ADDR` | no | `console/server/src/console_server/config.py` | |
| `CONSOLE_CLIENT_ID` | `CB_CONSOLE_CLIENT_ID` | no | same | |
| `CONSOLE_ISSUER` | `CB_CONSOLE_ISSUER` | no | same | |
| `CONSOLE_LOGIN_OFF` | `CB_CONSOLE_LOGIN_OFF` | no | same | |
| `CONSOLE_NATS_CA_FILE` | `CB_CONSOLE_NATS_CA_FILE` | no | same | |
| `CONSOLE_NATS_SEED_FILE` | `CB_CONSOLE_NATS_SEED_FILE` | no | same | |
| `CONSOLE_NATS_URL` | `CB_CONSOLE_NATS_URL` | no | same | |
| `CONSOLE_OPERATOR_GROUP` | `CB_CONSOLE_OPERATOR_GROUP` | no | same | |
| `CONSOLE_PRESENCE_STREAM` | `CB_CONSOLE_PRESENCE_STREAM` | no | same | |
| `CONSOLE_PRESENCE_SUBJECT` | `CB_CONSOLE_PRESENCE_SUBJECT` | no | same | |
| `CONSOLE_PUBLIC_URL` | `CB_CONSOLE_PUBLIC_URL` | no | same | |
| `CONSOLE_SESSION_SEC` | `CB_CONSOLE_SESSION_SEC` | no | same | |
| `CONSOLE_STATS_STREAM` | `CB_CONSOLE_STATS_STREAM` | no | same | |
| `CONSOLE_STATS_SUBJECT` | `CB_CONSOLE_STATS_SUBJECT` | no | same | |
| `CONSOLE_TOKEN_ALGS` | `CB_CONSOLE_TOKEN_ALGS` | no | same | |
| `CONSOLE_WEB_DIR` | `CB_CONSOLE_WEB_DIR` | no | same, `app.py`; `console/server/Dockerfile` | |
| `VITE_CONSOLE_API` | `VITE_CB_CONSOLE_API` | no | `console/web/src/main.tsx`; `console/server/Dockerfile` | build time only; Vite passes only `VITE_` names to the page, so `VITE_` stays in front; no fallback needed, the build sets it |

### The OpenCode plugin (7)

Read in `opencode/plugin/mattermost-agent-bus.ts`, or handed by it to the MCP
server it starts.

| Old name | New name | Mattermost | Where it is read | Note |
|---|---|---|---|---|
| `MM_AGENT_BUS_ROOT` | `CB_AGENT_BUS_ROOT` | no | `opencode/plugin/mattermost-agent-bus.ts` | the bus checkout; also in the Claude Code and Cursor adapter docs |
| `MM_BUS_ENDPOINT` | `CB_BUS_ENDPOINT` | no | same | internal |
| `MM_BUS_KEY` | `CB_BUS_KEY` | no | same | internal |
| `MM_BUS_MCP_ENDPOINT` | `CB_BUS_MCP_ENDPOINT` | no | same | internal |
| `MM_CHANNEL_PUSH` | `CB_CHANNEL_PUSH` | no | same | handed to the server |
| `MM_REG_SECRET` | `CB_MM_REG_SECRET` | yes | same | named in its comments and docs |
| `MM_WORKSPACE` | `CB_WORKSPACE` | no | same | handed to the server |

### The pi extension (9)

Read in `pi/mattermost-agent-bus.ts`, or handed by it to the MCP server it
starts.

| Old name | New name | Mattermost | Where it is read | Note |
|---|---|---|---|---|
| `MM_AGENT_BUS_ROOT` | `CB_AGENT_BUS_ROOT` | no | `pi/mattermost-agent-bus.ts` | |
| `MM_BUS_ENDPOINT` | `CB_BUS_ENDPOINT` | no | same | internal |
| `MM_BUS_KEY` | `CB_BUS_KEY` | no | same | internal |
| `MM_BUS_MCP_ENDPOINT` | `CB_BUS_MCP_ENDPOINT` | no | same | internal |
| `MM_CHANNEL_PUSH` | `CB_CHANNEL_PUSH` | no | same | handed to the server |
| `MM_REG_SECRET` | `CB_MM_REG_SECRET` | yes | same | |
| `MM_REG_SECRET_CMD` | `CB_MM_REG_SECRET_CMD` | yes | same | pi only |
| `MM_REG_SECRET_GOPASS` | `CB_MM_REG_SECRET_GOPASS` | yes | same | |
| `MM_WORKSPACE` | `CB_WORKSPACE` | no | same | handed to the server |

### Tests and CI (6)

The test suites read these; the workflows in `.github/workflows/` set them.
They need no fallback: the tests and the workflows change in one pull request.

| Old name | New name | Mattermost | Where it is read | Note |
|---|---|---|---|---|
| `MM_NATS_SERVER` | `CB_NATS_SERVER` | no | `mcp/tests/conftest.py`, `mcp/tests/test_natsbus.py`, `mcp/tests/test_nats_port.py`, `console/server/tests/test_nats_live.py` | set in `checks.yml` |
| `MM_TEST_NATS` | `CB_TEST_NATS` | no | the same tests | set in `checks.yml` |
| `MM_TEST_KEYCLOAK` | `CB_TEST_KEYCLOAK` | no | `mcp/tests/test_login_keycloak.py` | set in `keycloak.yml` |
| `MM_TEST_FD_TOKEN` | `CB_MM_TEST_FD_TOKEN` | yes | `tests/hooks/conftest.py` | a test's own hand-off of the bot token |
| `BUS_TESTRIG_PYTHON` | `CB_TESTRIG_PYTHON` | no | `testrig/src/bus_testrig/bus.py` | |
| `BUS_TESTRIG_REQUIRE_BUS` | `CB_TESTRIG_REQUIRE_BUS` | no | `testrig/tests/conftest.py` | set in `testrig.yml` |

### Excluded names

Not renamed, because they are not our environment variables:

- **Other products' variables** we read: `HOME`, `PATH`, `TMPDIR`, `TZ`,
  `XDG_STATE_HOME`, `DISPLAY`, `WAYLAND_DISPLAY`, `SSL_CERT_FILE`,
  `SSL_CERT_DIR`, `HTTPS_PROXY`, `NO_PROXY`, `CLAUDECODE`,
  `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PROJECT_DIR`,
  `CLAUDE_SESSION_ID`, `CORRAL_AGENT`, `CORRAL_AGENT_EXIT`, `CORRAL_SANDBOX`,
  `CORRAL_SESSION_ID`, `CORRAL_WORKDIR`, `OPENCODE_SESSION_ID`,
  `GITHUB_ACTIONS`, `GITHUB_ENV`.
- **Constants and names of things, not variables:** `BUS_BRIDGE`,
  `BUS_BRIDGE_SEEN`, `BUS_DIRECTORY` (NATS bucket names, which also contain
  `BRIDGE_SEEN`), `BRIDGE_KEYS` (a Python constant `_BRIDGE_KEYS`),
  `BRIDGE_TTL_SEC` (a constant in `nats_port.py`), `BRIDGE_USER_PUBLIC_NKEY`
  (a placeholder in `nats-server.example.conf`), `CONSOLE_FAMILIES` and
  `CONSOLE_NATS` (test constants), and the other `AGENT_*`, `BUS_*`,
  `NATS_*`, `STATS_*` constants in the code.
- **Value of a setting:** the props prefix itself (`x_`), the default of
  `MM_PROPS_PREFIX`. Changing the default is not part of this rename.
- **Name stems in patterns and comments:** `MM_API`, `MM_BUS_HOST`,
  `MM_KEPT`, `MM_REL`, `MM_RR` (each the start of names listed above).
- **Test placeholders:** `MM_A`, `MM_B`, `MM_BAR`, `MM_FOO`, `MM_X`,
  `MM_OLD`, `MM_OTHER`, `MM_ID`.
- **`MM_BOT_NAME`:** only in `tests/hooks/test_sweep.py`, as the key of an
  old session file format; no code reads it.
- **Script locals** in upper case (`STATE_DIR`, `ROOT`, `LISTEN_UNTIL` and
  so on): set and read inside one script.

## The `Needs:` rule

A CHANGELOG entry may say what its release needs first, in a `Needs:` line.
Every agent's update notice shows it, so its grammar is narrow. Today it
allows only `MM_*` names, versions and short lowercase words. The grammar
lives in four places:

- `CLAUDE.md`, section "Releasing";
- `NEEDS_PATTERN` in `mcp/src/mattermost_ai_agent_bus/release.py`;
- `MM_NEEDS_RE` in `bin/mm-agent-lib.sh`, the same text;
- `scripts/doc-check.py`, which reads the pattern from `release.py`.

The change: the grammar allows `CB_*` names, and `MM_*` names too while the
fallbacks exist. When the fallbacks go, `MM_*` goes from the grammar.

A server or host script before 0.103.0 checks the line with the old grammar
and skips a line it does not accept. So the `Needs:` line of 0.103.0 itself
names the old `MM_*` names, which still work through the fallbacks; it must
not name `CB_*` names.

This pull request changes none of these files. The first component pull
request, the MCP server and hooks, changes all four together.

## Other places that change with the rename

Each component's pull request also updates:

- **The lab:** the manifests in bgl-infra set the old names. They change
  through their own ask to the owner, after 0.103.0 is out, while the
  fallbacks still read the old names.
- **`.envrc` and configuration examples:** `.env.example`, the examples in
  `adapters/*.md` (Claude Code, Codex, corral, Cursor, OpenCode, pi),
  `console/server/README.md`, `services/auth/README.md`,
  `services/auth/nats-server.example.conf` and `console/server/Dockerfile`.
- **The website:** the quick start and every other name in `site/index.html`.
- **The README configuration section** ([README.md](../README.md#configuration)):
  its table of variables.
- **Texts agents read at runtime:** the hook `reason` texts, the
  `hooks/hooks.json` description (it names `MM_LISTEN_SEC` and
  `MM_LISTEN_COORD_SEC`), the MCP `instructions` in
  `mcp/src/mattermost_ai_agent_bus/server.py` (they name `MM_CHANNEL` and
  `MM_LISTEN_DIGEST_SEC`), and the deprecation and error messages.
- **The skill:** `skills/ai-agent-bus/SKILL.md` and its pages
  (`channels.md`, `runtime-claude-code.md`, `runtime-no-hooks.md`,
  `self-registration.md`).
- **The specification:** the arc42 sections under [docs/arc42](arc42),
  [docs/protocol.md](protocol.md) and [docs/smoke-test.md](smoke-test.md).
- **CI:** the names the workflows set (see [Tests and CI](#tests-and-ci-6)).
- **The CHANGELOG of 0.103.0:** every rename under `### Breaking`, with a
  `Needs:` line as above.
- **doc-check:** a rule that refuses a new environment variable without the
  `CB_` prefix, as the issue proposes.
