# Environment variable names: the `CB_` prefix

## Purpose and status

Every environment variable this project defines gets the `CB_` prefix
(choirbus). A variable that exists only because of Mattermost gets `CB_MM_`.
The owner decided this on 2026-11-26 ([#169](https://github.com/dembaca/ai-agent-bus/issues/169)).

**Held to the code.** `scripts/doc-check.py` reads this page and the tree: failing
CI is a name the code reads that the page does not list, and a mapping whose old
name no code mentions (the registrar's kept names excepted).

**Status.** This page is the mapping and the rules. The renames come in one
pull request per component and ship together as release 0.103.0. A component
reads the `CB_` names, with the old names as a fallback, once it has a line
below; until then it reads the old names, and the old names in the
[README configuration table](../README.md#configuration), the
[arc42 docs](arc42) and the adapter docs stay right for it.

- Built: the login service and `authd` ([#175](https://github.com/dembaca/ai-agent-bus/pull/175)),
  in `services/auth`, package `internal/envname`; the names of
  [their table](#the-login-service-and-authd-44).
- Built: the MCP server (`mcp/src/mattermost_ai_agent_bus/config.py`), the hooks,
  the shell scripts (`bin/mm-agent-lib.sh`), the host installer and the `Needs:`
  grammar ([#180](https://github.com/dembaca/ai-agent-bus/pull/180)).
- Built: the central bridge ([#176](https://github.com/dembaca/ai-agent-bus/pull/176)),
  in `services/bridge`, `internal/config/env.go` (the removal release's
  refusal is the constant `oldNamesRemoved` there); the names of
  [its table](#the-bridge-19).
- Built: the OpenCode and pi plugins ([#181](https://github.com/dembaca/ai-agent-bus/pull/181)),
  `envFirst`/`fileFirst` in both files; they read the new names with the old ones
  as a fallback and hand the server they start both spellings of
  `CB_WORKSPACE` and `CB_CHANNEL_PUSH`. A conflict there is reported once and the
  plugin behaves as without a bus, so the turn hooks stop; the server it started
  is not reached, and a server of its own refuses the start.
- Built: the console ([#188](https://github.com/dembaca/ai-agent-bus/pull/188)),
  `given()` in `console/server/src/console_server/config.py`; the names of
  [its table](#the-console-20).

## The rules

### Which prefix

- An `MM_` variable that exists only because of Mattermost (URLs, tokens, the
  team, channel names, bots, features only Mattermost has) becomes
  `CB_MM_<rest>`.
- The registrar's settings are not renamed: they go with the registrar
  (see [Goes with the registrar](#goes-with-the-registrar-10)).
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
   start and names both (never a secret's value).
4. The fallbacks go in 0.105.0 (the owner's decision of 2026-11-27; 0.104.0
   still reads the old names), called *the removal release* below. What happens to an old name from then on says
   [After the fallbacks go](#after-the-fallbacks-go-refuse-never-ignore).

A shell script that cannot log a warning (a hook that must stay quiet) writes
it to its log file instead. Two cases log no warning, since no person set the
old name there: a value the bus writes for itself (marked *internal* below),
and every name inside a keyless sandbox, that is with `CB_BUS_MCP_ENDPOINT`
or `MM_BUS_MCP_ENDPOINT` set: the host's session hook wrote the settings
there, an older one under the old names only. Rule 3 holds in both.

What counts as set:

- **Empty counts as set.** A name that is in the environment with an empty
  value is set: an empty new name never falls back to the old name, and an
  empty value next to a non-empty one is a disagreement (rule 3). Two
  settings give "empty" a meaning of its own, so anything else would turn
  them around: `MM_RELEASE_SIGNERS` set but empty refuses the update instead
  of installing without a signature check (`bin/mm-agent-lib.sh`, function
  `mm_release_signers`, its `${MM_RELEASE_SIGNERS+set}` test), and
  `MM_STATS_CHANNEL=""` switches the statistics off (`stats.py`,
  function `channel`).
- **An unexpanded placeholder: unset in the MCP server and the hooks, a
  refusal in the Go services and the console.** A value that is exactly
  `${NAME}` is a plugin manifest's placeholder that was not expanded. The MCP server and
  the hooks start from a manifest, so there it counts as unset, as `env()` in
  `mcp/src/mattermost_ai_agent_bus/config.py` does today; every fallback
  reader there uses that one helper, or its twin in the hooks. A service
  without a plugin manifest — the Go services under `services/` and the
  console — therefore has only a deployment mistake there: read as unset, it
  would silently drop the setting, a pinned CA file for one, and the system
  roots would apply. Such a service refuses the start with an error that
  names the variable, never its value.
- **Three names for one setting.** Two settings have an alias today:
  `MATTERMOST_URL` for `MM_CHAT_URL` and `MATTERMOST_TOKEN` for
  `MM_BOT_TOKEN`. In 0.103.0 each of them has three names
  (`CB_MM_CHAT_URL`, `MM_CHAT_URL`, `MATTERMOST_URL` and `CB_MM_BOT_TOKEN`,
  `MM_BOT_TOKEN`, `MATTERMOST_TOKEN`). Every one of the three that is set
  must have the same value, or the component refuses to start and names the
  names that differ. The new name wins only when they agree.

### Unknown names are warned about

The MCP server checks its environment at start (`config.unknown_names`): every
`CB_` name that this page does not list (and no other component's prefix
covers) becomes one warning line, `unknown setting CB_PROJECT_CHANNEL (did you
mean CB_MM_PROJECT_CHANNEL?)`, with the closest known name, the `CB_MM_` prefix
swap first. It shows in `bus_info`'s and `session_start`'s `warning` and once in
the session-start hook's line (RT-6.34 step 4). Names only, never a value.
An environment name may hold any character but `=`, so only names of the form
`[A-Z0-9_]{1,64}` are shown, and at most ten of them (`config.UNKNOWN_CAP`); the odd names
and those over the cap are counted in a last line, `and 3 more unknown CB_ settings`
(#269), so a host with many foreign `CB_` variables does not get a long line.
`tests/hooks/test_env_names.py` pins that every name this page lists is known to it.

### After the fallbacks go: refuse, never ignore

An old name that is simply no longer read fails open: an operator who did
not migrate loses the setting without a word, and for some settings that
switches a protection off. So from the removal release on, and for at least
one release after it, a component that finds an old name set **without** its
new name refuses to start and names both. A hook refuses the session in the
same way: the session-start hook does not start the bus for that session and
says why in its reason text, so the agent and its human see it. With both
names set, rule 3 of the reading rule still holds: different values refuse,
equal values pass with a warning that the old name can go.

These settings weaken a protection when an old name is ignored; they are the
reason for the rule and its tests name each of them:

- `MM_RELEASE_SIGNERS`: without it, and without a pinned signers file, an
  update installs without a signature check.
- `MM_TRUSTED_HUMANS`, `MM_TRUSTED_HUMAN_USER_IDS`, `MM_OWNER`,
  `MM_OWNER_USER_ID`, `MM_RESPONSIBLE_HUMAN`: who may approve; without them
  other rules decide who is trusted.
- `MM_NATS_CA_FILE`, `AUTH_NATS_CA_FILE`, `CONSOLE_NATS_CA_FILE`,
  `BRIDGE_CA_FILE`, `AUTHD_DIR_CA_FILE`: the pinned certificate authority;
  without it the system's roots count.
- `AUTH_BRAKE_FILE` (the emergency brake), `AUTH_DIRECTORY_IMPORTED` (guards
  the directory on NATS after the import), `AUTH_APPROVER_GROUP`,
  `AUTH_OPERATOR_GROUP`, `AUTH_DIRECTORY_ADMIN_GROUP`,
  `AUTH_KEYCLOAK_AUDIENCE`, `AUTH_NAME_BINDING`, `CONSOLE_OPERATOR_GROUP`:
  the login service's and the console's checks of who may do what.

The rule covers every renamed name, not only these. A few more change
behaviour silently when ignored without weakening a protection, for example
`MM_LOGIN_NKEY`: a host falls back to an ephemeral key, and its sessions lose
their names across a restart (#163). The refusal catches those too.

`MM_REGISTRAR_KEYS` (the pinned registrar keys) is not on the list: it keeps
its name and goes with the registrar (see
[Goes with the registrar](#goes-with-the-registrar-10)).

### Lists of names

Some code does not read one name but keeps a list of them: what may pass to
a child process, what must never pass, what is unset or removed after use.
A list that knows only one spelling either lets the other one through (a
deny or unset list) or drops it (an allow or pass list). So in 0.103.0
**every such list carries both the old and the new name**, and a pattern
that matches `MM_` names also matches `CB_` names. In the removal release
the old names leave the allow and pass lists, but stay in the deny and
unset lists for as long as the refusal above lasts: a deny entry too many
costs nothing.

A list that is only read through the reading rule (`config.env` in the MCP
server) is the exception: the reading rule finds the old name, so the list
names the new name only. Those are `TRUSTED_VARS`, `SANDBOX_MARKERS` and
`WORKSPACE_VARS` below.

The lists today, by the symbol or line that holds them:

| List | Where | What it does |
|---|---|---|
| `_HOST_ENV` | `mcp/src/mattermost_ai_agent_bus/hosted.py` | pattern of the names preStart may hand the host process; matches `CB_` names too |
| `_HOST_ENV_DENY` | `hosted.py` | never handed to the host: the registration secret and its gopass path, the session file, the bot name, `MM_BUS_KEY`, `MM_BUS_MCP_ENDPOINT`, `MM_BUS_ENDPOINT`, `MM_BUS_UNAVAILABLE`, and `CB_AGENT_SESSION_FILE`, `CB_BUS_KEY`, `CB_BUS_MCP_ENDPOINT`, `CB_BUS_ENDPOINT`, `CB_BUS_UNAVAILABLE` |
| `_TOKEN_ENV` | `hosted.py` | removed from the host process's environment once read: `CB_MM_BOT_TOKEN`, `MM_BOT_TOKEN`, `MATTERMOST_TOKEN` |
| `NAMING`, `_NAMING_PATHS`, `_IDENTITY` | `hosted.py` | what a relay may tell the host, and which of it says whose relay it is |
| the start line's filter | `adapters/corral/10-mm-session-start.sh`, the `jq` `test` and `del` that build the host's start line | the hook's own copy of `_HOST_ENV` and of the deny list, including `MATTERMOST_TOKEN`, `MM_BUS_KEPT_SID`, `CB_MM_BOT_TOKEN` and `CB_BUS_KEPT_SID` |
| `ENV_JSON` | `10-mm-session-start.sh` | what the sandbox gets: written under both names (see below), the chat URL also as `MATTERMOST_URL` |
| `unset CB_MM_BOT_TOKEN MM_BOT_TOKEN MATTERMOST_TOKEN` | the top of `10-mm-session-start.sh` and of `adapters/corral/90-mm-session-stop.sh` | no token inherited by the hooks |
| other `unset` lines | `10-mm-session-start.sh` (`CB_BUS_HOOK_DISPATCH`, `MM_BUS_HOOK_DISPATCH`) | the dispatcher's mark goes once read |
| `self_update` pass list | `90-mm-session-stop.sh`, function `self_update` | what the update under `env -i` gets: both spellings of `CB_BUS_HOST_DIR`, `CB_RELEASE_URL`, `CB_RELEASE_SIGNERS`, `CB_MM_CHAT_URL`, and `MM_REGISTRAR_KEYS`, `MM_REGISTER_URL`; without the new names a host that set only `CB_RELEASE_SIGNERS` would update without its signers |
| `mm_registrar_release` pass list | `bin/mm-agent-lib.sh`, function `mm_registrar_release` | `MM_REGISTRAR_KEYS`, `MM_REGISTER_URL`, `CB_MM_CHAT_URL`, `MM_CHAT_URL` for the release record check |
| `CHILD_ENV` | `mcp/src/mattermost_ai_agent_bus/release.py` | the same for the server's update check: both spellings of `CB_RELEASE_URL`, `CB_RELEASE_SIGNERS`, `CB_BUS_HOST_DIR`, `CB_MM_CHAT_URL` |
| `CB_ENV_NAMES` | `bin/mm-agent-lib.sh` | every setting the scripts and hooks read, each new name with its old ones; a test holds it to `RENAMED` in `config.py` |
| `mm_load_env_file` | `bin/mm-agent-lib.sh` | the session file keys a script takes into its environment |
| the kept bot's keys | `10-mm-session-start.sh`, where it takes over a kept session file (`mm_kv "$claim" …`) | the session file keys a continued session takes over |
| `TRUSTED_VARS` | `mcp/src/mattermost_ai_agent_bus/owners.py` | the trust settings; read through the reading rule, new names only |
| `SANDBOX_MARKERS` | `mcp/src/mattermost_ai_agent_bus/sandbox.py` | the names that say "inside a sandbox"; read through the reading rule, new names only |
| `WORKSPACE_VARS` | `mcp/src/mattermost_ai_agent_bus/session_file.py` | where the workspace comes from; read through the reading rule, new names only |
| the test rig's filter | `testrig/src/bus_testrig/bus.py`, function `_clean_env` | drops every inherited `CB_`, `MM_` and `MATTERMOST_` name |
| the plugins' server environment | `opencode/plugin/mattermost-agent-bus.ts`, function `addMcp`; `pi/mattermost-agent-bus.ts`, function `mcpConfig` | what the plugins hand the MCP server they start: both spellings of `CB_WORKSPACE` and `CB_CHANNEL_PUSH` |

The registrar's names (`MM_REG_SECRET`, `MM_REG_SECRET_GOPASS`,
`MM_REG_BOT_NAME` and the others of
[Goes with the registrar](#goes-with-the-registrar-10)) keep their names, so
their entries in these lists stay as they are and get no new spelling.

### Values the bus writes for itself

Values the bus sets for its own children or writes into its own files
(marked *internal* in the tables, and the keys in
[the session files](#keys-in-the-session-files-9)) are written under **both**
names in 0.103.0, so a reader of the old version still finds them, and every
reader accepts either name. In the environment the new name comes first
(the reading rule). In a file the bus wrote, the last line of either
spelling wins and keys that differ never refuse: a downgraded writer
appends its old key after the new one, and the later line is the true one
(`config.value` in the MCP server, `mm_kv` and `mm_load_env_file` in
`bin/mm-agent-lib.sh`; `session_file.parse` keeps each key at its last line). A host process, a relay, a
hook and a sweep of different versions meet during an update, and a kept
session file lives up to a day. This holds for:

- the session files, `session_file.py:328-340` (including the alias keys
  `MATTERMOST_URL` and `MATTERMOST_TOKEN`);
- the endpoint files `endpoint-*.env`, written in `hookapi.py:328-331`
  (`MM_BUS_ENDPOINT`, `MM_BUS_KEY`, `MM_BUS_PID`, `MM_BOT_USER_ID`,
  `MM_BUS_CLIENT`): an old `bin/mm-agent-sweep.sh:271` must still find
  `MM_BUS_PID`, the OpenCode and pi plugins take `CB_BUS_ENDPOINT`/`CB_BUS_KEY`
  or their old spellings from them, the last line of either winning;
- `claude-sid.env` (`MM_CLAUDE_SID`, `hosted.py:445`);
- the relay's message to the host, `session_values()` in `hosted.py:178-186`:
  it sends `MM_WORKSPACE` and `CB_WORKSPACE`, `MM_CHANNEL_PUSH` and
  `CB_CHANNEL_PUSH`, so an old host process still gets them; `NAMING`
  accepts both, and `_IDENTITY` takes the pair as one value (both present
  and different is a refused relay);
- the start line the session-start hook hands the host process, and
  `ENV_JSON`, the sandbox's environment;
- the variables set for children (`MM_BUS_ENDPOINT`, `MM_BUS_KEY`,
  `MM_BUS_MCP_ENDPOINT`, `MM_BUS_UNAVAILABLE`, `MM_BUS_KEPT_SID`,
  `MM_BUS_DEADLINE`, `MM_BUS_UPLOAD`, `MM_REL_GIT_DIR`, `MM_REL_TMP`), and
  the server entry the OpenCode plugin writes into OpenCode's configuration
  (its `environment`, which carries `CB_WORKSPACE` and `MM_WORKSPACE`).

Writing both names doubles every secret that travels this way, so the
[lists of names](#lists-of-names) must hold both before the writers do. In
the removal release the writers write the new name only; the readers of
these internal values still accept the old one in that release, since a
value the bus wrote itself is no setting a person could lose.

### Collision check

The tables below map 158 old names (each name counted once) to new names;
10 more keep their names and go with the registrar. The check for this page
found:

- No new name was in use in the repository before the rename: no `CB_`
  name existed. The built components' new names are the ones in these
  tables.
- No two old names map to one new name, with two deliberate exceptions.
  `MATTERMOST_URL` is today an alias of `MM_CHAT_URL`, and `MATTERMOST_TOKEN`
  an alias of `MM_BOT_TOKEN`. Each pair is one setting already, so each maps
  to one new name, and the alias is dropped with the fallbacks.
- No new `CB_X` equals a new `CB_MM_X`, `CB_AUTH_X`, `CB_BRIDGE_X` or
  `CB_CONSOLE_X` name.

## The tables

Columns: the old name, the new name, whether the variable is
Mattermost-specific, where it appears, and a note. "Where it appears" lists
every file outside the tests that names it: it reads it, sets it, passes it
on, or names it in a comment or a message. The rename touches each of them. Paths without a
directory in the first two tables are in `mcp/src/mattermost_ai_agent_bus/`.
A name read by several components appears in each component's table, since
each component's pull request renames its own reads.

### The MCP server and the plugin (53)

| Old name | New name | Mattermost | Where it appears | Note |
|---|---|---|---|---|
| `MM_ACK_WAIT_SEC` | `CB_ACK_WAIT_SEC` | no | `askroute.py`, `lease.py`, `life.py`, `receipt.py` | |
| `MM_AGENT_KIND` | `CB_AGENT_KIND` | no | `attest.py`, `client.py`, `server.py`, `stats.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-register.sh` | session or autonomous |
| `MM_ASK_REMIND_SEC` | `CB_ASK_REMIND_SEC` | no | `remind.py` | |
| `MM_ASK_ROUTE` | `CB_ASK_ROUTE` | no | `askroute.py`, `server.py` | |
| `MM_ASK_ROUTE_WAIT_MIN` | `CB_ASK_ROUTE_WAIT_MIN` | no | `askroute.py` | |
| `MM_BOT_TOKEN` | `CB_MM_BOT_TOKEN` | yes | `client.py`, `compose.py`, `hosted.py`, `registration.py`, `server.py`, `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-channels.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh`, `bin/mm-agent-status.sh`, `bin/mm-agent-sweep.sh`, `bin/mm-agent-unread.sh`, `bin/mm-agent-unregister.sh`, `testrig/src/bus_testrig/bus.py` | |
| `MATTERMOST_TOKEN` | `CB_MM_BOT_TOKEN` | yes | `client.py`, `hosted.py`, `server.py`, `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh`, `bin/mm-agent-status.sh`, `bin/mm-agent-sweep.sh`, `bin/mm-agent-unread.sh`, `bin/mm-agent-unregister.sh` | alias of `MM_BOT_TOKEN` today; dropped with the fallbacks |
| `MM_BOT_USER_ID` | `CB_MM_BOT_USER_ID` | yes | `client.py`, `hookapi.py`, `lease.py`, `nats_port.py`, `owners.py`, `server.py`, `session_file.py`, `stats.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-listen.sh`, `bin/mm-agent-register.sh`, `bin/mm-agent-stats.sh`, `bin/mm-agent-status.sh`, `bin/mm-agent-statusline.sh`, `bin/mm-agent-sweep.sh` | |
| `MM_BOT_USERNAME` | `CB_MM_BOT_USERNAME` | yes | `client.py`, `nats_port.py`, `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-listen.sh`, `bin/mm-agent-register.sh`, `bin/mm-agent-statusline.sh`, `bin/mm-agent-sweep.sh` | |
| `MM_BRIDGE_BOT` | `CB_MM_BRIDGE_BOT` | yes | `compose.py`, `lease.py`, `nats_port.py` | the bridge's Mattermost bot account |
| `MM_BUS_ENDPOINT` | `CB_BUS_ENDPOINT` | no | `hookapi.py`, `hosted.py`, `sandbox.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh` | internal: the hook API, set by the host hook |
| `MM_BUS_KEPT_SID` | `CB_BUS_KEPT_SID` | no | `hosted.py`; `adapters/corral/10-mm-session-start.sh` | internal |
| `MM_BUS_KEY` | `CB_BUS_KEY` | no | `hookapi.py`, `hosted.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh` | internal |
| `MM_BUS_MCP_ENDPOINT` | `CB_BUS_MCP_ENDPOINT` | no | `hosted.py`, `sandbox.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh`, `bin/mm-agent-statusline.sh` | internal |
| `MM_BUS_UNAVAILABLE` | `CB_BUS_UNAVAILABLE` | no | `hosted.py`, `server.py`; `adapters/corral/10-mm-session-start.sh`, `adapters/corral/mm-hook-dispatch.sh` | internal |
| `MM_CHANNEL` | `CB_MM_CHANNEL` | yes | `client.py`, `compose.py`, `homes.py`, `port.py`, `server.py`, `session_file.py`, `websocket_inbox.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-channels.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-poll.sh`, `bin/mm-agent-post.sh`, `testrig/src/bus_testrig/bus.py` | the lobby's channel name |
| `MM_CHANNEL_ID` | `CB_MM_CHANNEL_ID` | yes | `client.py`, `homes.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-channels.sh`, `bin/mm-agent-lib.sh` | |
| `MM_CHANNEL_PUSH` | `CB_CHANNEL_PUSH` | no | `channel.py`, `hosted.py`, `server.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh`, `testrig/src/bus_testrig/bus.py` | Claude Code's channel mode, not a Mattermost channel |
| `MM_CHAT_URL` | `CB_MM_CHAT_URL` | yes | `client.py`, `compose.py`, `release.py`, `selfupdate.py`, `server.py`, `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh`, `bin/mm-agent-registrar-keys.sh`, `bin/mm-agent-status.sh`, `bin/mm-agent-unread.sh`, `testrig/src/bus_testrig/bus.py` | |
| `MATTERMOST_URL` | `CB_MM_CHAT_URL` | yes | `client.py`, `selfupdate.py`, `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh`, `bin/mm-agent-status.sh`, `bin/mm-agent-unread.sh` | alias of `MM_CHAT_URL` today; dropped with the fallbacks |
| `MM_CROWN_BEAT_SEC` | `CB_CROWN_BEAT_SEC` | no | `lease.py` | |
| `MM_CROWN_LEASE_SEC` | `CB_CROWN_LEASE_SEC` | no | `lease.py`, `life.py` | |
| `MM_HEARTBEAT_SEC` | `CB_HEARTBEAT_SEC` | no | `lease.py` | |
| `MM_LEASE_SEC` | `CB_LEASE_SEC` | no | `departed.py`, `lease.py`, `life.py`; `bin/mm-agent-lib.sh`, `bin/mm-agent-status.sh` | |
| `MM_LISTEN_COORD_SEC` | `CB_LISTEN_COORD_SEC` | no | `turnend.py`; `hooks/hooks.json`, `hooks/stop-inbox-check.sh` | |
| `MM_LISTEN_DIGEST_SEC` | `CB_LISTEN_DIGEST_SEC` | no | `channel.py`, `server.py`; `bin/mm-agent-listen.sh` | |
| `MM_LISTEN_SEC` | `CB_LISTEN_SEC` | no | `turnend.py`; `hooks/hooks.json`, `hooks/stop-inbox-check.sh` | |
| `MM_LOGIN_CLIENT_ID` | `CB_LOGIN_CLIENT_ID` | no | `compose.py`, `login.py` | |
| `MM_LOGIN_ISSUER` | `CB_LOGIN_ISSUER` | no | `compose.py`, `login.py` | |
| `MM_LOGIN_NKEY` | `CB_LOGIN_NKEY` | no | `login.py` | |
| `MM_NATS_CA_FILE` | `CB_NATS_CA_FILE` | no | `compose.py`, `natsbus.py` | |
| `MM_NATS_HOMES` | `CB_NATS_HOMES` | no | `compose.py`, `hosted.py`, `lease.py`, `nats_port.py`, `server.py` | The channels over NATS: homes, and the guests and the lobby the hello asks for (plan B piece 12, RT-6.22 step 7) |
| `MM_NATS_MEMORY` | `CB_NATS_MEMORY` | no | `compose.py` | |
| `MM_NATS_URL` | `CB_NATS_URL` | no | `compose.py` | |
| `MM_OWNER` | `CB_OWNER` | no | `attest.py`, `client.py`, `compose.py`, `owners.py`, `permission.py`; `bin/mm-agent-lib.sh` | human names; the NATS side reads them too |
| `MM_OWNER_USER_ID` | `CB_MM_OWNER_USER_ID` | yes | `client.py`, `owners.py`, `permission.py` | a Mattermost user id |
| `MM_PROJECT_CHANNEL` | `CB_MM_PROJECT_CHANNEL` | yes | `client.py`, `config.py`, `port.py`, `server.py`, `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-channels.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-poll.sh`, `bin/mm-agent-post.sh` | |
| `MM_PROJECT_CHANNEL_ID` | `CB_MM_PROJECT_CHANNEL_ID` | yes | `client.py`, `homes.py`, `hookapi.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-channels.sh`, `bin/mm-agent-lib.sh` | |
| `MM_PROPS_PREFIX` | `CB_PROPS_PREFIX` | no | `a2a.py`, `compose.py`, `hosted.py`, `nats_port.py`, `natsbus.py`, `server.py`; `services/bridge/internal/config/config.go`, `services/bridge/internal/message/message.go`, `services/bridge/internal/mirror/mirror.go`, `services/bridge/internal/reader/reader.go` | one setting of the whole bus: the NATS messages carry the same keys |
| `MM_REMIND_MAX_SEC` | `CB_REMIND_MAX_SEC` | no | `remind.py`, `stall.py` | |
| `MM_RESPONSIBLE_HUMAN` | `CB_RESPONSIBLE_HUMAN` | no | `attest.py`, `login.py`, `owners.py`, `profile.py`, `server.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh` | |
| `MM_SESSION_KEEP_SEC` | `CB_SESSION_KEEP_SEC` | no | `client.py`, `server.py`, `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-sweep.sh` | |
| `MM_STALL_SEC` | `CB_STALL_SEC` | no | `stall.py` | |
| `MM_STATS_CHANNEL` | `CB_MM_STATS_CHANNEL` | yes | `server.py`, `stats.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-stats.sh`; `services/auth` (`internal/app/callout.go`) | the Mattermost channel; over NATS the login service's stats prefix counts; the login service's STATS import reads this channel (RT-6.45) |
| `MM_TEAM` | `CB_MM_TEAM` | yes | `client.py`, `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-channels.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-poll.sh`, `testrig/src/bus_testrig/bus.py` | |
| `MM_TRUSTED_HUMAN_USER_IDS` | `CB_MM_TRUSTED_HUMAN_USER_IDS` | yes | `client.py`, `owners.py`, `permission.py`; `adapters/corral/10-mm-session-start.sh` | Mattermost user ids |
| `MM_TRUSTED_HUMANS` | `CB_TRUSTED_HUMANS` | no | `attest.py`, `client.py`, `compose.py`, `login.py`, `natsbus.py`, `owners.py`, `permission.py`, `profile.py`, `server.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh`, `services/auth/internal/callout/service.go`, `services/auth/internal/directory/directory.go` | human names; the NATS side reads them too |
| `MM_TYPING_SEC` | `CB_MM_TYPING_SEC` | yes | `server.py` | Mattermost's typing indicator |
| `MM_WORKSPACE` | `CB_WORKSPACE` | no | `hookapi.py`, `hosted.py`, `session_file.py`; `bin/mm-agent-lib.sh` | |
| `MM_RELEASE_SIGNERS` | `CB_RELEASE_SIGNERS` | no | `release.py`; `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-host-install.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-release-check.sh` | also in the hooks table |
| `MM_RELEASE_URL` | `CB_RELEASE_URL` | no | `release.py`; `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-host-install.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-release-check.sh` | also in the hooks table |
| `CB_RECORDS_URL` | `CB_RECORDS_URL` | no | `release.py` | new with plan B piece 14b (#244): no old spelling; the bridge's address for the signed records, where the release notice reads `GET …/register/v1/release` and checks the bridge's signature; unset: off, the host's release check as before. Built for the release record; the other signed records follow |
| `CB_PLANB_TEST` | `CB_PLANB_TEST` | no | `planb.py` | new with #244: no old spelling; plan B pieces switched on for tests, read only while pytest runs a test |

#### Why the channel names get `CB_MM_` and `BRIDGE_HOMES` does not

`MM_CHANNEL` (the lobby) and `MM_PROJECT_CHANNEL` (the home and the guest
channels) name homes, and so does `BRIDGE_HOMES`. The first two are settings
of the Mattermost adapter: `client.py` reads them together with `MM_TEAM`,
and the host hook (`bin/mm-agent-channels.sh`) resolves them in that
Mattermost team to the channel ids `MM_CHANNEL_ID` and
`MM_PROJECT_CHANNEL_ID`. A session that runs over NATS still gets its home
and lobby from that adapter: `compose.py` wraps the Mattermost adapter in the
NATS port and hands it the adapter's channel names, and `MM_NATS_HOMES` only
picks which of them run over NATS. There is no way today to name a home
without a Mattermost channel, so the two are Mattermost settings.
`BRIDGE_HOMES` is the bridge's choice of which homes it mirrors; its values
are home names on both sides, and the setting is about mirroring, not about
Mattermost. Once a home can exist without Mattermost, the two channel
settings need a `CB_` name that is not about Mattermost; that is a change of
its own, not part of this rename.

### Hooks and scripts (13)

These live in `bin/`, `hooks/` and `adapters/corral/`. The scripts also read
most names of the table above; those are renamed there.

| Old name | New name | Mattermost | Where it appears | Note |
|---|---|---|---|---|
| `MM_AGENT_SESSION_FILE` | `CB_AGENT_SESSION_FILE` | no | `hosted.py`; `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-lib.sh` | |
| `MM_BOT_TOKEN_FD` | `CB_MM_BOT_TOKEN_FD` | yes | `bin/mm-agent-lib.sh`, `bin/mm-agent-status.sh` | internal: hands the token to a helper on a file descriptor |
| `MM_BUS_DEADLINE` | `CB_BUS_DEADLINE` | no | `bin/mm-agent-lib.sh`, `hooks/after-compact.sh`, `hooks/stop-inbox-check.sh`, `hooks/turn-status.sh` | internal; set by `hooks/stop-inbox-check.sh`, `hooks/after-compact.sh` |
| `MM_BUS_HOOK_DISPATCH` | `CB_BUS_HOOK_DISPATCH` | no | `adapters/corral/10-mm-session-start.sh`, `adapters/corral/mm-hook-dispatch.sh` | |
| `MM_BUS_HOST_DIR` | `CB_BUS_HOST_DIR` | no | `release.py`; `adapters/corral/90-mm-session-stop.sh`, `adapters/corral/mm-hook-dispatch.sh`, `bin/mm-agent-host-install.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-release-check.sh` | |
| `MM_BUS_MAX_TIME` | `CB_BUS_MAX_TIME` | no | `bin/mm-agent-lib.sh`, `bin/mm-agent-poll.sh`, `bin/mm-agent-post.sh`, `bin/mm-agent-statusline.sh`, `hooks/stop-inbox-check.sh` | |
| `MM_BUS_UPLOAD` | `CB_BUS_UPLOAD` | no | `bin/mm-agent-lib.sh`, `bin/mm-agent-post.sh` | internal; set by `bin/mm-agent-post.sh` |
| `MM_LISTEN_POLL_SEC` | `CB_LISTEN_POLL_SEC` | no | `bin/mm-agent-listen.sh` | |
| `MM_RELEASE_SIGNERS` | `CB_RELEASE_SIGNERS` | no | `release.py`; `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-host-install.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-release-check.sh` | |
| `MM_RELEASE_URL` | `CB_RELEASE_URL` | no | `release.py`; `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-host-install.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-release-check.sh` | |
| `MM_TEAM_ID` | `CB_MM_TEAM_ID` | yes | `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-channels.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-sweep.sh`, `bin/mm-agent-unread.sh` | |
| `MM_REL_GIT_DIR` | `CB_REL_GIT_DIR` | no | `bin/mm-agent-host-install.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-release-check.sh` | internal; exported by `bin/mm-agent-release-check.sh` |
| `MM_REL_TMP` | `CB_REL_TMP` | no | `bin/mm-agent-host-install.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-release-check.sh` | internal; exported by `bin/mm-agent-release-check.sh` |

#### Keys in the session files (10)

These are keys in the files the bus writes into the host's state directory
(`KEY=value` lines), not variables a person sets. A kept session file lives
up to a day, so in 0.103.0 the writer writes both keys and the readers take
either (see [Values the bus writes for itself](#values-the-bus-writes-for-itself)).

| Old key | New key | Mattermost | Where it appears | Note |
|---|---|---|---|---|
| `MM_BUS_CLIENT` | `CB_BUS_CLIENT` | no | `hookapi.py` | |
| `MM_BUS_HOST_PID` | `CB_BUS_HOST_PID` | no | `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-host-install.sh`, `bin/mm-agent-sweep.sh` | |
| `MM_BUS_HOST_START` | `CB_BUS_HOST_START` | no | `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh` | |
| `MM_BUS_PID` | `CB_BUS_PID` | no | `hookapi.py`; `bin/mm-agent-sweep.sh` | |
| `MM_CLAUDE_SID` | `CB_CLAUDE_SID` | no | `hosted.py`; `adapters/corral/90-mm-session-stop.sh` | in `claude-sid.env` |
| `MM_KEPT_CLAUDE_SID` | `CB_KEPT_CLAUDE_SID` | no | `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh` | |
| `MM_KEPT_WORKDIR` | `CB_KEPT_WORKDIR` | no | `bin/mm-agent-lib.sh` | |
| `MM_SERVER_ENDED` | `CB_SERVER_ENDED` | no | `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-sweep.sh` | |
| `CB_SESSION_NONCE` | `CB_SESSION_NONCE` | no | `session_file.py` | new in 0.103.0: no old spelling, in `nonce-<uid>.env` (#163) |
| `MM_SESSION_PID` | `CB_SESSION_PID` | no | `client.py`, `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-host-install.sh`, `bin/mm-agent-sweep.sh` | |

The session files also carry variables from the first table
(`MM_BOT_TOKEN`, `MM_BOT_USERNAME`, `MM_BOT_USER_ID`, `MM_BUS_KEY` and
others). Those keys follow the same mapping and the same rule; their readers
include `bin/mm-agent-statusline.sh`, which takes `MM_BOT_USERNAME` and
`MM_BOT_USER_ID` from the session file. The registrar's keys in them
(`MM_REG_BOT_NAME`, `MM_AGENT_PREFIX`, `MM_BOT_ATTESTATION`) keep their
names.

#### Shell variables that are not environment

These names look like settings but are globals inside the scripts: function
results, file names, the release check's state and the emoji constants of
`bin/mm-agent-symbols.sh`. Nobody sets them from outside. The hooks pull
request renamed them by the same rule, without a fallback (each was the same
name with `MM_` for `CB_`):

- under `CB_MM_…`: `CB_MM_API_BODY`, `CB_MM_API_STATUS`;
- under `CB_…`: `CB_ANSWER`, `CB_AUTO_FILE`, `CB_AUTO_LOG`, `CB_BUS_BODY`,
  `CB_BUS_CHANNEL`, `CB_BUS_FROM`, `CB_BUS_PROJECT`, `CB_BUS_STATUS`,
  `CB_CREDS_FROM_FD`, `CB_CROWN`, `CB_CROWN_UNTIL`, `CB_HOLD_FILE`,
  `CB_HOOK_INPUT`, `CB_INBOX`, `CB_LISTENING`, `CB_NEEDS_RE`,
  `CB_REL_ALLOWED`, `CB_REL_COMMIT`, `CB_REL_ENFORCE`, `CB_REL_ERR`,
  `CB_REL_FROM`, `CB_REL_FROM_TEXT`, `CB_REL_KEYS`, `CB_REL_PARSED`,
  `CB_REL_PINNED`, `CB_REL_REPLACE`, `CB_REL_SIGNER`, `CB_REL_URL`,
  `CB_SIGNERS_FILE`, `CB_SIGNERS_MAX`, `CB_SIGNOFF`, `CB_UPDATE`,
  `CB_WITHDRAWN_FILE`;
- kept: `BUS_KEY` (a shell local in the session-start hook, the bus's key passed
  from fd; it reaches the server as `CB_BUS_KEY`);
- new with the rename, the reading rule's own state in `bin/mm-agent-lib.sh`:
  `CB_ENV_DEFER`, `CB_ENV_ERR`, `CB_ENV_INTERNAL`, `CB_ENV_NAMES`,
  `CB_ENV_QUIET`, `CB_ENV_WARNED`;
- not renamed, they go with the registrar: `MM_UNREG_HTTP`,
  `MM_UNREG_TOKEN_HTTP`, `MM_UNREG_WHY` (unregistering a bot) and
  `MM_RR_KEYS`, `MM_RR_KID`, `MM_RR_STATUS`, `MM_RR_VERSION`, `MM_RR_WHY`
  (the registrar's release record).

### The login service and authd (48)

All in `services/auth/`. Its binary `authd` runs the service; its directory
commands read the two `AUTHD_` names and two of the service's names (noted).
Besides the files a row lists, every name here appears in the service's
rename table `internal/envname/envname.go`, which maps each new name to its
old one; `CB_AUTH_LDAP_ID_CLAIM` (#266) is new since the rename and gives its
own name as the old one, as the bridge's `CB_BRIDGE_MM_BOT_KEEP` does. One row
is not the service's own: `CB_MM_STATS_CHANNEL` is the MCP
server's setting, listed with it above, which the login service reads by the
same rule for its STATS import (RT-6.45).

| Old name | New name | Mattermost | Where it appears | Note |
|---|---|---|---|---|
| `AUTH_ACCOUNT` | `CB_AUTH_ACCOUNT` | no | `internal/app/callout.go` | |
| `AUTH_ADMIN_LISTEN_ADDR` | `CB_AUTH_ADMIN_LISTEN_ADDR` | no | `internal/app/callout.go` | |
| `AUTH_ADMIN_TLS_CERT_FILE` | `CB_AUTH_ADMIN_TLS_CERT_FILE` | no | `internal/app/callout.go` | |
| `AUTH_ADMIN_TLS_KEY_FILE` | `CB_AUTH_ADMIN_TLS_KEY_FILE` | no | `internal/app/callout.go` | |
| `AUTH_APPROVER_CLAIM` | `CB_AUTH_APPROVER_CLAIM` | no | `internal/app/callout.go` | |
| `AUTH_APPROVER_GROUP` | `CB_AUTH_APPROVER_GROUP` | no | `internal/app/callout.go` | |
| `AUTH_BRAKE_FILE` | `CB_AUTH_BRAKE_FILE` | no | `internal/app/callout.go` | |
| `AUTH_CONSOLE_CLIENT_ID` | `CB_AUTH_CONSOLE_CLIENT_ID` | no | `internal/app/callout.go`, `internal/callout/person.go` | |
| `AUTH_CONSOLE_METRICS` | `CB_AUTH_CONSOLE_METRICS` | no | `internal/app/callout.go`, `internal/callout/person.go`; the console's docs | the unattended metrics page's consumers (#158, RT-6.43 step 10): `true` or `false` |
| `AUTH_DIRECTORY_ADMIN_GROUP` | `CB_AUTH_DIRECTORY_ADMIN_GROUP` | no | `internal/app/callout.go` | |
| `AUTH_DIRECTORY_BUCKET` | `CB_AUTH_DIRECTORY_BUCKET` | no | `internal/app/callout.go` | |
| `AUTH_DIRECTORY_IMPORTED` | `CB_AUTH_DIRECTORY_IMPORTED` | no | `internal/app/callout.go`, `internal/admin/import.go`, `internal/directory/directory.go` | guards the directory on NATS after the import |
| `AUTH_ISSUER_SEED_FILE` | `CB_AUTH_ISSUER_SEED_FILE` | no | `internal/app/callout.go` | |
| `AUTH_JS_SEED_FILE` | `CB_AUTH_JS_SEED_FILE` | no | `internal/app/callout.go` | |
| `AUTH_JS_URL` | `CB_AUTH_JS_URL` | no | `internal/app/callout.go` | |
| `AUTH_KEYCLOAK_AUDIENCE` | `CB_AUTH_KEYCLOAK_AUDIENCE` | no | `internal/app/callout.go` | |
| `AUTH_KEYCLOAK_CLIENT_ID` | `CB_AUTH_KEYCLOAK_CLIENT_ID` | no | `internal/app/callout.go`, `internal/dircli/dircli.go` | also `authd` |
| `AUTH_KEYCLOAK_ISSUER` | `CB_AUTH_KEYCLOAK_ISSUER` | no | `internal/app/callout.go`, `internal/dircli/dircli.go` | also `authd` |
| `AUTH_KEYCLOAK_JWKS_URL` | `CB_AUTH_KEYCLOAK_JWKS_URL` | no | `internal/app/callout.go` | |
| `CB_AUTH_LDAP_ID_CLAIM` | `CB_AUTH_LDAP_ID_CLAIM` | no | `internal/app/callout.go`, `internal/token/config.go` | new with #266: no old spelling; the token claim with the LDAP entry's id, default `ldap_id`: a Mattermost LDAP account whose `auth_data` equals it is proven (RT-6.42 step 3) |
| `AUTH_LISTEN_ADDR` | `CB_AUTH_LISTEN_ADDR` | no | `internal/config/config.go` | |
| `AUTH_LOBBY` | `CB_AUTH_LOBBY` | no | `internal/app/callout.go`; a comment in `natsbus.py` | the lobby on NATS; must equal the sessions' `CB_MM_CHANNEL` |
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
| `AUTH_OPERATOR_GROUP` | `CB_AUTH_OPERATOR_GROUP` | no | `internal/app/callout.go`, `internal/callout/person.go` | |
| `AUTH_PERSON_MAX_LOGINS` | `CB_AUTH_PERSON_MAX_LOGINS` | no | `internal/app/callout.go`, `internal/callout/person.go` | |
| `AUTH_PERSON_PREFIX` | `CB_AUTH_PERSON_PREFIX` | no | `internal/app/callout.go`, `internal/callout/person.go` | |
| `AUTH_PLAIN_REQUESTS` | `CB_AUTH_PLAIN_REQUESTS` | no | `internal/app/callout.go` | |
| `AUTH_PRESENCE_PREFIX` | `CB_AUTH_PRESENCE_PREFIX` | no | `internal/app/callout.go` | |
| `AUTH_REQUEST_TIMEOUT` | `CB_AUTH_REQUEST_TIMEOUT` | no | `internal/app/callout.go` | |
| `AUTH_REQ_PREFIX` | `CB_AUTH_REQ_PREFIX` | no | `internal/app/callout.go` | the request subjects' prefix (ADR-014 phase 5), default `req` |
| `AUTH_SHUTDOWN_TIMEOUT` | `CB_AUTH_SHUTDOWN_TIMEOUT` | no | `internal/config/config.go` | |
| `AUTH_STATS_PREFIX` | `CB_AUTH_STATS_PREFIX` | no | `internal/app/callout.go`, `internal/callout/perms.go`, `internal/consumers/streams.go`; a comment in `natsbus.py` | |
| `AUTH_SUBJECT_PREFIX` | `CB_AUTH_SUBJECT_PREFIX` | no | `internal/app/callout.go` | |
| `AUTH_TOKEN_LEEWAY` | `CB_AUTH_TOKEN_LEEWAY` | no | `internal/app/callout.go` | |
| `AUTH_XKEY_SEED_FILE` | `CB_AUTH_XKEY_SEED_FILE` | no | `internal/app/callout.go` | |
| `AUTHD_DIR_API` | `CB_AUTHD_DIR_API` | no | `internal/dircli/dircli.go` | `authd` only |
| `AUTHD_DIR_CA_FILE` | `CB_AUTHD_DIR_CA_FILE` | no | `internal/dircli/dircli.go` | `authd` only |
| `MM_STATS_CHANNEL` | `CB_MM_STATS_CHANNEL` | yes | `internal/app/callout.go` | the MCP server's setting (above), read the same way: the channel the STATS import reads (RT-6.45); off, none, 0, false or empty turn the import off |

`services/auth/nats-server.example.conf` names several of them in comments;
it changes with the service.

### The bridge (33)

All in `services/bridge/`, read in `internal/config/config.go`. The last
three renames come with the bridge's agent bots
([#166](https://github.com/dembaca/ai-agent-bus/pull/166), not merged when
this was written); the sweep's keep time (#151 step 2), the slash commands'
ten (#151 step 3) and the release record's two (#151 step 4) are new after the
rename and have no old spelling. The bridge as a whole exists for
Mattermost; the column says whether the setting itself is about Mattermost.

| Old name | New name | Mattermost | Where it appears | Note |
|---|---|---|---|---|
| `BRIDGE_BUS_PREFIX` | `CB_BRIDGE_BUS_PREFIX` | no | `internal/config/config.go` | |
| `BRIDGE_CA_FILE` | `CB_BRIDGE_CA_FILE` | no | `internal/config/config.go`, `cmd/bridged/main.go` | |
| `BRIDGE_DELIVER` | `CB_BRIDGE_DELIVER` | no | `internal/config/config.go` | |
| `BRIDGE_REQ_PREFIX` | `CB_BRIDGE_REQ_PREFIX` | no | `internal/config/config.go` | the request subjects' prefix (ADR-014 phase 5), default `req` |
| `BRIDGE_HOMES` | `CB_BRIDGE_HOMES` | no | `internal/config/config.go` | the homes' names on both sides |
| `BRIDGE_INBOX` | `CB_BRIDGE_INBOX` | no | `internal/config/config.go` | |
| `BRIDGE_KV_BUCKET` | `CB_BRIDGE_KV_BUCKET` | no | `internal/config/config.go` | |
| `BRIDGE_LISTEN_ADDR` | `CB_BRIDGE_LISTEN_ADDR` | no | `internal/config/config.go` | |
| `BRIDGE_MM_ALLOW_HTTP` | `CB_BRIDGE_MM_ALLOW_HTTP` | yes | `internal/config/config.go`, `cmd/bridged/main.go`, `internal/mmws/mmws.go`, `internal/registrar/mattermost/adapter.go` | |
| `BRIDGE_MM_TEAM` | `CB_BRIDGE_MM_TEAM` | yes | `internal/config/config.go` | |
| `BRIDGE_MM_TOKEN_FILE` | `CB_BRIDGE_MM_TOKEN_FILE` | yes | `internal/config/config.go` | |
| `BRIDGE_MM_URL` | `CB_BRIDGE_MM_URL` | yes | `internal/config/config.go`, `cmd/bridged/main.go` | |
| `BRIDGE_NATS_SEED_FILE` | `CB_BRIDGE_NATS_SEED_FILE` | no | `internal/config/config.go`, `cmd/bridged/main.go` | |
| `BRIDGE_NATS_URL` | `CB_BRIDGE_NATS_URL` | no | `internal/config/config.go` | |
| `BRIDGE_PROPS_PREFIX` | `CB_BRIDGE_PROPS_PREFIX` | no | `internal/config/config.go` | must equal the homes' `CB_PROPS_PREFIX` |
| `BRIDGE_SEEN_BUCKET` | `CB_BRIDGE_SEEN_BUCKET` | no | `internal/config/config.go` | |
| `BRIDGE_STREAM` | `CB_BRIDGE_STREAM` | no | `internal/config/config.go` | |
| `BRIDGE_MM_ADMIN_TOKEN_FILE` | `CB_BRIDGE_MM_ADMIN_TOKEN_FILE` | yes | `internal/config/config.go` | new with #166 |
| `BRIDGE_BOT_PREFIX` | `CB_BRIDGE_MM_BOT_PREFIX` | yes | `internal/config/config.go` | new with #166; the Mattermost bots' username prefix (at least two characters, #274) |
| `BRIDGE_HIDE_BOT_DM` | `CB_BRIDGE_MM_HIDE_BOT_DM` | yes | `internal/config/config.go` | new with #166; a Mattermost direct message |
| `CB_BRIDGE_MM_BOT_KEEP` | `CB_BRIDGE_MM_BOT_KEEP` | yes | `internal/config/config.go` | new with #151 step 2: no old spelling; the sweep's keep time for the agents' bots |
| `CB_BRIDGE_COMMANDS_ALLOW_HTTP` | `CB_BRIDGE_COMMANDS_ALLOW_HTTP` | no | `internal/config/config.go` | plain http to a listener that is not loopback; default false |
| `CB_BRIDGE_COMMANDS_LISTEN_ADDR` | `CB_BRIDGE_COMMANDS_LISTEN_ADDR` | no | `internal/config/config.go`, `cmd/bridged/main.go` | empty: the whole feature is off |
| `CB_BRIDGE_COMMANDS_TLS_CERT_FILE` | `CB_BRIDGE_COMMANDS_TLS_CERT_FILE` | no | `internal/config/config.go`, `cmd/bridged/main.go` | with the key file, or neither |
| `CB_BRIDGE_COMMANDS_TLS_KEY_FILE` | `CB_BRIDGE_COMMANDS_TLS_KEY_FILE` | no | same | with the cert file, or neither |
| `CB_BRIDGE_DEBUG_COMMAND_TOKEN_FILE` | `CB_BRIDGE_DEBUG_COMMAND_TOKEN_FILE` | yes | `internal/config/config.go` | the debug command's tokens, 0600 |
| `CB_BRIDGE_MANDATE_COMMAND_TOKEN_FILE` | `CB_BRIDGE_MANDATE_COMMAND_TOKEN_FILE` | yes | same | the mandate command's tokens, 0600 |
| `CB_BRIDGE_MODE_COMMAND_TOKEN_FILE` | `CB_BRIDGE_MODE_COMMAND_TOKEN_FILE` | yes | same | the mode command's tokens, 0600 |
| `CB_BRIDGE_RECORDS_BUCKET` | `CB_BRIDGE_RECORDS_BUCKET` | no | `internal/config/config.go` | default `BUS_BRIDGE_RECORDS` |
| `CB_BRIDGE_SIGNING_KEY_FILE` | `CB_BRIDGE_SIGNING_KEY_FILE` | no | `internal/config/config.go`, `cmd/bridged/main.go` | base64 or base64url of a 32-byte seed, 0600 |
| `CB_BRIDGE_TRUSTED_PROXIES` | `CB_BRIDGE_TRUSTED_PROXIES` | no | `internal/config/config.go`, `internal/commands` | IPs or CIDRs, comma-separated; only their `X-Forwarded-For` counts for the read buckets, empty: the peer's address |
| `CB_BRIDGE_RELEASE_SIGNERS_FILE` | `CB_BRIDGE_RELEASE_SIGNERS_FILE` | no | `internal/config/config.go`, `cmd/bridged/main.go` | the release record's signers in authorized_keys form; unset: the registrar's exact 404 |
| `CB_BRIDGE_RELEASE_VERSION` | `CB_BRIDGE_RELEASE_VERSION` | no | same | `v<x.y.z>`, `latest`, or empty (keys only) |

`BRIDGE_BOT_PREFIX` and `BRIDGE_HIDE_BOT_DM` (#166) are about Mattermost but
have no `MM` in their names; the rename gives them one, as the rule says:
`CB_BRIDGE_MM_BOT_PREFIX` and `CB_BRIDGE_MM_HIDE_BOT_DM`.

### The console (20)
| Old name | New name | Mattermost | Where it appears | Note |
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
| `CONSOLE_METRICS_LISTEN_ADDR` | `CB_CONSOLE_METRICS_LISTEN_ADDR` | no | `console/server/src/console_server/config.py` | the unattended metrics page's listener (#158, RT-6.43 step 10) |
| `CONSOLE_METRICS_NKEY_SEED_FILE` | `CB_CONSOLE_METRICS_NKEY_SEED_FILE` | no | same; `services/auth/nats-server.example.conf` (a comment) | pairs with `CB_CONSOLE_METRICS_TOKEN_FILE`; refused when open to group or world (`defaultMode: 0400` and the console's uid under Kubernetes) |
| `CONSOLE_METRICS_TOKEN_FILE` | `CB_CONSOLE_METRICS_TOKEN_FILE` | no | same | pairs with the seed file; both or neither |
| `CONSOLE_PRESENCE_SUBJECT` | `CB_CONSOLE_PRESENCE_SUBJECT` | no | same | |
| `CONSOLE_PUBLIC_URL` | `CB_CONSOLE_PUBLIC_URL` | no | same | |
| `CONSOLE_SESSION_SEC` | `CB_CONSOLE_SESSION_SEC` | no | same | |
| `CONSOLE_STATS_STREAM` | `CB_CONSOLE_STATS_STREAM` | no | same | |
| `CONSOLE_STATS_SUBJECT` | `CB_CONSOLE_STATS_SUBJECT` | no | same | |
| `CONSOLE_TOKEN_ALGS` | `CB_CONSOLE_TOKEN_ALGS` | no | same | |
| `CONSOLE_WEB_DIR` | `CB_CONSOLE_WEB_DIR` | no | same, `app.py`; `console/server/Dockerfile` | |
| `VITE_CONSOLE_API` | `VITE_CB_CONSOLE_API` | no | `console/web/src/main.tsx`, `console/web/src/api/http.ts`, `console/web/vite.config.ts`; `console/server/Dockerfile` | build time only; Vite passes only `VITE_` names to the page, so `VITE_` stays in front; the new name first, else the old one (no warning: a build has no process to warn in); two differing values refuse the build |
### The OpenCode plugin (6)

Read in `opencode/plugin/mattermost-agent-bus.ts`, or handed by it to the MCP
server it starts.

| Old name | New name | Mattermost | Where it appears | Note |
|---|---|---|---|---|
| `MM_AGENT_BUS_ROOT` | `CB_AGENT_BUS_ROOT` | no | `opencode/plugin/mattermost-agent-bus.ts` | the bus checkout; also in the Claude Code and Cursor adapter docs |
| `MM_BUS_ENDPOINT` | `CB_BUS_ENDPOINT` | no | same | internal |
| `MM_BUS_KEY` | `CB_BUS_KEY` | no | same | internal |
| `MM_BUS_MCP_ENDPOINT` | `CB_BUS_MCP_ENDPOINT` | no | same | internal |
| `MM_CHANNEL_PUSH` | `CB_CHANNEL_PUSH` | no | same | handed to the server |
| `MM_WORKSPACE` | `CB_WORKSPACE` | no | same | handed to the server |

### The pi extension (6)

Read in `pi/mattermost-agent-bus.ts`, or handed by it to the MCP server it
starts.

| Old name | New name | Mattermost | Where it appears | Note |
|---|---|---|---|---|
| `MM_AGENT_BUS_ROOT` | `CB_AGENT_BUS_ROOT` | no | `pi/mattermost-agent-bus.ts` | |
| `MM_BUS_ENDPOINT` | `CB_BUS_ENDPOINT` | no | same | internal |
| `MM_BUS_KEY` | `CB_BUS_KEY` | no | same | internal |
| `MM_BUS_MCP_ENDPOINT` | `CB_BUS_MCP_ENDPOINT` | no | same | internal |
| `MM_CHANNEL_PUSH` | `CB_CHANNEL_PUSH` | no | same | handed to the server |
| `MM_WORKSPACE` | `CB_WORKSPACE` | no | same | handed to the server |

### Tests and CI (6)

The test suites read these; the workflows in `.github/workflows/` set them.
They need no fallback: the tests and the workflows change in one pull request.

| Old name | New name | Mattermost | Where it appears | Note |
|---|---|---|---|---|
| `MM_NATS_SERVER` | `CB_NATS_SERVER` | no | `mcp/tests/test_natsbus.py`, `mcp/tests/test_nats_port.py`, `console/server/tests/test_nats_live.py` | set in `checks.yml` |
| `MM_TEST_NATS` | `CB_TEST_NATS` | no | the same tests and `mcp/tests/conftest.py` | set in `checks.yml` |
| `MM_TEST_KEYCLOAK` | `CB_TEST_KEYCLOAK` | no | `mcp/tests/test_login_keycloak.py` | set in `keycloak.yml` |
| `MM_TEST_FD_TOKEN` | `CB_MM_TEST_FD_TOKEN` | yes | `tests/hooks/conftest.py` | a test's own hand-off of the bot token |
| `BUS_TESTRIG_PYTHON` | `CB_TESTRIG_PYTHON` | no | `testrig/src/bus_testrig/bus.py` | |
| `BUS_TESTRIG_REQUIRE_BUS` | `CB_TESTRIG_REQUIRE_BUS` | no | `testrig/tests/conftest.py`, `testrig/tests/test_bus_process.py` | set in `testrig.yml` |

### Goes with the registrar (10)

The owner decided on 2026-11-26 that the registrar goes soon
([#151](https://github.com/dembaca/ai-agent-bus/issues/151), steps 3 to 5),
so its settings are **not renamed**: they keep their old names until they go
with it. They need no fallback, no refusal and no new spelling in the
[lists of names](#lists-of-names); their entries there stay as they are.

| Old name | Where it appears | Why it goes with the registrar |
|---|---|---|
| `MM_REG_SECRET` | `attest.py`, `client.py`, `hosted.py`, `profile.py`, `registration.py`, `server.py`; `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh`, `bin/mm-agent-sweep.sh`, `bin/mm-agent-unregister.sh`, `opencode/plugin/mattermost-agent-bus.ts`, `pi/mattermost-agent-bus.ts` | the enrollment secret a bot is registered with |
| `MM_REG_SECRET_GOPASS` | `hosted.py`; `adapters/corral/10-mm-session-start.sh`, `pi/mattermost-agent-bus.ts` | where the secret is read from |
| `MM_REG_SECRET_CMD` | `pi/mattermost-agent-bus.ts` | the same, as a command (pi only) |
| `MM_REG_SECRET_FD` | `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh` | hands the secret to a helper on a file descriptor |
| `MM_REGISTER` | `bin/mm-agent-lib.sh` | `always` registers a bot in every session |
| `MM_REGISTER_URL` | `client.py`, `compose.py`, `release.py`, `selfupdate.py`, `server.py`; `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh`, `bin/mm-agent-registrar-keys.sh` | the registrar's URL; it also serves the release record |
| `MM_REG_BOT_NAME` | `client.py`, `hosted.py`, `server.py`, `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh`, `bin/mm-agent-sweep.sh`, `bin/mm-agent-unregister.sh` | the registrar's short name of the bot; names the session file and unregisters the bot |
| `MM_AGENT_PREFIX` | `client.py`, `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh`, `bin/mm-agent-sweep.sh` | the username prefix of the registrar's bots, set from its answer (`client.py`); the bridge's bots have their own, `BRIDGE_BOT_PREFIX` |
| `MM_BOT_ATTESTATION` | `attest.py`, `hosted.py`, `session_file.py`; `adapters/corral/10-mm-session-start.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-register.sh` | the registrar's signed record of the bot |
| `MM_REGISTRAR_KEYS` | `askroute.py`, `attest.py`, `client.py`, `debug.py`, `mandate.py`, `owners.py`, `release.py`, `selfupdate.py`; `adapters/corral/90-mm-session-stop.sh`, `bin/mm-agent-host-install.sh`, `bin/mm-agent-lib.sh`, `bin/mm-agent-registrar-keys.sh` (prints an `export` line of it), `bin/mm-agent-release-check.sh` | the pinned registrar keys; they also check the registrar's release record |

`MM_REGISTRAR_KEYS` pins more than the registrar's bots: it also checks the
release record the registrar serves (the update notice and its `Needs:` and
`Withdrawn:` lines). That record moves into the bridge
([#151](https://github.com/dembaca/ai-agent-bus/issues/151), step 4), and its
pinned keys come with it under a new `CB_` name of their own, not through
this rename. Until then `MM_REGISTRAR_KEYS` keeps working under its name.

`MM_REGISTER_URL` likewise names more than the registrar: it is also where
hosts read the signed records and the release record. The hosts' setting for
that address is `CB_RECORDS_URL` (decided 2026-11-27 by the owner, in phase 5
of the NATS decision as plan B,
[ADR-014](arc42/09-architecture-decisions.md)); it is built for the release
record with plan B piece 14b (unset: off, the host's release check as before),
and the other signed records follow. Until then `MM_REGISTER_URL` keeps
working under its name. `MM_REG_SECRET`, `MM_REG_SECRET_GOPASS` and
`MM_REG_SECRET_CMD` get no new name: hosts delete them, and the stored value
they point to, two weeks after the registrar's shutdown (the same decision).

### Excluded names

Not renamed, because they are not our environment variables. Each name with
its reason; `scripts/doc-check.py` reads this table as the guard's exclusion
list, so a name here is one the guard accepts in the code without a mapping.

| Name | Why it is not renamed |
|---|---|
| `HOME` | another product’s variable we read |
| `CONSOLE_METRICS_PUBLIC_NKEY` | the NATS example config's `<…>` placeholder: the nkey is set at the server, never in the env (#168) |
| `PATH` | another product’s variable we read |
| `TMPDIR` | another product’s variable we read |
| `TZ` | another product’s variable we read |
| `XDG_STATE_HOME` | another product’s variable we read |
| `DISPLAY` | another product’s variable we read |
| `WAYLAND_DISPLAY` | another product’s variable we read |
| `SSL_CERT_FILE` | another product’s variable we read |
| `SSL_CERT_DIR` | another product’s variable we read |
| `HTTPS_PROXY` | another product’s variable we read |
| `NO_PROXY` | another product’s variable we read |
| `CLAUDECODE` | another product’s variable we read |
| `CLAUDE_CODE_SESSION_ID` | another product’s variable we read |
| `CLAUDE_PLUGIN_ROOT` | another product’s variable we read |
| `CLAUDE_PROJECT_DIR` | another product’s variable we read |
| `CLAUDE_SESSION_ID` | another product’s variable we read |
| `CORRAL_AGENT` | another product’s variable we read |
| `CORRAL_AGENT_EXIT` | another product’s variable we read |
| `CORRAL_SANDBOX` | another product’s variable we read |
| `CORRAL_SESSION_ID` | another product’s variable we read |
| `CORRAL_WORKDIR` | another product’s variable we read |
| `OPENCODE_SESSION_ID` | another product’s variable we read |
| `GITHUB_ACTIONS` | another product’s variable we read |
| `GITHUB_ENV` | another product’s variable we read |
| `BUS_BRIDGE` | a NATS bucket name, a constant |
| `BUS_BRIDGE_SEEN` | a NATS bucket name, a constant |
| `BUS_BRIDGE_RECORDS` | a NATS bucket name, a constant |
| `BUS_DIRECTORY` | a NATS bucket name, a constant |
| `BRIDGE_KEYS` | the Python constant `_BRIDGE_KEYS` |
| `BRIDGE_TTL_SEC` | a constant in `nats_port.py` |
| `BRIDGE_USER_PUBLIC_NKEY` | a placeholder in `nats-server.example.conf` |
| `CONSOLE_FAMILIES` | a test constant |
| `CONSOLE_NATS` | a test constant |
| `MM_A` | a test placeholder |
| `MM_B` | a test placeholder |
| `MM_BAR` | a test placeholder |
| `MM_FOO` | a test placeholder |
| `MM_X` | a test placeholder |
| `MM_OLD` | a test placeholder |
| `MM_OTHER` | a test placeholder |
| `MM_ID` | a test placeholder |
| `MM_BOT_NAME` | only in `tests/hooks/test_sweep.py`, the key of an old session file format; no code reads it |
| `MM_API` | a name stem in patterns and comments (`MM_API_BODY` and the like) |
| `MM_BUS_HOST` | a name stem in patterns and comments |
| `MM_KEPT` | a name stem in patterns and comments |
| `MM_REL` | a name stem in patterns and comments |
| `MM_RR` | a name stem in patterns and comments |

Script locals in upper case (`STATE_DIR`, `ROOT`, `LISTEN_UNTIL` and so on) are
set and read inside one script; they are covered by
[the shell variables that are not environment](#shell-variables-that-are-not-environment).
## The `Needs:` rule

A CHANGELOG entry may say what its release needs first, in a `Needs:` line.
Every agent's update notice shows it, so its grammar is narrow. Today it
allows only `MM_*` names, versions and short lowercase words. The grammar
lives in four places:

- `CLAUDE.md`, section "Releasing";
- `NEEDS_PATTERN` in `mcp/src/mattermost_ai_agent_bus/release.py`;
- `MM_NEEDS_RE` in `bin/mm-agent-lib.sh`, the same text;
- `scripts/doc-check.py`, which reads the pattern from `release.py`; its
  error text (line 251, "Needs: takes MM_* variables, …") names `MM_*`
  itself and changes with the pattern.

The change: the grammar allows `CB_*` names, and `MM_*` names too while the
fallbacks exist and while the registrar's names keep `MM_`. `MM_*` leaves
the grammar only when neither is left.

A server or host script before 0.103.0 checks the line with the old grammar
and skips a line it does not accept. So the `Needs:` line of 0.103.0 itself
names the old `MM_*` names, which still work through the fallbacks; it must
not name `CB_*` names.

The removal release has the same problem: a host still at 0.102.0 or older
can jump straight to it, and it reads that release's `Needs:` line with the
old grammar. So the removal release's `Needs:` line must not name `CB_*`
names either. It says what it needs with versions and lowercase words only,
for example `Needs: host copy at 0.103.0 or newer, new names set.` The
`CB_` names themselves go into the CHANGELOG entry's text, which every
version shows.

The pull request of the MCP server and the hooks changed all four together:
the grammar takes `CB_*` and `MM_*` names.

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
- **The CHANGELOG of 0.103.0 and of the removal release:** every rename
  under `### Breaking`, each with a `Needs:` line as above (no `CB_` names
  in either).
- **doc-check:** a rule that refuses a new environment variable without the
  `CB_` prefix, as the issue proposes: not built yet. The check that every
  name the code reads is on this page, and every old name it maps is still in
  the code, is built (`scripts/doc-check.py`, [#177](https://github.com/dembaca/ai-agent-bus/pull/177)).
