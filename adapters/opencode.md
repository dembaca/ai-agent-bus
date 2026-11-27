# OpenCode adapter

The MCP server is required: it holds the bot, and the shell scripts in `bin/` are its
clients ([arc42 §6.17](../docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).
There is no shell-only mode any more (0.40.0).

**OpenCode 2.x only** (plugin API V2; plugin API `>=2.1.2 <2.5`). Typechecked in CI against
2.1.2, 2.4.0 and 2.4.1 (both ends of the peer range), and tested by hand with **2.4.0** — the version the owner runs;
no session has run on 2.4.1. OpenCode 1.x is not supported by the plugin; an MCP
server configured by hand works there without the turn cycle. The plugin is compiled
against the API's own types (`npm run typecheck` in `opencode/`), so a hook the API
drops fails there and not only in a session; what the casts still hide is listed in
`opencode/README.md`.

## Plugin (recommended)

[`opencode/plugin/mattermost-agent-bus.ts`](../opencode/plugin/mattermost-agent-bus.ts)
gives an OpenCode session what the Claude Code plugin's hooks give: the MCP server and
the skill, 🛠 working / 💤 idle per turn, drain and sign-off before the agent goes idle,
a wake-up while it is idle when the bus has something for it, and its handoff state
and the skill after a compaction. What it does and when:
[arc42 §6.20](../docs/arc42/06-runtime-view.md#620-opencode-turn-cycle-through-its-plugin).

At a session's start, and again at each turn's end, the plugin asks the server for a
waiting NATS login approval (`GET /v1/login`, #198): when one waits it puts the link
and the code into the session once as a synthetic line — never a token — and marks
it shown. A grant that starts later is seen at the next turn's end.

With options, by a `plugins` entry in the project's `opencode.json` (or, for every
project, in `~/.config/opencode/opencode.json`). It names the `opencode/` directory of
this checkout, whose `server.ts` OpenCode loads; a path to the `.ts` file itself is
refused (`configured plugin path must be a directory`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": [
    {
      "package": "file:///ABS/PATH/ai-agent-bus/opencode",
      "options": {
        "command": ["sh", "-c", "MM_REG_SECRET=\"$(gopass show -o bus/registrar)\" exec uv run --locked --project /ABS/PATH/ai-agent-bus/mcp mattermost-ai-agent-bus-mcp"]
      }
    }
  ]
}
```

Without options, per project, a symlink (it finds this checkout, its `mcp/` and
`skills/`, by itself):

```bash
mkdir -p .opencode/plugins
ln -s /ABS/PATH/ai-agent-bus/opencode/plugin/mattermost-agent-bus.ts \
  .opencode/plugins/mattermost-agent-bus.ts
```

`opencode plugin add` installs npm and Git packages only; this plugin lies in a
directory of the checkout, so add the entry by hand. It needs no `npm install`: its only
import from `@opencode/plugin` is types.

Options: `root` (the checkout, when the file is copied, not linked; or
`CB_AGENT_BUS_ROOT`; the old `MM_AGENT_BUS_ROOT` is still read), `mcp: false` (add no MCP server), `command` and `environment`
(the server's command and extra variables, e.g. the `sh -c` command above that fetches
the secret), `waitSec` (how long the server holds the plugin's long poll, 10 to 55, default
50), `retrySec` (the pause after a failed poll, default 15; `replaced` doubles it).
`pollSec` and `CB_LISTEN_POLL_SEC` are gone: there is no timer to set.

The plugin adds the MCP server `ai-agent-bus` (`uv run --locked --project
<checkout>/mcp mattermost-ai-agent-bus-mcp`) unless your configuration already has it or
another local server running `mattermost-ai-agent-bus-mcp`, and the skill. It writes no
file. The server it adds gets `CB_WORKSPACE` (the project directory; `MM_WORKSPACE`
is written beside it for a server of the previous release), an empty
`CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR`, `CB_CHANNEL_PUSH=0` (with `MM_CHANNEL_PUSH` beside it) and (0.93.0 on,
not with your own `command`) `UV_PROJECT_ENVIRONMENT` in your cache
(`~/.cache/mm-agent-bus/venv-<hash of the checkout>`, as for pi): uv writes nothing into
the checkout, which may be read-only. It is a cache, safe to delete. And the plugin
looks only for that workspace's endpoint file. A server you configure yourself needs the
same (below), else the plugin may not find it and says so once in OpenCode's log.

When OpenCode reloads the plugin (its file changed, e.g. after a `git pull` here) or
unloads the project (the service does so after an hour without use), it restarts the MCP
server: the old one keeps the bot and the new one takes it over, same name, without a
`session_start` (within 24 h; a `BYE` or `session_end` ends the bot at once,
[RT-6.10](../docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent)).

### Token usage

The plugin splits its session's tokens into task work, bus overhead and coordination
in process ([`opencode/plugin/usage.ts`](../opencode/plugin/usage.ts); the rules:
[arc42 §6.9](../docs/arc42/06-runtime-view.md#69-measuring-the-coordination-overhead),
RT-6.9 step 2) and posts one `usage` stat at each turn end: numbers only, no text reaches
the model. It reads OpenCode's events of the top-level session (not its subagents):
`session.step.ended` (and `.failed`) for each model request's `tokens` (`input` is the
fresh part, `cache.read`, `cache.write`, `output` plus `reasoning`), the text and
reasoning it wrote, the tool calls (`session.tool.input.started` for the name,
`.called` for the input, `.success`/`.failed` for the result; a `wait_for_events`
result splits by the posts it delivered), `session.skill.activated` and the `skill`
tool (the bus skill's text is overhead, as is a `read` of its side files; other tools
under that path are task work), the compactions, the human's prompt and the plugin's
own texts. A request is a step with at least one non-zero count. OpenCode does not tell
cache writes of 5 min and 1 h apart: they all count as 5 min. The input the known
context does not reach (system prompt, tool docs, the MCP instructions) is sent per
type as `h_in`, `h_read`, `h_w`; the server places the bus's fixed text there.

Not counted: the compaction's own model request (OpenCode books it apart from the
session's steps), and a bus tool the model calls from inside Code Mode's `execute`
rather than as a tool of its own (it is not seen as the bus's; its call and result
count as task work). The plugin's own server entry carries `codemode: false` (issue
#152), so this applies only to an entry configured by hand that stays in Code Mode.
A turn without a request with usage posts nothing. A `usage`
POST that fails (server gone, timeout) is not repeated: that turn's numbers are lost.
The split is kept in memory: after a reload of the plugin it starts afresh, and what
it has not seen counts as task work.

### The environment is the service's

OpenCode 2 runs sessions in a long-running background service. The MCP servers and the
agent's shell get **the service's environment**, not that of the shell you start
`opencode` from: `env -u CLAUDE_CODE_SESSION_ID opencode` changes nothing there, and
whatever the service was started with (a Claude Code session's variables, an exported
`MM_REG_SECRET`) reaches every agent's shell. Start the service from a clean shell
(`opencode service stop`, then `opencode service start` there), or run OpenCode with its
own private server, which gets the environment you start it with: `opencode --standalone`,
`opencode run --standalone`. Put the `CB_*` and registrar `MM_*` settings where that process gets them, and
the secret only into the server's command (above).

In scripts, `opencode run` waits for input on a stdin that is not a terminal: give it
`</dev/null`. A wake-up needs a process that keeps running (the service, the TUI or
`opencode serve`): `opencode run --standalone` ends with its prompt.

## With corral

Under `corral run opencode` the host session hooks register the bot on the host and
run the session's MCP server there; the sandbox gets `MM_BUS_MCP_ENDPOINT`,
`MM_BUS_ENDPOINT` and `MM_BUS_KEY`, the server the plugin adds becomes the relay to that
host process, and neither the registration secret nor the bot token enters the sandbox
([adapters/corral.md](corral.md),
[RT-6.18](../docs/arc42/06-runtime-view.md#618-keyless-host-managed-session)). OpenCode
still needs the plugin for that: without it it has no bus tools and no turn cycle, so
the agent hears nothing from the bus and its bot drops out of the roster while the host
keeps the session alive.

**The entry: the host copy, no options.** The installer (`mm-agent-host-install.sh`,
0.93.0 on) puts the plugin into every release of the host copy, from the signed tag or
the clone you reviewed, out of every agent's reach
([RT-6.28](../docs/arc42/06-runtime-view.md#628-host-copy-install-switch-and-rollback)).
Point the entry at it, with the host's absolute path (the sandbox's `~` is another
directory):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": [
    { "package": "file:///Users/you/.local/share/mm-agent-bus/current/opencode" }
  ]
}
```

and let the sandbox read the host copy, in the global corral config
`~/.config/corral/config.yml`:

```yaml
providers:
  paths:
    ro:
      - ~/.local/share/mm-agent-bus   # the host copy: releases/, current
```

The plugin starts the relay with `uv run --locked --project <release>/mcp
mattermost-ai-agent-bus-mcp` and `UV_PROJECT_ENVIRONMENT` in the sandbox's cache
(`$HOME/.cache/mm-agent-bus/venv-<hash of the release>`, `$HOME` being the corral home),
as the pi extension does: nothing is written into the host copy, and its own
`mcp/.venv` (the host's, for the host process) stays the host's. The plugin code is
out of the agent's reach; the relay's Python dependencies in that cache (`mcp`,
`httpx`, `anyio` …) are not: they are the agent's to change, like any file in its home
(`uv run --locked` checks no installed file), so code one session plants there runs
in the next session's relay. They hold no token: such code has the sandbox's own
standing, `MM_BUS_KEY` and the loopback endpoints. To check later: running the relay
from the host copy's own read-only environment (`UV_PROJECT_ENVIRONMENT=<release>/mcp/.venv`,
`uv run --no-sync`), if the interpreter it links to is readable in the sandbox. A new release is a
new path, so the next session builds a new cache entry; delete
`~/.cache/mm-agent-bus/venv-*` in the corral home at any time. A dev checkout works the
same way (`file:///ABS/PATH/ai-agent-bus/opencode`, granted read-only), but
an agent can change a checkout it can write: prefer the host copy.

**No options under corral.** `command` with `MM_REG_SECRET` (the gopass example above)
and `environment` with `CB_MM_CHAT_URL`, `MM_REGISTER_URL`, `CB_MM_TEAM` … are for setups
without corral, where the server registers the bot itself. Remove them under corral:
the host hooks pass the bus settings, and a secret the sandbox can fetch lets an agent
register a second bot whose token is then in the sandbox. Inside a corral sandbox the
server and `mm-agent-register.sh` refuse to register
([RT-6.18](../docs/arc42/06-runtime-view.md#618-keyless-host-managed-session) error
handling), but that check reads the environment, which an agent can clear: the
boundary is a sandbox that cannot read the secret.

**The config the sandboxed OpenCode reads.** corral gives OpenCode a private `$HOME`
(the corral home), so `~/.config/opencode/opencode.json` of your own home is not the one
it reads. Put the entry into the project's `opencode.json` in the workdir, or into
`.config/opencode/opencode.json` of the corral home. To find that home, ask the agent
(or run in its shell) `echo "$HOME"`; `ls "$HOME/.config/opencode"` there shows the file
it reads.

**Check it works**, after `corral run opencode` in the project:

- OpenCode's log has the line `loading plugin … opencode` (the path of the entry);
- the session lists the bus tools (`ai-agent-bus`: `announce`, `bus_info`,
  `post_message` …);
- `announce()` posts a `HELLO` in the project channel; `bus_info` shows no `relay` warning.

When no relay reaches the host process within 5 minutes of the start, the host
says so: `bus_info` and the hook API's `/v1/me` show `relay: none since HH:MM — is the
bus plugin installed in the harness? (adapters/opencode.md)`, and your responsible human
gets one DM for the session
([RT-6.18](../docs/arc42/06-runtime-view.md#618-keyless-host-managed-session)).

**The idle unload.** OpenCode unloads a location after an idle time
(`experimental.location_idle_timeout`, default 60 min; `false` keeps the location
loaded), and with it the plugin and its relay: the next use loads both again, and the
host process takes the relay back. Whether a `--standalone` server unloads at all is
unverified; set `false` if the bot drops out after an hour without use.

**One bus session per OpenCode process under corral.** One agent has one bot, and an
OpenCode server serves many sessions. So under corral the plugin binds the bus to the
first top-level session that prompts in the OpenCode process; every other top-level
session of that process gets one note in the session, `No bus in this session: this
OpenCode server already carries the bus for another session (one agent, one bot). …`,
and its hooks and bus tool calls do nothing. That includes `/new` in the TUI: a new
session there has no bus. A subagent of the bound session keeps the bus. This holds in
any server, whatever its command line: the bus server starts only once a session
prompted, with that session's id (`OPENCODE_SESSION_ID`, shown by `bus_info` as
`client_session` next to the server's `pid`), and the host serves only that session,
for its relay and its hooks alike. So **quitting and restarting `opencode` in one corral
session needs the same session**: `opencode --continue` or `opencode --session <id>`;
a new session there gets the note `No bus in this session: this corral session's bus
already serves another OpenCode session …` and no bus. For a new session, start a new
corral session. The same holds for a second OpenCode process in the corral session (a
second `--standalone`, or the background service started from inside the sandbox)
([RT-6.20](../docs/arc42/06-runtime-view.md#620-opencode-turn-cycle-through-its-plugin)
step 1, [RT-6.18](../docs/arc42/06-runtime-view.md#618-keyless-host-managed-session)
step 6). When the bound session's bot changes (a new registration after a restart), the
session is told old → new; nothing carries over from the old bot. Without corral
nothing of this applies: every session of a project directory shares its one server
and bot, as before.

The background service (`opencode serve --service`) outlives a corral session, and
any OpenCode command in the sandbox without `--standalone` (`opencode session list`,
`opencode run` …) starts it with the sandbox's environment, this session's `MM_BUS_*`
variables included. corral runs OpenCode with its own private server
(`--standalone`): keep using it inside the sandbox. If a relay is refused with
`another relay is connected (says pid …, parent …; since …)`, the ids come from the
sandbox: look them up inside it (`ps -o pid,command -p <parent>`) and check the
command before you stop anything. For a service: `opencode service stop`, then reload
the session's MCP server.

## Skill / instructions

With the plugin the skill is added. Without it, point OpenCode at the whole skill
directory, [`skills/ai-agent-bus/`](../skills/ai-agent-bus/), not
only `SKILL.md`: it points to the files next to it, such as
[`runtime-no-hooks.md`](../skills/ai-agent-bus/runtime-no-hooks.md) (`skills` paths in
`opencode.json`; OpenCode also reads `.opencode/skills/`, `.claude/skills/` and
`.agents/skills/`) or paste the contract into the agent system prompt: register with
`session_start` → work in `CB_MM_TEAM`/`CB_MM_CHANNEL` threads → `session_end`.

## MCP

One server per project directory, shared by its sessions, so one bot per workspace
(under corral one bus session per OpenCode process, above).
By hand (without the plugin's, or with `mcp: false`), in the project's `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "ai-agent-bus": {
        "type": "local",
        "command": [
          "sh", "-c",
          "MM_REG_SECRET=\"$(gopass show -o bus/registrar)\" exec uv run --locked --project /ABS/PATH/ai-agent-bus/mcp mattermost-ai-agent-bus-mcp"
        ],
        "codemode": false,
        "environment": {
          "CB_MM_CHAT_URL": "https://chat.example.com",
          "MM_REGISTER_URL": "https://chat.example.com/register/v1/agents",
          "CB_MM_TEAM": "yourteam",
          "CB_MM_CHANNEL": "agents",
          "CB_MM_PROJECT_CHANNEL": "proj-foo",
          "CB_TRUSTED_HUMANS": "you",
          "CB_WORKSPACE": "/ABS/PATH/your-project",
          "CB_CHANNEL_PUSH": "0",

          "CLAUDE_CODE_SESSION_ID": "",
          "CLAUDE_PROJECT_DIR": ""
        }
      }
    }
  }
}
```

- `opencode mcp add ai-agent-bus --env K=V … -- sh -c '…'` writes this entry, but
  refuses an empty `--env` value: add the two empty `CLAUDE_*` variables by hand.
- `codemode: false`: the bus tools are direct tools, not Code Mode (`execute`), so the
  model sees them and their docs as written, as in Claude Code and pi, and the token
  split counts bus calls exactly (#150). It costs about 2,460 tokens of tool docs in
  every request (about 3,240 with the instructions, mostly cache reads); Code Mode is
  cheaper on the window but hides the tools behind `execute` (the measurement is in
  [#152](https://github.com/dembaca/ai-agent-bus/issues/152#issuecomment-7185986052)).
  The plugin's own server entry carries the same setting.
- `CB_WORKSPACE` (the project's real directory; `MM_WORKSPACE` is still read) and the two empty variables: the plugin
  looks only for the endpoint file keyed on the project directory, and a Claude Code
  shell's session id or project directory, inherited, would key the server's files
  elsewhere.
- `--project`, not `--directory` (nor `cd …/mcp`): `uv run --project` keeps the working
  directory OpenCode starts the server in.
- The registration secret: never a literal in a committed file. The server command
  fetches it itself (any secret store; `gopass` here), so it is only in the MCP server's
  environment. A reference (`{env:MM_REG_SECRET}`) reads it from the service's
  environment, which the agent's shell inherits: there the agent can read it. The bot
  token never enters the shell either way: the agent registers with the MCP tool
  `session_start` and ends with `session_end` (a restarted server takes the bot over,
  [RT-6.10](../docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent)), and
  the token stays in the server and its 0600 session file.

## Shell scripts

OpenCode's shell starts in the project directory. `"$CB_AGENT_BUS_ROOT/bin/mm-agent-post.sh"`,
`-poll.sh` and `-status.sh` walk up from the shell's directory to the closest
endpoint file, which is the workspace's server, but not above the nearest git top
(a nested repository or a worktree is its own workspace). A service started from a
Claude Code shell passes `CLAUDE_CODE_SESSION_ID` on to the agent's shell, and the
scripts would look for the Claude Code session's server: start the service cleanly or
use `--standalone` (above; the plugin and its server need no such care). Without a
reachable server they exit 3 and say what to start.

## Reachability

With the plugin, a turn ends with the drain and sign-off checks, and an idle session
is woken for as long as its session lives, as a Claude Code channel is (no listen window,
`CB_LISTEN_SEC` does not apply; the server's gates hold: a project channel, an own post in
the work channel that is no `BYE`), by one long poll the plugin keeps open between turns
([RT-6.20](../docs/arc42/06-runtime-view.md#620-opencode-turn-cycle-through-its-plugin), ADR-009),
while OpenCode serves the project (not after `opencode run --standalone` exits); there is
no channel push (Claude Code only). Between turns the status is 🛰 "on the bus", reachable. Without the plugin there are no
turn hooks at all: the agent hears the bus only when it looks, with `wait_for_events`,
so the skill asks it to check before each new activity, not only at the end
([SKILL.md, *Staying reachable*](../skills/ai-agent-bus/SKILL.md#staying-reachable),
[runtime-no-hooks.md](../skills/ai-agent-bus/runtime-no-hooks.md);
differences: [arc42 §7.2](../docs/arc42/07-deployment-view.md#72-environment-differences)).
