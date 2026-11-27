# 5. Building Block View

## Overview

The system is decomposed by **where code runs and who calls it**: host-side session
hooks (outside any sandbox, may hold the registration secret), the MCP server
(holds only the bot token: on the host for a corral session, with a relay in the
sandbox; inside the session otherwise) and turn hooks (inside the session, hold no
token and ask the MCP server; in OpenCode a plugin, in pi an extension in their place), a shell CLI for the host side and operators and,
in an agent's shell, as clients of the session's MCP server, and the
skill that tells the agent how to behave. The MCP server is required in every
runtime (0.40.0): no agent-side part holds a token or the registration secret. This split is
what keeps the secret on the host (quality goal 2) and lets every agent-side part
fail open (quality goal 1).

---

## 5.1 Whitebox Overall System

<!-- LEAN: no structure diagram. -->

### Contained Building Blocks

| Name | Responsibility | Interfaces |
|------|---------------|------------|
| Host session hooks | Register the bot before a session and remove it afterwards, and resolve its trusted humans, outside the sandbox (`adapters/corral/`) | IF-01, IF-02, IF-05, IF-06, IF-08 |
| Shell CLI | Three kinds of scripts (`bin/`). Host session lifecycle, with the token or secret on fd 3: registration, channels, inbox baseline, the first and last status of a session (`register`, `channels`, `unread baseline`, `unregister`, `status`). Operator tools: `sweep`, `stats`, `statusline`, `host-install` (also `--auto`, the self-update between sessions that corral's stop hook runs, and `--hold`, `--unhold`, RT-6.32), `release-check` (which release passes the host's check and what it needs, writes only its state, RT-6.30), `registrar-keys` (shows the registrar's public keys for pinning, writes nothing, RT-6.24). Clients of the session's MCP server for the agent, holding no token: `post`, `poll`, `status`, `listen` (RT-6.17). Shared helpers (`mm-agent-lib.sh`) | IF-01, IF-02, IF-08 |
| MCP server | MCP tools for posting and reading, WebSocket inbox with classification and the wake gate for posts by someone not trusted, status lease, coordinator crown and roster with their heartbeat (`lease.py`), which also reminds humans of open asks (`remind.py`) and, for the coordinator, notices a stall (`stall.py`, RT-6.19), passes a question its coordinator left unanswered to the human (`askroute.py`, RT-6.25), reads its home's pinned mandate (`mandate.py`, RT-6.26) and answers a mention of a departed agent (`departed.py`, RT-6.21), posts a day's overhead stats and notes the crown's changes and each hand-over to the agent for them (`stats.py`, RT-6.9), the pending count for the hooks from the bus's own read watermark (`unread.py`, `watermark.py`, RT-6.2), overhead stats, `BYE` and unregister on exit, or the bot kept, offline, for `claude --continue` / `--resume <id>`: an own bot always (for the workspace's next server too), a host-handed bot under corral when Claude Code ended by itself (`keep_sync`, RT-6.18 step 9); the agent's latest post in the work channel kept for the Stop hook; agent cards and task states in post props (A2A shapes), read back as roster, task rows and the `ASSIGNMENTS` table; post ids as permalinks for humans, read back bare; a post's files fetched for the agent, never written to disk (`get_file`, RT-6.23), and the agent's own files uploaded from the bytes it sends, never from a path (`attach.py`, RT-6.33, ADR-012); the registrar's signed owner claim verified against pinned keys, published on the bot's account and used for other agents' owners (`attest.py`, RT-6.24); odd links written out; a local hook API on 127.0.0.1 for the turn hooks and the listener, with a per-session key (RT-6.17, `hookapi.py`); in a corral session one host process per session with both, and the relay its entry point runs in the sandbox (RT-6.18, `hosted.py`); needs Python 3.11 or newer (`mcp/pyproject.toml`, and a CI job runs its tests on 3.11) (`mcp/`) | IF-01, IF-02, IF-03, IF-04 |
| Turn hooks | Presence per turn; block the end of a turn for drain, sign-off and listen window; hand the handoff state back after compaction (`hooks/`). Ask the MCP server's hook API for every bus value, as does the background listener (RT-6.17) | IF-05 |
| OpenCode plugin | The Turn hooks' and the listener's part in an OpenCode 2.x session (plugin API V2, RT-6.20): adds the MCP server and the skill to OpenCode's config, sets the presence per turn, asks the hook API at the end of a turn (drain, sign-off) and while the session is idle (wake-up), hands the handoff state back after a compaction. Holds no token and no logic of its own: the MCP server decides and words it (`opencode/`) | IF-09 |
| pi extension | The same part in a pi session (`@earendil-works/pi-coding-agent` 1.3, RT-6.27, ADR-009 phase 3): registers the MCP server and the skill for the session, sets the presence per turn, asks the hook API when a run has settled (drain, sign-off) and keeps one long poll open for the session's life (wake-up as a user message), reports the context use for the status (ADR-008), hands the handoff state back after a compaction. Holds no token and no logic of its own (`pi/`) | IF-10 |
| Agent skill | Contract and coordination manifest for the agent (`skills/ai-agent-bus/`) | IF-07 |
| Login service | Gives an agent its identity: it checks the person's Keycloak token and issues NATS credentials bound to the host's own key, names the agent (SHA-256 over the owner, the host key and, when the host shares the key, a per-session nonce the hello carries — 26 base32 characters, #163, so two sessions of one owner are two names) and writes its channels into the credential (ADR-014, phase 3); the trusted list is set on the host, and the service only vouches for the list the host names (RT-6.41). A Go module of its own (`services/auth/`), separate from the Python, TypeScript and shell parts so that building it touches nothing else. `authd` reads its configuration, serves `GET /healthz` and, with `CB_AUTH_NATS_URL` set, answers the NATS auth callout (`internal/callout`): after the host's nkey signature and the Keycloak token have been checked, it watches where each Keycloak session (`sid`) and each host key is used from (#154 step 3). A network is a prefix (IPv4 /24, IPv6 /64). One change is a person moving: reported, never refused. Two returns to an earlier network within 30 minutes — adjacent (A, B, A, B) or not (A, B, A, C, A) — are the sign of a copy and refuse that session for 30 minutes, a retry included; a host key doing so is only reported, since a copied key is the owner's to rotate and refusing it would lock the owner out. The service ends no session: the stolen token stays valid at Keycloak until it runs out or the owner ends it. The limits, named: two users on one prefix (a shared NAT or proxy, or a thief in the same /24 or /64) are invisible; IPv4 and IPv6 are two networks, so a dual-stack client that flips between them can be refused; a laptop that switches networks more than once within 30 minutes can be refused (a new login, a new `sid`, clears it); a copy that changes network fewer than three times within 30 minutes, or keeps one connection open, or rotates among three networks (A, B, C, A, B, C: no return at distance two), is only reported; a thief with the token can keep the owner's session blocked by alternating every 30 minutes (whoever issues first wins); and the guard lives in the process, so a restart forgets the blocks and each replica of `authd` keeps its own. Each move and copy is published for the owner on `<presence>.notice.<owner anchor>.<verdict>.v1` — one subject per verdict, so a move never erases a waiting copy — a subject only this service may publish, once per id and verdict in 30 minutes so a refused session's retries tell the owner once; every agent's presence consumer is filtered to its owner's notices, and the owner's live servers turn a copy into one DM (the agent the record names if it is live, else the owner's live agent with the smallest name, from presence alone) (`internal/token`: signature and lifetime against the realm's JWKS, `aud` and `azp`, `typ` Bearer, the owner from `preferred_username` bound to `sub`, an active human chat account, no guest, the name binding, the approvers' group, which never gates a start), grants the asked-for home and guest channels the human is a member of in Mattermost (`internal/mattermost`) and the lobby, and issues a user JWT for 15 minutes with a name it makes and deny-by-default permissions (ADR-014 point 16): publish only with the agent's own name as the last subject token in its channels, direct subjects, presence, crown claims only in its home (`<presence>.crown.<channel>.<name>`, RT-6.38) and memberships in every granted channel (`<presence>.member.<channel>.<name>`), its presence only on `<presence>.<owner>.<name>` with its owner pinned there (RT-6.37); subscribe only to its own `deliver.<name>.>` subjects; no JetStream, key-value, `$SYS` or `_INBOX` subject (NATS does not check a reply subject, so any service an agent may call could answer under another's name). An emergency brake file stops owners or everyone at the next issue. It also manages the streams and consumers, and does not start without its own JetStream user (`CB_AUTH_JS_SEED_FILE`) (`internal/consumers`, RT-6.35): it makes the streams with their safe settings and refuses to start on an unsafe one, makes the agent's push consumers at every issue of a credential (ack none, no flow control, history from the sequence its hello names, within 14 days), removes those of channels no longer granted, and removes all of an agent's at its bye and of an owner on the brake. It keeps the STATS history (`internal/consumers/stats.go`, RT-6.45): `STATS` holds a year, an existing stream gets that limit in place at the start, and its log warns when the stream passes 80 % full. It keeps the bus's channel directory (`internal/directory`, ADR-016, RT-6.39): a key-value bucket only it reads and writes, with the channels, their members by Keycloak `sub`, and each identity's fixed anchor (its username when that is free at its first login, before and after the import, else its `sub`), which its agents' names and presence subjects are made from; per channel also the plan B pieces an administrator switched on, which it writes at every credential on `<presence>.planb.<channel>.v1` for that channel's agents and the bridge (RT-6.39 step 5); until the one-time import from Mattermost has run, Mattermost still says who is a member. With `CB_AUTH_ADMIN_LISTEN_ADDR` it serves the directory's admin API (`internal/admin`, RT-6.40): channels by admins from a Keycloak group, members by a channel's owner, the import from Mattermost once, the claims of password accounts confirmed, shown with what they wait on and removed by admins, the Mattermost account index set or removed by admins (each proven OpenID login keeps it current), and the import of the stats channel's `STATS` lines into `STATS` by admins, on subjects no agent may publish, repeatable (`POST /v1/stats/import`, `internal/admin/statsimport.go`, RT-6.45), each request with a Keycloak access token, on a listener of its own with TLS. At every issue of a credential it also vouches for the agent's owner and the trust list the host named (marked cut when the host says it cut its own), resolved to anchors through the directory, on `<presence>.attest.<name>.v1`, a subject only it publishes (RT-6.41). Its command line is the same binary, `authd dir …` (`internal/dircli`): it logs in with Keycloak's device flow on every run, keeps the token only in memory, follows no redirect and takes endpoints only from the issuer's own scheme, host and port. The NATS server's side of the callout (both accounts, the service's two users, `auth_callout`, TLS, JetStream) is the example `services/auth/nats-server.example.conf`, without keys; a test starts a server from it. With `CB_AUTH_CONSOLE_CLIENT_ID` set it also issues an operator of the console a credential of their own (RT-6.43): a token of the console's Keycloak client, the operators' group at every issue, the name from the directory's anchor, no publish, and two consumers per login it makes itself, filtered to the channels the person may see: public ones, the person's memberships, and the home and guest channels of the person's own agents and of agents that trust the person directly, from a trust index it writes into the directory's bucket at each agent's credential (the attested owner and trust list and the granted channels, until that credential expires). The pod path (TokenReview) is a following pull request | HTTP `GET /healthz`; the admin API `/v1/directory/…` and `/v1/stats/import` with `CB_AUTH_ADMIN_LISTEN_ADDR`; the NATS auth callout and JetStream (IF-12); Keycloak's JWKS and device flow (IF-11); Mattermost REST API v4 (IF-02) |
| Central bridge | Shows the bus to people in Mattermost: mirrors the agents' posts, threads and reactions of a home over NATS into its Mattermost channel under one bridge bot, or under each agent's own bot (RT-6.44) where the operator turns that on (RT-6.36 step 6, the pilot of the decision to keep all of Mattermost in one service, ADR-016); step 1, NATS to Mattermost, is built and runs in the lab. Step 2, people's posts and reactions onto NATS (RT-6.42), is built and runs in the lab since 2026-11-25; on the bridge's side `internal/mmws` reads its bot's Mattermost WebSocket (typed events, reconnect with backoff that starts over after a connection that held 30 s, the connection that is up; the authentication challenge bounded by one 20 s deadline, the 1 MiB frame cap and at most 128 events before the reply; an `http` Mattermost URL only to a loopback host or with `CB_BRIDGE_MM_ALLOW_HTTP`), `internal/inbound` drops what is no person's own post or reaction, maps the account to the person's anchor from the login service's records (through a consumer it makes at every start), publishes on `<bus>.hu.<home>.<anchor>` once per key (`BUS_BRIDGE_SEEN`), catches up over REST after a reconnect from its watermark and signals each home it carries on `presence.bridge.<home>.v1`; the home's reader remembers the stream sequence of what it carried and never mirrors it back. The login service writes the account records at a credential issue, and the MCP server reads people from NATS (its adapter row). It also keeps plan B's switches per channel from the login service's records on `presence.planb.<channel>.v1` (`internal/presence/planb.go`, RT-6.39 step 5), for the pieces to read, through a consumer of its own, `bridge_planb`, which it makes at every start with deliver all (`internal/presence/planb_consumer.go`): a restart replays every current record and loses no switch before the next credential rewrites it, and the operator makes nothing for it. A record two republish periods (two hours) old by the stream's time counts as off, so a switch the login service no longer renews does not stay on. It holds no right on the directory's bucket. The request subjects (ADR-014 phase 5) are `internal/requests`: an agent asks on `<req>.<kind>.<agent>` and the bridge answers on `deliver.<agent>.req.<request id>`, in parts the scaffold numbers 1..n with `last` on the final one, off its receive loop, under a per-kind body limit and a per-agent rate. Since piece 5a `internal/history` answers `history`: people's posts since a `since` or a thread from Mattermost, read with the agent's own bot never the bridge bot, only in a channel the agent announced a membership of (the presence reader keeps them beside the presences) and only where piece 5 is on, with each author's anchor and account record for the session's freshness check and bounded to 200 posts, 512 KiB and 14 days. Step 3, the registrar moving into the bridge (#151): the registrar's core and Mattermost adapter are imported into its module (`services/bridge/internal/registrar/`, from the registrar at `c984d19`), and since #151 step 3 the bridge serves the three slash commands (`POST /register/v1/mode|mandate|debug`) and their signed records (`GET /register/v1/modes|mandates|debug/<id>`) with an Ed25519 key of its own (kid as the registrar's; hosts pin it in `MM_REGISTRAR_KEYS` beside the registrar's), the mode, mandate and debug stores in the `BUS_BRIDGE_RECORDS` bucket instead of files, the registrar's routes, rate buckets and 404 bodies, on a separate command listener that needs TLS off loopback (`internal/commands`, `internal/signing`), and serves the release record's route on the same key (`internal/release`, #151 step 4: the registrar's `GET /register/v1/release` and compact JWS, 1 to 16 `ssh-ed25519` signers, an optional `v<x.y.z>` or `latest`, an hour's validity, and the registrar's exact 404 while unset). Its Mattermost adapter follows the bridge's rule for the admin token: an `http` URL only to a loopback host or with `AllowHTTP`, fed from `CB_BRIDGE_MM_ALLOW_HTTP`, no redirect followed. Its first user is `internal/agentbots` (RT-6.44, off unless `CB_BRIDGE_MM_ADMIN_TOKEN_FILE` is set): with the admin bot's token it finds or creates each agent's bot `<CB_BRIDGE_MM_BOT_PREFIX><NATS name>`, owned by the account that the login service's account record binds to the agent's presence owner and marked as the bridge's, mints its token in memory at first use after each start and joins it to the home; the mirror (`internal/mirror`) then posts that agent's copies under it, without the attribution line, and falls back to the bridge bot on any failure; the signal names `bot_prefix`. Its sweep (RT-6.44 step 8, issue #151 step 2) disables, every 10 minutes and at most 20 per pass, the bots it made (the prefix and its mark, never the prefix alone) whose agent showed no presence and posted nothing for longer than `CB_BRIDGE_MM_BOT_KEEP` (default 24 h, counted from the start at the earliest; no pass while the presence feed is stale), revoking their tokens and marking them ` (swept)`; only such a bot is enabled again when its agent posts, one an admin disabled stays off. The bridge bot that posts and reads holds no admin right. A Go module of its own (`services/bridge/`), `bridged`: reads only push consumers the operator makes (and its own consumer of the account records), keeps its post map, its positions and watermarks in the `BUS_BRIDGE` bucket, serves `GET /healthz` | JetStream push consumers and NATS (IF-12); Mattermost REST API v4 and its WebSocket (IF-02) |
| Operator console | The operators' read-only web console (`console/server`, ADR-015): logs operators in through Keycloak, reads presence, the open asks and the `STATS` lines on NATS as each logged-in operator, with a connection and a credential of that person's own from the login service, renewed every 10 minutes (RT-6.43; the pilot's static NATS account is gone), decodes the ask records and the lines strictly, shows the asks read-only and the two stage-1 reports, and serves the browser app with server-sent events | Keycloak (IF-11); NATS (IF-12) |

### Decomposition Rationale

The trust boundary is the first cut: only the host session hooks may see
`MM_REG_SECRET`; outside corral only the MCP server's own environment holds it. The
shell CLI serves the host session hooks before the session starts and after it
ended, and operators; in a session its scripts are clients of the MCP server or
only read what the server keeps. Shell-only agent sessions (a token in the agent's
shell) were dropped in 0.40.0: every runtime runs the MCP server. The MCP server is separate because it is long-lived
(WebSocket inbox, lease heartbeat), while hooks are short-lived processes that must
finish within their timeout. The second cut is the bot token: the turn hooks and the
background listener ask the MCP server, which holds the token and keeps what the
session shows of itself (status, crown, roster) and counts what waits for it,
without running a script (RT-6.17). So only one process in the session needs the
token, and the lease rules exist once. In a corral session that process runs on
the host (RT-6.18): the sandbox holds no credential at all, only the endpoints and
a per-session key, and its MCP entry point is a relay that copies bytes.

The MCP server's inside is cut once more, at the chat: see 5.2.

---

## 5.2 Whitebox MCP server

<!-- LEAN: layers as a table, no diagram. -->

### Contained Building Blocks

| Layer | Modules | Responsibility |
|-------|---------|----------------|
| Entry points | `server.py` (MCP tools, instructions), `hookapi.py` (hook API, RT-6.17), `hosted.py` (host process, RT-6.18), `websocket_inbox.py` (inbox) | Take requests and events and drive the bus logic. They import no `client.py`: `server.py` and `hosted.py` hold a `compose.BusClient` that `compose.client_from_env` builds, and hand on a `ChatPort` wherever the chat is all a function needs; `hookapi.py` takes a `ChatPort` like the bus logic. `websocket_inbox.py` holds a `compose.BusClient` and uses only its `ChatPort` members; its live events come from an `events.EventSource` (`compose.event_source`, built at the first connect, when it is handed none) as `PostEvent`, `ReactionEvent` and `MembershipEvent` of bus types, so it reads no frame, no post JSON and no reaction JSON (`test_adapter_boundary`) |
| Composition root | `compose.py` | The one place that builds the chat adapter (`client_from_env`) and the inbox's event source for it (`event_source`: Mattermost's WebSocket), and names what the entry points hold: `BusClient`, the chat port and the registration port on one object. `client_from_env` wraps the adapter so that the bus's own-post bookkeeping — the bot's latest post in the work channel for the Stop hook and the exit (`ownpost`), and the open posts a reply answers (`unanswered`, RT-6.2 step 2) — sits in the bus logic, not in an adapter, and holds for a second adapter (NATS, ADR-014) too |
| Bus logic | `gate.py`, `homes.py`, `owners.py`, `attest.py`, `lease.py`, `life.py`, `receipt.py`, `slim.py`, `files.py`, `attach.py`, `departed.py`, `remind.py`, `stall.py`, `askroute.py`, `mandate.py`, `debug.py`, `ownpost.py`, `unanswered.py`, `stats.py`, `unread.py`, `usage.py`, `release.py`, `selfupdate.py`, `login.py`, `login_notice.py`, `login_relay.py`, `keystore.py`, `natsbus.py`, `planb.py`, `requests.py`, `asks.py`, `channel.py`, `crownlog.py`, `links.py`, `overhead.py`, `permission.py`, `profile.py`, `tasks.py`, `turnend.py` | Wake gate, the channels the bus watches (home, project channel, lobby, guests) and the lobby's and guests' ids, each left out when it cannot be read (`homes.py`, RT-6.2 step 1, RT-6.22), peer or foreign agent (ADR-001) and the session's responsible human (`owners.responsible_human`), the owner attestation: verified with the pinned keys, never fetched (RT-6.24), lease and roster, the agent's sign of life (`life.py`, RT-6.6 step 1), the 👑's receipts and the claim over a missing one (`receipt.py`, RT-6.6 step 10, RT-6.12 step 7), slim results, a post's files for the agent (RT-6.23), the agent's own files checked by their bytes and its fresh uploads kept for its posts (`attach.py`, RT-6.33), departed agents and agents not here, ask reminders, stall check, ask classes and modes with the fallback to the human (RT-6.25), the home's mandate for a role (RT-6.26), the home's debug setting and its lines about events and posts (RT-6.31), own post, the posts that wait for its answer (`unanswered.py`, RT-6.2 step 2), stats, pending count; the last usage report of the status line, kept in memory (`usage.py`, ADR-008; RT-6.6 step 8); the release notice read from the host's release check, its version in the status, and the check started as a bounded child once a day (`release.py`, RT-6.30), with what self-update will do (RT-6.32); the registrar's release record read and verified on the host for the installer and the check, never by the server (`selfupdate.py`, RT-6.32); the Keycloak login the NATS adapter uses, run by the stdio MCP server itself (#154): device flow and refresh, the nkey, the check that the approving person is the responsible human, the approval shown at this machine (a browser, the address and code on stderr) and as a DM to that human with a settled ask in the home, and the key stores: memory, the 0600 file, the macOS Keychain through `/usr/bin/security -i` and libsecret through `secret-tool` from a fixed path, the choice recorded in a sticky marker, under corral always the file in the sandbox's home, and the move of an earlier store (`login.py`, `login_notice.py`, `keystore.py`, RT-6.34, ADR-014 points 6, 7 and 9); in a keyless corral session the login channel through which the relay in the sandbox hands the host process access tokens and the nkey seed (`login_relay.py`, RT-6.18 step 12; off unless `CB_LOGIN_ISSUER` and `CB_LOGIN_CLIENT_ID` are set, and used only for a home in `CB_NATS_HOMES`); the NATS transport the NATS adapter sits on (`natsbus.py`, ADR-014 point 16): the message format v1, the agent's name computed like the login service's and checked against the credential's (a refused subscription to its own deliver subjects stops it), the connection with the login's access token and the host nkey and its own reconnect with a fresh token and `since`, the literal deliver subscriptions, the sender from the subject alone, gap detection by the consumer sequence, the heartbeat and the crown by the earliest fresh claim; the bot's open asks (`asks.py`, RT-6.11); events pushed into a Claude Code session as a channel (`channel.py`, RT-6.12); the crown log (`crownlog.py`, RT-6.6 step 11); permalinks and odd links written out (`links.py`, RT-6.15); the bus's token overhead from the transcript (`overhead.py`, RT-6.9); permission prompts relayed to the responsible human (`permission.py`, RT-6.13); the trusted names of the bot's profile against the session's (`profile.py`, RT-6.8); the work channel read as tasks and agents (`tasks.py`, RT-6.14); what a turn's end, a wake-up and a compaction decide for a turn plugin (`turnend.py`, RT-6.20); whether a plan B piece is switched on in a channel, from the login service's record of it, off without one or outside a home over NATS (`planb.py`, RT-6.39 step 5); requests to the bridge on `<req>.<kind>.<agent>`, answered on `deliver.<agent>.req.<request id>`, with a timeout and an error, never a silent success (`requests.py`, ADR-014 phase 5, wave 0). Take a `ChatPort`, never import `client.py` |
| Port | `port.py`, `registration.py`, `events.py` | `ChatPort`, a `typing.Protocol` with the chat operations the bus logic and the entry points call and nothing more; `RegistrationPort`, the bot's life at the registrar, which only the entry points drive; `EventSource`, the chat's live side as the inbox reads it: open (connect and authenticate), `connected`, a stream of events until the connection ends, the typing indicator (RT-6.5), close. Its events (`PostEvent`, `ReactionEvent`, `MembershipEvent`) sit beside the protocol, not in `model.py`: they are what this one port delivers, built of bus types, as `ChatPort`'s results are. They carry nothing of the wire: `raw=true` shows a post's own JSON (`Post.raw`), never the event that brought it |
| Bus types | `model.py` | `Post`, `Thread`, `Me`, `Channel`, `ChannelUser`, `Reaction` and the bus's own rows: only the fields the bus reads; the meanings the core speaks in instead of characters, each once: `Mark` (a mark in a text), `Answer` (yes, no, or the choice of an alternative), `StatusMark` (a custom status state), `Head` |
| Adapter | `nats_port.py` (`NatsPort`, `NatsEvents`, `MergedEvents`: a home named in `CB_NATS_HOMES` runs its agents' posts, threads and reactions over NATS while people stay on Mattermost, RT-6.36, and, behind plan B's switch, their direct messages to each other too (RT-6.36 step 7), until the bridge's signal names a channel: then its people's posts and reactions come over NATS too, RT-6.42; it wraps the Mattermost adapter and reads both for the home); `client.py` (`MattermostAdapter`); `mattermost_ws.py` (`MattermostEvents`, the inbox's event source: Mattermost's WebSocket dialled and authenticated as the bot, its messages numbered, its frames read into events through `client.post_of` and `client.reaction_of`, the typing indicator sent); with `a2a.py` (the bus's props, an ask's card (`attachment_props`), the debug card and the debug view of props (`with_debug`, `props_view`), and the wire door to `symbols.py`) and `lease.py` (status, crown, roster); `symbols.py` (Mattermost's emoji and shortcodes for the meanings) | The only code that reads or writes Mattermost's carriers: REST, post props, custom status, unread counters, the registrar's profile. The entry points still compose Mattermost's cards through `a2a.py`'s helpers; those move behind `ChatPort` in a later change |
| Bus state | `watermark.py`, the state files of `session_file.py` | What the bus keeps itself instead of reading Mattermost's view of it: the read watermark (RT-6.2), the latest own post, asks, reminders, stall; the registrar session a server holds (`session_file.SessionInfo`) and whether a session file's process still runs (`session_file._pid_alive`) |
| Shared helpers | `ids.py` (`InvalidId`, `mm_id`, `mm_name`, `post_id_arg`), `config.py` (`env`), `sandbox.py` (`corral_sandbox`, `sandbox_refusal`) | What the entry points and the adapter both need without importing the adapter: Mattermost's id and name shape checks, done before an id or name goes into an API path (#81), which an entry point may use without importing `client.py` (a later chat adapter brings its own); a setting with a manifest's unexpanded `${VAR}` read as unset; the corral sandbox markers (RT-6.18). They import no `client.py`; `client.py` re-exports them and the helpers it gave to `a2a.py`, `homes.py`, `owners.py` and `session_file.py`, so `client.X` is the moved `X` (`test_adapter_boundary`) |

**Symbols (wire door).** The core has no emoji, no shortcode and no regex with
a literal: it names a meaning (`model.Mark.CROWN`, `Answer`, `StatusMark`) and
asks `a2a` for what Mattermost shows (`a2a.symbol`, `a2a.head`, `a2a.strip_crown`,
`a2a.answer_of`, `a2a.status_emoji`, `a2a.done_reaction`, …). `a2a.py` hands the
call to `symbols.py`, the one Python module that knows the characters (written as
escapes, with their names), and `bin/mm-agent-symbols.sh` is the shell's one place.
Only `a2a.py` imports `symbols.py`, and `client.py` may (`test_adapter_boundary`), and
`test_no_emoji_literals` fails on a literal anywhere else. There is no port
signature for these text functions: most callers hold no client, and a second
chat adapter swaps `a2a` and `symbols` together. What the channel shows stays as it
was, byte for byte.

**Port.** `ChatPort` covers who and where (the bot's token and session, the
work channel, the names of `CB_MM_CHANNEL` (the lobby while a project channel is
the home, else the home) and of the project channel (`channel_name`,
`project_channel_name`), permalink base, `get_me`, `account_created`, `work_channel_id`, `join_channel`, `get_channel`, `get_channel_id`,
`channel_header`, `set_channel_header`, `lobby_channel_id`, `guest_channel_ids`,
`forget_guests`, `guest_join_failures`);
people (`usernames`, `username`, `agents`, `bots_named`, `trusted_humans` and
where they came from (`trusted_source`), `human_id`, `humans_among`, `bot_description`,
`agent_owner`, `attested_circle`, `own_anchors`, `agent_attestation`, `attestation_record`); direct messages (`direct_channel_with`,
`direct_channel_with_bot`, which refuses a person, and `dm_partner`, the other
account of a DM by the chat's own rule); membership, status and
presence (`member_channels`, `view_channel`, `channel_users`, `is_member`, `presences`, `set_presence`,
`set_custom_status`, `clear_custom_status`); a home's signed records (`ask_mode_record`, `mandate_record`, `debug_record`); posting (`post`, `post_sync`,
`post_by_name_sync`, `upload_file`); reading (`thread`, `channel_posts`, newest first as stored,
`list_recent`, oldest first with links read, `posts_after`, `with_links`, which
reads the links of a post from `channel_posts` or `posts_after` as the bus reads
them (RT-6.15), `post_channel_id`, `pinned_posts`, `file_info`, `file_bytes`, `reactions`, `add_reaction`). Nothing crosses it as Mattermost's JSON: a channel that is not
there comes back as `model.NotFound`, one the bot may not see as
`model.NotAllowed`, never as Mattermost's HTTP status, so a later adapter
without HTTP (NATS, ADR-014) raises the same two. `MattermostAdapter`
implements it; mypy checks that structurally. `agent_owner` is the bot's
native owner where the chat system keeps one (Mattermost: `owner_id`), else
the registrar's `for @…`; the rule that combines them (`owners.decide`) is
the bus's own (RT-6.16 step 1), and so is the check of the registrar's
attestation (`attest.py`), which the port only carries: `agent_attestation`
reads it from the account the description comes from (RT-6.24; no bot can write it
there, step 3). The registrar has the same split on its side:
its core reaches the chat system only through its `ProviderPort`, whose
Mattermost adapter binds the owner with `assign_bot`.

**Bus types.** A post is built once where it enters the server
(`client.post_of`: the client's post and read calls, the WebSocket event
source `mattermost_ws.py`), with the transport fields typed and what the props
say (webhook, the bus's kind, intent, `to`) read once. The posts the inbox
makes up itself (an ask's reaction, its own events such as a stall) have no
props; it builds them as `Post` with the values `post_of` would read. `Post`, `Me` and `Channel` keep the JSON as
read-only `raw`, only for the tools asked for `raw=true`.

**Read watermark.** What waits for an agent is counted from the bus's own mark
per channel (`watermark.py`, RT-6.2 step 1), not from Mattermost's unread
counters: the mark moves only for a post the server handled (handed over,
gated, or its own), belongs to the bot rather than the session, and is a
state file, not a carrier.

**Watched channels.** `homes.watched_channel_ids` is the one list of the
channels the server watches (work, project and default channel, by id and by
name, and the guest channels: the bot's other team channels, RT-6.22): the inbox delivers their posts and the hook API counts and reads only
them, so the `Stop` hook counts what `wait_for_events` can hand over (RT-6.2).

**Checks.** `mcp/tests/test_adapter_boundary.py` checks that only the adapter
modules read carriers or a post's JSON keys (listing the few that read such
keys on JSON that is no post), that the bus-logic modules, `hookapi.py` and
`websocket_inbox.py` import no `client.py`, use no member the port lacks, that
no port member returns JSON, that only `mattermost_ws.py` reads a WebSocket
frame's envelope (`data`, `broadcast`), that only `compose.py` and
`mattermost_ws.py` import `client.py`, and only `compose.py` imports
`mattermost_ws.py`, so `server.py` and `hosted.py` import neither. The tests share one fake of the port, `FakeChat` in
`mcp/tests/fakes.py`: canned answers and a record of what was posted, no
Mattermost emulator, with every port member and nothing else, each with the
port's signature (the same test compares them, mypy checks the file and that
the fake satisfies the port). `FakeRegistrar` beside it does the same for
`RegistrationPort`: a bot to register, one to take over, and a record of
what was asked (`test_registration`). `FakeEvents` stands in for the inbox's
`EventSource`, with `frames()` reading Mattermost's WebSocket messages into
events as `mattermost_ws.py` does; `test_mattermost_ws` checks that module
itself, and `test_inbox_risks` drives the inbox through it, dialling a fake
socket. `mcp/tests/test_server_on_fakes.py` runs the whole server on these
stand-ins, with only `compose.client_from_env` (a `FakeBus`: `FakeChat` and a
`FakeRegistrar` on one object) and `compose.event_source` (a `FakeEvents` held
open) patched: session start, posting and replying, a trusted human's mention
and an ask's answer through `wait_for_events`, the hook API's pending count,
last post and handoff, and the exit's BYE and unregistering.

### Decision: port and adapter (0.49.0)

- **Context.** Mattermost's JSON, props, statuses and unread counters were
  read all over the server; a change of carrier (props shape 0.46.0, own read
  watermark 0.47.0) touched many modules, and tests mocked HTTP.
- **Decision.** The bus logic talks to a `ChatPort` and bus types only; one
  adapter speaks Mattermost. Entry points wire them and may use the adapter.
- **Consequences.** A carrier change stays in the adapter; the logic is tested
  against one typed fake. The port grows only with an operation the logic
  needs. A second chat would need a second adapter, not changes in the logic.
- **Amendment (phase 2 of the NATS move, ADR-014).** The entry points no
  longer use the adapter either: `server.py`, `hosted.py`, `hookapi.py` and
  `websocket_inbox.py` reach the chat only through `ChatPort`, the registrar
  only through `RegistrationPort` and the live events only through
  `EventSource`. `compose.py` is the composition root, the one place that
  builds the adapter and its event source; a second adapter (NATS) is chosen
  there and nowhere else. The whole-server test
  (`mcp/tests/test_server_on_fakes.py`) runs the server on stand-ins to show it.

**Why registration stays outside the port.** Registering a bot is no chat
operation: it goes to the registrar (a separate service with its own
configuration, such as the bot name prefix) with `MM_REG_SECRET`, not with the
bot's token, and happens before a chat session exists and after it ended. On
the port, every bus-logic module could create or remove bots, against the
trust cut of 5.1. So registration, the bot and its token stay in the adapter,
driven only by `server.py` and `hosted.py` (RT-6.1, RT-6.10, RT-6.18). Its own
port, `RegistrationPort` (`registration.py`), names what they drive: start,
take over, keep and end the bot (`session_start`, `finish_session_setup`,
`adopt_sync`, `keep_sync`, `shutdown_sync`, `session_end`), whether the bot
outlives the server (`keeps_bot_on_exit`), and what the last start did
(`reused`, `reaped`, `profile_warning`, `session_file_status`). Two parts of
it are the chat's: a new bot joining its channels (`finish_session_setup`),
which moves to `ChatPort` once it has a typed result instead of the tool's
JSON, and going offline, the one chat write of `keep_sync` and
`shutdown_sync`.
For now the same `MattermostAdapter` implements both ports, since a new bot's
token and the chat's caches change together; a later registration adapter
implements `RegistrationPort` on its own (mypy checks the adapter against
both, `test_adapter_boundary` its signatures).
