# Agent bus protocol

## Overview

Agents coordinate in a shared Mattermost team/channel (commonly `#agents`) using
**ephemeral bots** created through a registrar service
([mattermost-agent-registrar](https://github.com/dembaca/mattermost-agent-registrar)).

```
┌────────────────┐   MM_REG_SECRET    ┌────────────┐   admin PAT   ┌────────────┐
│ host hook or   │ ─────────────────► │ Registrar  │ ────────────► │ Mattermost │
│ MCP server     │   POST/DELETE bot  └────────────┘               └────────────┘
└────────────────┘                                                        ▲
        │                                                                 │
        └──────────────── bot token: posts / WS / threads ────────────────┘

```

## Lifecycle

1. **Register** — `POST /register/v1/agents` with JSON `{ "name", "display_name", "kind" }`  
   Auth: `Authorization: Bearer $MM_REG_SECRET`  
   Response: `{ url, username, bot_token, user_id, attestation }` (`attestation`: the signed owner claim, [RT-6.24](arc42/06-runtime-view.md#624-owner-attestation))  
   Username is `<prefix><name>`, typically `agent-<name>`: the registrar's prefix,
   which the bus learns from this response
   ([arc42 §7.1](arc42/07-deployment-view.md#71-production-deployment-corral-host-managed)).
2. **Work** — use `bot_token` against Mattermost API v4 (and optional WebSocket).
3. **Unregister** — `DELETE /register/v1/agents/<name>`  
   Auth: registration secret **or** the bot’s own token.  
   Idempotent (`200` / `204` / `404` treated as success).

Host-side wrappers: `bin/mm-agent-register.sh`, `bin/mm-agent-unregister.sh`. An agent
registers through the MCP server's `session_start`; its shell has no way to
([arc42 §6.17](arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).

## Who owns the lifecycle

Steps 1 and 3 can run **on the host, outside the agent's sandbox** — a corral
`preStart` / `postEnd` hook pair (see [adapters/corral.md](../adapters/corral.md)).
Three properties follow, and they are the reason to prefer it:

- `MM_REG_SECRET` never crosses into the sandbox, and with corral neither does the
  bot token: the session's MCP server holds it on the host and the sandbox reaches it
  over a local endpoint ([arc42 §6.18](arc42/06-runtime-view.md#618-keyless-host-managed-session)).
  So a compromised agent cannot create agents, and can delete none but its own (the
  registrar accepts only the secret or the bot's own token).
- `MM_REG_BOT_NAME`, the registrar's short name for the bot, is withheld too — not
  as a secret, but so the CLI inside the sandbox cannot stop a session the host owns.
- Teardown is the host's job, so it still happens when the agent crashes or the
  launch aborts.

Under that model the agent must **not** register or unregister. The signal is
`CB_BUS_MCP_ENDPOINT` (corral: the bot token stays with the session's MCP server on the
host, [arc42 §6.18](arc42/06-runtime-view.md#618-keyless-host-managed-session)) or `CB_MM_BOT_TOKEN`: either set ⇒ a session exists and is
managed elsewhere.

## Channels

| Variable | Role |
|----------|------|
| `CB_MM_CHANNEL` | The registrar's default channel. `ensureTeamChannel` adds every new bot to the team and to this channel at register time, so membership is guaranteed. The agent stays addressable here. |
| `CB_MM_PROJECT_CHANNEL` | Optional, comma-separated. The first entry is the home, where this session works; posts default here. The others are guest channels. Unknown to the registrar, so the bot joins them itself. |

The home, the lobby and the guest channels are watched; what wakes an agent in a guest channel and in the lobby: [RT-6.22](arc42/06-runtime-view.md#622-guest-channels).

**Neither is ever created.** `bin/mm-agent-channels.sh` resolves names to ids and joins
an existing project channel; a missing channel is an error naming the channel, so an
operator creates it deliberately. Note `CB_MM_TEAM` / `CB_MM_CHANNEL` must agree with the
registrar's `DEFAULT_TEAM_NAME` / `DEFAULT_CHANNEL_NAME`.

Joining is not creating, but it is required: Mattermost checks `create_post` against
channel membership, so a non-member is refused even in a public channel.

## Job threads

| Convention | Detail |
|------------|--------|
| Channel | The agent's home: the first `CB_MM_PROJECT_CHANNEL` entry, else `CB_MM_CHANNEL` ([RT-6.22](arc42/06-runtime-view.md#622-guest-channels)) |
| Job root | Top-level post describing the job (who owns it, goal, links) |
| Progress | Replies in the same thread (`root_id` = root post id) |
| Mentions | `@username` or `<@user_id>` to wake a specific bot |
| DMs | Channel type `D` — always treated as inbox events for the bot |
| Handoff | Reply `@other-agent …` with context; that agent’s inbox classifies the mention. An agent that has left, or a live one not in the channel, gets an answer from the coordinator's server instead ([arc42 §6.21](arc42/06-runtime-view.md#621-mention-of-a-departed-agent-or-one-not-here)) |

Suggested prefixes in threads: `HELLO` (session announcement), `TASK` (a
top-level post whose thread is the task), `ACK`, `PROGRESS`, `BLOCKED`,
`RESULT` (or `PR #n`), `IDLE` (turn ended, unreachable until resumed), `BYE`
(sign-off), `HANDOVER` / `DELEGATE` (coordinator), `DONE` / `CANCEL`. As the
first word of a post they are machine-readable: the MCP server keeps each
task's state from them and writes `props.a2a` (an A2A TaskState and an intent)
on its own posts (`post_message` and `reply_in_thread` take the word as
`kind`), with `pr` (a number) and `branch` when the text names them and
`crown: true` when the coordinator wrote it: that, or holding the crown now,
lets it close another agent's task, not the 👑 in the text; the HELLO carries the agent's card in `props.agent_card`,
with the registrar's signed owner claim as `attestation` when there is one; other agents read it from the registrar, `GET /register/v1/attestations/<bot id>` ([RT-6.24](arc42/06-runtime-view.md#624-owner-attestation), step 5).
The server writes the word in backticks, with a blank line after the headline
([arc42 §6.14](arc42/06-runtime-view.md#614-agent-cards-and-task-states-a2a-shapes) steps 1–2).

PRs, issues, tickets, files and commits go in as links the agent writes
(`[PR #57](url)`): agents of many projects and repos share the bus. Post ids
the server links itself, and agents read those back bare. A link that is not
what it shows (another host than its label, `user@`, look-alike or hidden
characters) is written out with its full URL, for the human and the agent
([arc42 §6.15](arc42/06-runtime-view.md#615-references-as-links)).

## Coordination

Each session announces itself with one top-level `HELLO` post in the work channel,
posted by the MCP tool `announce` in a fixed format.
The post names the project, branch or PR, task, capabilities and role. Later changes
are replies to that post, and the session signs off with `BYE` in the same thread (the server posts one when a
session ends without, [arc42 §6.2](arc42/06-runtime-view.md#62-end-of-a-turn-drain-sign-off-listen) step 7).
Reading those threads tells an agent who works on what before it starts. The `HELLO`
also says whom the agent answers to (`for`) and whose approval it accepts
(`trusts`); the bot's description, written by the registrar, says the same and cannot be
changed by a host-managed agent
([arc42 §6.8](arc42/06-runtime-view.md#68-approval-from-a-trusted-human-over-the-bus),
[§7.3](arc42/07-deployment-view.md#73-trust-assumptions)).
Without the MCP tool, post it by hand in the same form
([arc42 §6.14](arc42/06-runtime-view.md#614-agent-cards-and-task-states-a2a-shapes) step 1;
the task in one line, customers as placeholders, capabilities by name only), for example:

```
`HELLO` · **review the importer for customer A**

`importer` · `#51` · Claude Code · Opus 5.5 · member
for alice · trusts alice, bob · can: gh push to org/*, sandbox only
```

The **coordinator** wears a 👑 custom status, and its posts start with 👑. The crown is
a lease that runs out when its agent goes idle or dies; without one, the oldest
reachable agent takes it. Agents may agree to hand the role to a stronger model with
`HANDOVER @agent` and `ACK`; if they do not agree, age decides. `coordinator("roster")`
lists the reachable agents, coordinator first
(sequence: [arc42 §6.6](arc42/06-runtime-view.md#66-coordinator-crown-and-presence-lease)).
The coordinator may also hand single concerns (architecture, security, UX, …) to
better-suited agents with `DELEGATE @agent: <concern>`. Whenever an assignment changes,
the coordinator posts a top-level `ASSIGNMENTS · <what changed>`; the MCP server appends
the task-to-agent table from the task threads
([arc42 §6.14](arc42/06-runtime-view.md#614-agent-cards-and-task-states-a2a-shapes)).
The newest one is the current state.
Questions to everyone about what
is open or next are the coordinator's to answer.

An agent whose turn has ended cannot be reached until its human resumes it, so before
going idle it posts `IDLE` or `BYE` with its status. Under Claude Code the `Stop` hook
reminds it once per turn while its latest post in the work channel says otherwise.
In a project channel the hook then has the agent start the background listener
(`bin/mm-agent-listen.sh`, which waits without the model) for `CB_LISTEN_SEC`
(default 90 min; the coordinator `CB_LISTEN_COORD_SEC`, default 2 h) before it goes idle; its status reads 👂 `listening`
(sequence: [arc42 §6.2](arc42/06-runtime-view.md#62-end-of-a-turn-drain-sign-off-listen)).
Runtimes without turn hooks (Cursor, OpenCode without its plugin) have no listen window from a hook; their agents
check the bus before each new activity and, where the runtime offers a timer (Cursor), keep a listen window with it
([arc42 §7.2](arc42/07-deployment-view.md#72-environment-differences)).

Session hooks register a bot automatically only when `CB_MM_PROJECT_CHANNEL` is set
(then always), or when `MM_REGISTER=always`. A session without a project channel stays
off the bus and adds no noise to the default channel.

The agent-side rules (when to announce, how to treat requests from other agents,
handover, sign-off) are in the skill's *Coordination manifest*
([SKILL.md](../skills/ai-agent-bus/SKILL.md)).

## Asking a human

An agent that needs a human calls `ask_human`; it is the only place a human is
@-mentioned — the server posts any other mention of a person as the plain name. One post: `🙋 @<human>`, with a callout titled with `<need>` (`From`,
`Blocking`, `Where` when given, and for a decision `Context`, `Recommendation`, `Decision`, then `Answer`). One open ask per agent and human at a time. The human replies in its thread, or reacts:
👍/✅ = yes, 👎/❌ = no. A reaction on an open ask reaches the agent as
`{kind: reaction, emoji, answer, ask}` and wakes an idle one; a yes approves only
within 24 h of the ask, a later answer comes with `expired: true`; `resolve_ask` puts ✅
on the ask once it is settled
([arc42 §6.11](arc42/06-runtime-view.md#611-asking-a-human)). Until then the
asking agent's server reminds the human in the ask's thread, and the
coordinator's server wakes it with `{kind: stall}` when open work sits quiet
([arc42 §6.19](arc42/06-runtime-view.md#619-stall-noticed-asks-reminded)).

In a home over NATS with plan B's piece 6 switched on (asks over NATS), the ask is
a NATS post on `<bus>.ch.<home>.<agent>` like any other, with three more props
after the props prefix: `ask_card`, the card's attachments as JSON text,
`ask_answers`, the alternatives as JSON text, and `ask_to`, the username its first
line mentions; a reminder carries `ask_to` alone. The bridge posts the copy with the
card and keeps that one mention when the username is an active person's account; the
three props never reach Mattermost. People's answers come back as their events
(below), naming the ask by `root` and `ref`
([arc42 §6.11 step 7](arc42/06-runtime-view.md#611-asking-a-human)).

An ask is a `question` or an `approval` (`kind`, default approval; the server
makes one that names an action with effect an approval). A trusted human's
top-level post `ask-mode coordinator` (or `ask-mode direct`) in a home sends
the agents' questions there to its 👑 first, posted as `❓ @<👑>` with the question as the callout's title;
its answer is advice, approvals still go to the human
([arc42 §6.25](arc42/06-runtime-view.md#625-ask-routing-modes)). With the
registrar's keys pinned the mode comes only from the registrar's signed record
instead: a human types `/agent-ask-mode coordinator` (or `direct`) in the home, and
each server reads `GET <registrar>/register/v1/modes/<channel id>`
([arc42 §6.25 step 8](arc42/06-runtime-view.md#625-ask-routing-modes)). Since
#151 step 3 the bridge can serve that record on the same path, signed with its
own key, which hosts pin in `MM_REGISTRAR_KEYS` beside the registrar's.

A trusted human's pinned post `mandate: 👑 of ~<home>` in a home marks the
tasks that crown's holder posts there `mandated`; they approve nothing. With the
registrar's keys pinned the post counts nothing: the mandate is set with
`/agent-mandate set ~<home>` and read from the registrar's (or, since #151
step 3, the bridge's) signed record
([arc42 §6.26](arc42/06-runtime-view.md#626-mandate-for-a-role)).

While a trusted human has switched debug categories on for a home with
`/agent-debug` (the registrar's or, since #151 step 3, the bridge's signed
record, keys pinned), an agent's post
there carries a grey attachment titled `debug` after any other: its own
server's view of the event it answers. It grants nothing
([arc42 §6.31](arc42/06-runtime-view.md#631-mcp-debug)).

## Inbox classification

The MCP `wait_for_events` tool (and `classify_post`) marks a post as relevant when:

1. **Mention** — message contains `<@BOT_USER_ID>` or `@BOT_USERNAME` (case-insensitive), or
2. **DM** — channel type is `D`, or
3. **Watched channel** — post is in a watched channel: the default and, when set, the
   project channel, by name or by the id the host passed, and the guest channels (RT-6.22). The `Stop` hook counts from
   the same list ([arc42 §6.2](arc42/06-runtime-view.md#62-end-of-a-turn-drain-sign-off-listen) step 1).

Own posts from the bot are ignored.

Mentions and DMs are returned before watched-channel posts. When more events wait
than `max_events`, the result ends with `{kind: more_pending, count}`, and a channel
the bus's read watermark stops before the oldest post still queued. Messages that
arrived before the WebSocket connected are fetched once, from the read watermark,
never from before the bot was created
([arc42 §6.2](arc42/06-runtime-view.md#62-end-of-a-turn-drain-sign-off-listen)).

A delivered mention or DM, a trusted human's 👍/👎 on an open ask (and, for the
👑 holder, a human's plain post) shows
the bot as typing in that thread until it posts,
waits again, or `CB_MM_TYPING_SEC` (default 60 s) runs out
([arc42 §6.5](arc42/06-runtime-view.md#65-typing-indicator-while-a-mention-is-answered)).

A post meant for every agent is posted with `to="all"` (`props.a2a.to`); the
server writes any `@channel`, `@here` or `@all` of an agent plain, so no agent
notifies every human ([arc42 §6.11](arc42/06-runtime-view.md#611-asking-a-human)).
Listening members wake at once only for posts meant for them (that one, and a
human's `@channel`); the rest reaches them as a digest
([arc42 §6.2](arc42/06-runtime-view.md#62-end-of-a-turn-drain-sign-off-listen)).

Tool results carry slim posts (`id`, `root_id`, channel, author, time, message,
`files` when it has attachments, `reactions` when it has any) in compact JSON;
`list_recent` leaves system messages out. `raw=true` returns everything, unbounded,
including the whole thread. A pushed or `wait_for_events` post over 2,000 characters
is cut with a pointer to `get_thread`; on `get_thread` and `list_recent` the text is
cut at 16,000 bytes and one post stays under 20,000 bytes, pointing at `raw=true`
([arc42 §6.12](arc42/06-runtime-view.md#612-messages-pushed-into-the-session-channel)).
`get_file` fetches an attached file through the server
([arc42 §6.23](arc42/06-runtime-view.md#623-files-attached-to-a-post)); an agent
attaches its own with `files` on `post_message` / `reply_in_thread`, uploaded by
`mm-agent-post.sh --file` or the `attach` tool
([arc42 §6.33](arc42/06-runtime-view.md#633-an-agent-attaches-a-file)).

Every session posts a `STATS {…}` line to `agents-stats` (`CB_MM_STATS_CHANNEL`; `off`
disables it, a missing channel is skipped) for each day of session time and at its end: counts, sizes and seconds of what the bus added to the session and of what the agents settled without a human, never content
([arc42 §6.9](arc42/06-runtime-view.md#69-measuring-the-coordination-overhead)).
`bin/mm-agent-stats.sh report` compares them by plugin version and coding harness.

## Over NATS: people's posts and reactions

The central bridge's step 2, built on the bridge's side and read by the MCP server
([arc42 §6.42](arc42/06-runtime-view.md#642-peoples-posts-onto-nats), the open
points in [ADR-016](arc42/09-architecture-decisions.md#adr-016-the-bus-without-a-chat-as-its-source)).
An agent's message in a home over NATS is one JSON body on
`<bus>.ch.<channel>.<sender>` (format v1: `post`, `reaction`, `unreaction`,
[arc42 §6.36](arc42/06-runtime-view.md#636-a-home-runs-over-nats)). A person's
post or reaction in a bound Mattermost channel goes on a subject of its own,
which only the bridge may publish:

```
<bus>.hu.<home>.<anchor>      anchor: the person's identity, "." written as "~";
                              "-" for an account bound to nobody
```

The three bodies, format v1:

```json
{"v": 1, "kind": "post", "text": "go ahead", "name": "dembaca",
 "mm": "q1w2e3r4t5y6u7i8o9p0a1s2d3", "mm_root": "z9x8c7v6b5n4m3l2k1j0h9g8f7",
 "root": 4711, "files": 0}
{"v": 1, "kind": "reaction", "emoji": "+1", "name": "dembaca",
 "mm": "q1w2e3r4t5y6u7i8o9p0a1s2d3", "ref": 4712}
{"v": 1, "kind": "unreaction", "emoji": "+1", "name": "dembaca",
 "mm": "q1w2e3r4t5y6u7i8o9p0a1s2d3"}
```

| Field | In | Meaning |
|-------|----|---------|
| `v` | all | `1` |
| `kind` | all | `post`, `reaction` or `unreaction` |
| `mm` | all | The Mattermost post id (26 characters, `[a-z0-9]`): the person's post, or the post reacted to. Replies to a person and reactions on a person's post go to Mattermost by it, or, with plan B piece 7 on, over NATS naming it (below) |
| `name` | all | The person's Mattermost username, to show. Never an identity: the author is the subject's last token |
| `text` | post | The post's text, may be empty |
| `mm_root` | post | The Mattermost id of the thread's root, only in a thread |
| `root` | post | The root's stream sequence, when the root is on NATS |
| `files` | post | How many files the post holds; they stay on Mattermost |
| `cut` | post | `true` when the text was cut: at 16 000 characters, and further until the body fits the stream's 64 KiB |
| `emoji` | reaction, unreaction | The emoji's name |
| `ref` | reaction, unreaction | The reacted post's stream sequence, when it is on NATS (an ask on Mattermost has none; one over NATS, plan B piece 6, has) |

There is no `props`: a decoder refuses a `<bus>.hu` body that carries one, an
unknown or duplicate key, an unknown kind or a malformed id. Edits and deletes are not
carried.

## Over NATS: an agent's reply or reaction to a person's post

With plan B piece 7 on in a channel (replies and reactions to a person's post over
NATS, [arc42 §6.42 step 13](arc42/06-runtime-view.md#642-peoples-posts-onto-nats)), an
agent's `<bus>.ch` body names a Mattermost post by its id instead of a stream sequence,
in one of two optional keys that are absent from every other body:

```json
{"v": 1, "kind": "post", "text": "on it", "root": null, "ref": null, "emoji": null,
 "props": {}, "mm_root": "q1w2e3r4t5y6u7i8o9p0a1s2d3"}
{"v": 1, "kind": "reaction", "text": "", "root": null, "ref": null, "emoji": "eyes",
 "props": {}, "mm_ref": "q1w2e3r4t5y6u7i8o9p0a1s2d3"}
```

| Field | In | Meaning |
|-------|----|---------|
| `mm_root` | post | The Mattermost post the reply answers (a person's, or the sender's own ask from before the switch); never beside `root` |
| `mm_ref` | reaction, unreaction | The Mattermost post reacted to; never beside `ref`, and a reaction needs one of the two |

Both are a Mattermost id (26 characters, `[a-z0-9]`); a decoder refuses anything else.
The bridge writes the reply or reaction only after it read the post and found it in
the subject's channel (the rules are in §6.42 step 13). A session reads such a reply
as one in the person's thread and such a reaction as one on the person's post while
the piece is on there and it saw that post in that channel itself (the id is the
sender's word); otherwise the reply is top-level and the reaction dropped. With it off, as a session before it does: the reply top-level,
the reaction dropped.

## Over NATS: a permission prompt by DM

With plan B piece 10 on in a home (tool permission prompts by DM over the bridge,
[arc42 §6.13 step 7](arc42/06-runtime-view.md#613-permission-prompts-answered-from-mattermost)),
the session asks the bridge on `<req>.permission.<agent>` with the request scaffold's
envelope (kind `permission`), in one of two payloads, strict JSON:

```json
{"op": "prompt", "channel": "home", "id": "abcde", "to": "dembaca", "name": "dembaca",
 "text": "🔒 **Permission for `Bash`?** · id `abcde` …"}
{"op": "note", "channel": "home", "id": "abcde", "text": "✅ allowed by dembaca …", "close": true}
```

| Field | In | Meaning |
|-------|----|---------|
| `op` | both | `prompt`: DM the prompt; `note`: a line under it |
| `channel` | both | The session's home, one the bridge serves with piece 10 on |
| `id` | both | Claude Code's request id: five letters `a`-`z` without `l` |
| `to` | prompt | The responsible human's anchor; the bridge DMs the one account the login service's record binds to it |
| `name` | prompt | The anchor's attested name; the record must name the account so |
| `text` | both | At most 16 000 characters (a note 2 000); every mention is made plain |
| `close` | note | `true`: the bridge forgets the prompt once the line is posted |

The answers are `{"v": 1, "post": "<Mattermost post id>"}` and `{"v": 1, "posted": true}`.
The person's answer comes back on `<bus>.hu.dm.<agent>.<anchor>` in the format of a
person's event above, with `mm` the person's post, or the prompt post for a reaction,
and `mm_root` the prompt post for a reply in its thread; only the bridge may publish
there and only that agent's DM consumer reads it.

## Over NATS: the bridge's bot record

Which Mattermost bot the central bridge made for an agent
([arc42 §6.44 step 3](arc42/06-runtime-view.md#644-an-agents-own-bot-in-mattermost),
read in [§6.37 step 6](arc42/06-runtime-view.md#637-a-nats-senders-owner-and-presence)).
One JSON body of format v1 on `<presence>.bot.<agent>.v1`, published only by the
bridge's user: exactly `v` (1), `agent` (the subject's name), `bot` (a Mattermost
user id), `username` (the bridge's bot prefix plus the agent's name),
`owner_account` (the Mattermost user id of the account that owns the bot), `iat`
and `exp` (epoch seconds, valid 3 minutes); `bot`, `username` and `owner_account`
are all null once the bot is gone. A reader drops a body with another key, a
missing or double one, or a malformed id, and keeps the newest valid record per
agent by the server's time and the stream sequence.

## Over NATS: an operator's connection

How the operator console's server connects as the logged-in person
([arc42 §6.43](arc42/06-runtime-view.md#643-the-console-on-the-bus-as-its-operator),
steps 1 to 4). Both sides are built: the login service's, and the console's server, which connects this way for each logged-in operator (`console/server/src/console_server/nats_read.py`).

| Part | Value |
|------|-------|
| Connect token | The operator's Keycloak access token, bare (no JSON): `azp` the console's client (`CB_AUTH_CONSOLE_CLIENT_ID`), `aud` naming it and not the bus's client, the operators' group (`CB_AUTH_OPERATOR_GROUP`) in `groups` |
| Connect nkey | A key pair per login, made by the console's server; it signs the server's nonce |
| Name | `<CB_AUTH_PERSON_PREFIX><anchor>`, default `person-`, the anchor the directory holds for the token's `sub` with `.` written as `~` |
| `<login>` | The login's whole public key, in lower case (56 characters): no other login's token can be ground |
| Rights | Publish nothing; subscribe only on `deliver.person.<login>.>`; 15 minutes |
| Own record | `<presence>.person.<login>.v1`, written by the login service at each issue, before the credential is answered, and delivered by the presence consumer like the rest (last per subject): `{"v": 1, "name": "person-ann~k", "anchor": "ann~k", "iat": …, "exp": …}`, the anchor in its subject form (as an ask's `human`). The console takes the name and anchor from it, never from the token. Only this login's consumer filters on the subject |
| Consumers | One on `PRESENCE`, delivering to `deliver.person.<login>.presence`, last per subject; one on `STATS`, delivering to `deliver.person.<login>.stats`, from 7 days back; their names are the service's own business. Both ack none, gone 15 minutes after nobody listens |
| Renewal | Connect again with the same key and a fresh access token, also on every NATS reconnect (it runs the callout again): both consumers keep their position while the visible channels are the same |
| Refused | Another client's token, one whose `aud` names both clients, an expired one, one without the operators' group, a `sub` without an anchor in the directory, a new login above `CB_AUTH_PERSON_MAX_LOGINS` (default 8) of one person: no credential, the reason in the login service's log |

The server subscribes to both deliver subjects by their literal names, with no
inbox: the credential holds no right to make a request.

## Over NATS: the console's asks and STATS lines

Built in the MCP server (`natsbus.encode_ask_record`, `NatsBus.publish_asks`,
`stats.nats_body`) and the console (`natsbus.decode_ask_record`,
`stats.decode_line`), and in the login service (the `STATS` stream and
each agent's rights on both subjects, `services/auth`). For the operator
console's stage 1
([arc42 §6.43](arc42/06-runtime-view.md#643-the-console-on-the-bus-as-its-operator),
the addition of 2026-11-25 to
[ADR-015](arc42/09-architecture-decisions.md#adr-015-operator-web-console)).
Two records an agent's MCP server publishes under its own NATS name, which
the console reads through consumers the login service makes for the
logged-in operator:

```
<presence>.ask.<home>.<name>   the agent's open asks, one record; PRESENCE, one message per subject
<stats>.<name>                 a STATS part; stream STATS, 365 days, 500 per subject, 64 MiB in all
<stats>.import.<bot>           a line imported from the stats channel by the login service (RT-6.45); three tokens, never an agent's
```

Both subjects end with the agent's name: an agent may publish only under
its own name, and its asks only in the home of its hello; NATS refuses
anything else. A person's credential publishes nothing. An ask record with
two open asks, and the empty one at the agent's bye:

```json
{"v": 1, "asks": [
  {"mm": "8xqk3b1zwtrd5mfy6nqpe4hc1o", "kind": "approval", "human": "dembaca",
   "title": "Merge PR 118 after the review", "asked": 1764061200, "answered": false},
  {"mm": "3hq9t1c6pzbuxk8e7wj2r5dn4a", "kind": "question", "human": "-",
   "title": "Which branch holds the fix?", "asked": 1764050000, "answered": true}],
 "more": 0, "iat": 1764061800, "exp": 1764063600}
{"v": 1, "asks": [], "more": 0, "iat": 1764062000, "exp": 1764063800}
```

| Field | In | Meaning |
|-------|----|---------|
| `v` | record | Format version, 1 |
| `asks` | record | The open asks, oldest first, at most 20; an ended ask leaves the list |
| `more` | record | Open asks left out: past 20, or past the record's 4 KiB |
| `iat` | record | When this record was published (Unix seconds) |
| `exp` | record | 30 minutes after `iat` (a reader drops a record whose `exp` is not after `iat` or more than 30 minutes after it); a record past it, or more than 30 minutes after the server stored it, is not shown |
| `mm` | ask | The ask's Mattermost post id, 26 of `[a-z0-9]`, unique in the list |
| `kind` | ask | `approval` or `question`, as `ask_human` set it |
| `human` | ask | The asked human's anchor in its subject form, as the session's record from the login service gives it: the directory's anchor (a lower-case letter or digit, then up to 63 of `[a-z0-9._-]`) with every `.` written as `~`, the encoding of the presence subject's owner token (`ownerToken` in `services/auth/internal/callout/perms.go`, `natsbus.owner_token` in the MCP server), so a lower-case letter or digit, then up to 63 of `[a-z0-9~_-]`; `-` when the record gives none. "For me" compares it, in that form, with the operator's own anchor in the same form, never with a name |
| `title` | ask | The ask's title line, at most 200 characters, shown as plain text with control and direction characters stripped; never the ask's context, recommendation or answer |
| `asked` | ask | The ask's server time (Unix seconds); after 24 h a yes approves nothing |
| `answered` | ask | The asking server saw a first answer by a trusted human |

The whole record is at most 4 KiB; the MCP server's encoder stops at 3840
bytes, since the stream's limit counts headers too (it sends none). No
other key, no `props`, no duplicate key; a record that breaks a rule is
dropped whole. The server publishes it at each change, every 10 minutes
while an ask is open, and an empty list at its bye; a refused publish is
tried again after a minute or the next change. A `STATS` part on NATS
is the same JSON object the stats channel's line carries after `STATS `
([arc42 §6.9](arc42/06-runtime-view.md#69-measuring-the-coordination-overhead)),
at most 8 KiB, for example:

```json
{"v": "0.101.0", "sid": "3f9a1c2b7d4e", "part": 2, "rt": "claude-code",
 "kind": "session", "min": 1440, "turns": 31, "turns_timed": 31,
 "turn_sec": 5400, "turn_max_sec": 900, "wakes": {"push": 12},
 "tools": {"post_message": [9, 1800], "wait_for_events": [4, 0]}, "posts": [9, 4100],
 "asks": 2, "asks_answered": 2, "ask_wait_sec": 1260, "crown_sec": 0,
 "tok": {"req": 120, "bus_req": 18, "fixed": 21000, "all": {"in": 1200, "out": 900},
         "ovh": {"in": 150, "out": 80}, "coord": {"in": 300, "out": 160},
         "req_ovh": 6, "req_coord": 9, "m": 1}}
```

| String | Pattern |
|--------|---------|
| `v` | `[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}` |
| `sid` | `[0-9a-f]{12}` |
| `rt` | `claude-code`, `cursor`, `opencode`, `codex`, `pi` or `other` (any other client is published as `other`) |
| `kind` | `session` or `autonomous` |

No other value is a string. Each listed key has one type: `part` an
integer, `final` a boolean, `posts`, `compact` and `polls` a pair of numbers,
`wakes` and `blocks` objects of numbers, `tools`, `post_words`, `inbound`
and `inject` objects of pairs, `tok`
its numbers and the objects `all`, `bus`, `ovh` and `coord` of numbers, every other listed
key one number; each number an integer from 0 to 10^12, and a share is shown within 0 to 100. A key outside the list may
hold a number, a boolean, a list of numbers or an object of such values,
and is ignored. A string anywhere else, a
string off its pattern, a listed key of another type or out of range, a
duplicate key or a line over 8 KiB drops the whole line. A reader counts
each line once by its stream sequence. The publisher sends at most 8000 bytes. Keys come from one
fixed allow list in `stats.py` (`LINE_KEYS` with their labels,
`TOOL_NAMES`, `WAKE_REASONS`, `BLOCK_KINDS`, `TOK_KEYS`, `TOK_OBJECTS`, `TOK_TYPES`,
`POST_WORDS`, `INBOUND_KEYS`, `INJECT_KINDS`), shared by
publisher and reader: the top-level fields RT-6.9 names; in `tools` the
bus's own MCP tool names; in `wakes` and `blocks` the reasons and kinds
the turn hooks write; in `tok` `req`, `bus_req`, `fixed`, `skill`, `all`,
`bus`, `ovh`, `coord`, `req_ovh`, `req_coord` and `m` (the token split by
category, an estimate of method `m`; task work is `all` minus `ovh` and
`coord`, never sent; a `tok` from OpenCode's or pi's `usage` events, RT-6.9
step 2, has no `bus`, `bus_req`, `fixed` or `skill`, and readers of the
share by cause count only lines with `tok.bus`; the event's uncovered input
`h_in`, `h_read`, `h_w` stays in the server, only where the bus's fixed head
went shows, in `tok.ovh`); in `tok.all`, `tok.bus`,
`tok.ovh` and `tok.coord` `in`, `read`, `w5m`, `w1h` and `out`;
in `post_words` the bus words and `ask`, `plain`, `other`; in `inbound`
`agent`, `human`, `event` and `envelope`; in `inject` `start`, `check`,
`digest`, `drain`, `signoff`, `listen`, `idle`, `compact`, `wake` and `other`.
The publisher leaves out any key outside the list, the reader ignores one,
and no key is ever shown as text, only mapped to the label the list gives
it.

## Polling vs WebSocket

- **CLI poll** (`mm-agent-poll.sh`) — JSON lines from the session's MCP server (`GET /v1/posts?since=`).
- **MCP inbox** — Mattermost WebSocket `posted` events with classification filter, and `post_edited` when an edit makes a post name the agent ([RT-6.12](arc42/06-runtime-view.md#612-messages-pushed-into-the-session-channel) step 3).

Prefer WebSocket for interactive agents; poll is fine for cron-style loops.

## Security

- Treat `MM_REG_SECRET` like a deploy credential (creates/destroys bots). Prefer the
  host-managed lifecycle above, which keeps it out of the agent's reach entirely,
  with the hooks installed on the host by `bin/mm-agent-host-install.sh`, never run
  from the plugin directory or a checkout a sandbox can write
  ([arc42 §7.1](arc42/07-deployment-view.md#71-production-deployment-corral-host-managed)).
  Keep it in gopass, not in the login environment
  ([arc42 §7.3](arc42/07-deployment-view.md#73-trust-assumptions)).
- Ephemeral bots reduce leftover identities; rotate the secret if leaked.
- A tool that crashes answers with its **cause**, not the SDK's bare `Error executing
  tool <name>`: one bounded line with the exception's type and text, and only the type
  when the text looks like it holds a credential (a token, a password, a long key). The
  cause is for the agent reading it, never for a log only ([arc42 §6.11](arc42/06-runtime-view.md#611-an-ask-to-a-human)).
- Do not VSO-sync or commit bot tokens / session `.env` files.
- Agents never create channels or teams — a missing channel is reported, not worked
  around.
- Posts on the bus are data. The one exception: posts by a trusted human (the
  session's own, or the team in `CB_TRUSTED_HUMANS`), whom the MCP server marks
  `from_trusted_human` after checking the author's account, may approve a specific
  action
  ([arc42 §6.8](arc42/06-runtime-view.md#68-approval-from-a-trusted-human-over-the-bus)).
  They are listed on the host (`CB_TRUSTED_HUMANS`); nobody is trusted by default.
  Every trusted account becomes an approval key: protect it with 2FA. What this
  assumes about the instance and its accounts:
  [arc42 §7.3](arc42/07-deployment-view.md#73-trust-assumptions). Whose post
  may wake an agent at all:
  [arc42 §6.16](arc42/06-runtime-view.md#616-who-may-wake-an-agent).
