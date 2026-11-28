# Plan B piece 14: registration ends, all or nothing

A plan, not a decision, for the last piece of plan B (#244; #151 step 5). The
piece itself is specified in [ADR-014](arc42/09-architecture-decisions.md),
phase 5 as plan B, item 14 of "The pieces, in order"; this file says how to get
there and what the owner decided (the cutover below, sections 5 and 5a). Written
2026-11-28 against `origin/main` 8b5977f7, amended the same day for the cutover. Where a fact could not be checked in the code or the docs it says
"unverified".

## Starting point

- All thirteen earlier pieces are merged behind switches that are off
  everywhere (CHANGELOG `## Unreleased`), among them the session's side of
  people's history (piece 5b, PR #295, merged as 1ca76c91), its switch-on
  follow-ups (PR #305, merged as 5fa89883) and permission prompts by DM
  (piece 10, PR #300, merged as 81ba3075).
- Today every session registers a Mattermost bot at the registrar, also in a home
  over NATS. Under corral the host's start hook registers it and starts the host
  process that holds its token (the keyless session, RT-6.18); the relay in the
  sandbox only logs in to Keycloak (decision 10, #154). Without corral the MCP
  server registers it itself with `MM_REG_SECRET` (RT-6.10).
- **A gap in the written plan.** ADR-014 says "the start hook decides per session,
  at its start". But the start hook (`adapters/corral/10-mm-session-start.sh`)
  registers before any NATS login, and the plan B switches reach only the MCP
  server with its credential: the trusted-humans piece (piece 4) already found
  that "the start hook cannot know the switch". So the hook cannot decide on its
  own (question 1 below, decided: the host process decides; it goes with 14d in
  the cutover release).

## The cutover (DECIDED)

Decided by the owner on 2026-11-28 16:46 (ask `7htqausiwbbxmjiqmte97f7oic`, "ja,
Cutover"): piece 14 becomes a cutover. The flip of identity and state (14c-2c) and
the removal of the host side (14d) ship in one release, the cutover release. Before
it, in the lab, the login service's directory admin API goes on, every piece is
switched on in every home, and a smoke test runs. Then the owner switches all agents
to that release, the registrar keeps running one day as the way back, and then it is
shut down. This plan and ADR-014 item 14 change accordingly; gates 16 to 21 are
dropped, and the cutover checklist (section 3) takes their place.

The reason: there are no external users, and the owner switches all agents within an
hour. So the earlier plan's migration over several releases, made for hosts that skip
releases, is not needed: a decision release (14a to 14c), a release for the host
side's removal (14d) once every home ran every piece, and the registrar's shutdown after two weeks without
a registration. Where the sections below keep the earlier wording, it is marked.

**The owner's yes for the flip.** The merge of the decision (14a, #313) left a
condition on #244: the owner's yes is needed when the flip (14c-2c) turns
`nobot.CROWN_NEEDS_BOT` and `CB_CROWN_NEEDS_BOT`, since four differences of the
deferred path then reach real sessions: no kept bot on a resume, up to 60 s without
Mattermost routes, an empty bot name in the sandbox, and no unread baseline or idle
status from the hook. The decision ships the flip (14c-2c), so it is the owner's yes
for it; the four differences then hold for every session, and with 14d the deferred
path itself goes (the host process that registers after its own NATS read, answer 1
of section 5): a session then decides only in its MCP server, as without corral
today.

**The removal of the host side (14d) in steps**, from 05e6aa's plan for it and the
coordinator's answers (bus post `n1:ai-agent-bus-dev:1363` and the reply after it):

1. Before the cutover, with no behaviour change: the MCP entry point moves from
   `hosted.main`, every harness's entry point today, to `server.py`.
2. The flip (14c-2c).
3. The MCP server: the relay, `hosted.py`, `selfupdate.py`, the `release.env` part
   of `release.py`, and the `server.py` text. Plus a guard: with `CB_MM_BOT_TOKEN`,
   the relay's environment or `CB_BUS_UNAVAILABLE` set, the server says in one line
   that old corral host hooks are active.
4. The shell: corral's hooks, the installer, the release check, the channel join,
   registration, and their tests.
5. Docs, site, CHANGELOG and version.

Steps 2 to 5 target an integration branch, `cutover/plan-b`, each reviewed as usual;
one final pull request merges it into `main` right before the tag, so `main` stays
releasable until then.

**Stays until the shutdown:** `registration.py`, the registration half of
`client.py`, `attest.py`, `session_file.py`, the sweep (`mm-agent-sweep.sh`),
`mm-agent-unregister.sh` and `sandbox.py`'s block on self-registration. 14d deletes
nothing on the hosts: the host copy (`releases/`), the pinned signers and
`MM_REG_SECRET` stay for the one-day way back.

## The steps, one cutover release and the shutdown

The wave table of #244 already splits the piece; each step is one pull request or
more, each reviewed as usual (answer 8 of section 5). Everything before the flip
ships inert, with every switch off, in the releases before the cutover, 0.105.0 first. The cutover
release carries the flip (14c-2c) and the removal of the host side (14d) together;
the shutdown follows a day after it.

| Step | What it does | Pull request | Release |
|---|---|---|---|
| 14a, the decision | A session whose home, guest channels, lobby and stats channel all run over NATS with every piece on registers no bot and holds no Mattermost token; any channel off: registers as today. Inert until the flip (14c-2c): until then its predicate always names a reason, the crown needs the bot's identity (RT-6.6 step 13), so every session still registers | #313, merged; its follow-up #330, merged (`nobot.decide` answers at once until the flip) | 0.105.0, inert |
| 14b, the release notice | The MCP server reads the release record the bridge signs (`GET …/register/v1/release`, already routed in `services/bridge/cmd/bridged/main.go`) and tells its human; no host release check | #312, merged | 0.105.0, behind `CB_RECORDS_URL` |
| 14c, identity and state | The session's own identity is its NATS name, not the bot's Mattermost id (`_bot_id`, `_bot_name`, `ownpost.py`, `unanswered.py`); the state files still keyed by the bot id move to the name; in the six steps below | | |
| 14c-1, the state keys | Built behind piece 2's switch, RT-6.2 step 9: every per-bot file of the MCP server and the scripts, copied once at the latch, one process per name by `key-<name>.lock`, a restart takes over its predecessor's state by the session's tie (`CB_TIE`) | #328, merged | 0.105.0, behind piece 2's switch |
| 14c-1b, its follow-up | The conditions #244 lists before piece 2 or 14c goes on (step a of the cutover checklist) | #337, merged | 0.105.0 |
| 14c-2a, the identity | Built: every site that decides "is this me, my state, may I act" asks `identity.py`: with a bot the bot's id and username, unchanged, else the NATS name and the bridge's bot of it; the crown's gates take exactly a bot id or a NATS name (`session_file.key_ok`), the home's id and the inbox's events without a bot come from NATS, a guard test lists every read of users/me (RT-6.6 step 13); every session still registers | #333, merged | after 0.105.0, inert |
| 14c-2b, the rest without a token | The hook API, the status and the heartbeat without a token (still behind `client.token` after 14c-2a), with what needs them: `bus_info` (also naming the refused latch beyond its warning), `unread.after`'s `account_created()` and the NATS port's hand-over cursor at its start | not yet | before the cutover, inert |
| 14c-2c, the flip | `nobot.CROWN_NEEDS_BOT` and `CB_CROWN_NEEDS_BOT` (`bin/mm-agent-lib.sh`) deleted in one commit; `nobot.decide`'s early return goes with its docstring sentence and test, and the sentences about it in RT-6.10 step 8 and RT-6.18 step 13 | not yet, on `cutover/plan-b` | **the cutover release** |
| 14c-3, the login approval | Rescoped: the login approval loses its DM and its ask (`login_notice.py`) in a session without a bot; a session with a bot keeps the DM until registration ends | #329, open | before the cutover |
| 14d, the host side goes | corral's hooks, the host process, the relay, the signed host copy and its updater are removed, in the steps of the cutover above | not yet, on `cutover/plan-b` | **the cutover release** |
| Shutdown (phase 6) | A day after the cutover: the registrar off; the registration code leaves the MCP server | not yet | the shutdown release, after that day |

## 1 and 2. What goes, and what replaces it

| What goes | Files | Step | Replaced by |
|---|---|---|---|
| corral's start and end hooks and their dispatcher | `adapters/corral/10-mm-session-start.sh`, `90-mm-session-stop.sh`, `mm-hook-dispatch.sh` | 14d | nothing: the plugin's MCP server in the sandbox starts as without corral; the turn hooks (`hooks/`) stay |
| the host process of the keyless session (RT-6.18, RT-6.1) | `mcp/src/…/hosted.py`, the `--host` entry point | 14d | the MCP server in the sandbox, which already holds the NATS login (decision 10) |
| the relay and its login channel | `hosted.relay`, `login_relay.py`, `login.start_relayed` | 14d | the MCP server's own login (`login.py`, `keystore.py`, RT-6.34), as without corral today |
| the signed host copy, its installer and switch (RT-6.28, RT-6.29) | `bin/mm-agent-host-install.sh` | 14d | the harness's marketplace (ADR-014 piece 14, the owner's decision of 2026-11-27) |
| the host release check and the notice from `release.env` (RT-6.30) | `bin/mm-agent-release-check.sh`, the file part of `release.py` | 14b reads the record, 14d removes the check | the release record the bridge serves (#151 step 4), signed with the bridge's key; the version the console pins after its signature check (#192) |
| self-update between sessions (RT-6.32) | `selfupdate.py` | 14d | nothing: the marketplace updates the plugin |
| the block on self-registration in the sandbox (RT-6.10 step 1) | `sandbox.py` and its callers | shutdown (it stays for the registrar's day, the cutover above) | nothing: no one registers in such a session |
| the channel join in the host hook | `bin/mm-agent-channels.sh` (piece 13 left it, it runs before a server exists) | 14d | channels from the directory (piece 1); the bridge's bot is joined by the bridge (RT-6.44) |
| the login approval's DM and ask (RT-6.34) | `login_notice.py` (`DMNotifier`, the ask and its settling) | 14c | the approval at this machine only: browser, MCP log and the terminal through the harness's hooks (#198, decided 2026-11-27) |
| the bot's identity in the session | `_bot_id`, `_bot_name`, `ownpost.py`, `unanswered.py` | 14c | the NATS name (the roster, piece 2) |
| state files keyed by the bot id | `listen-`, `update-`, `presence-`, `coord-`, `turn-`, the asks, watermark, reminders, gate, stats, crown log (ADR-014 piece 2b "left keyed by the bot id") | 14c | the NATS name; copied once, the source left for the way back, as piece 2b did (built in 14c-1: `nonce-` stays keyed by the bot id, the name is made from it; `listen-`, `update-`, `turn-` follow the key uncopied) |
| the shell clients of registration | `bin/mm-agent-register.sh`, `-unregister.sh`, `-registrar-keys.sh`, the stop hook's unregistration | 14d; `-unregister.sh` at the shutdown (the cutover above) | nothing: gone |
| the host sweep of the registrar's bots | `bin/mm-agent-sweep.sh` | shutdown | the bridge's sweep (RT-6.44 step 8) |
| the mandate's unsigned fallback, the pinned post | `pinned_posts` in `mandate.py` | 14a (needs the signed records from the bridge) | the signed records the bridge serves (#151 step 3) |
| registration in the MCP server and the registrar's attestation | `registration.py`, the registration half of `client.py`, `attest.py`, `session_file.py`, the `MM_*` names of [env-names.md, "Goes with the registrar"](env-names.md#goes-with-the-registrar-10) | shutdown | nothing for NATS sessions; the owner attestation (RT-6.24) is already replaced by the login service's (RT-6.41) |
| the registrar's copied enrollment code in the bridge | `services/bridge/internal/registrar/core/command.go` (enrollment tokens); whether the bridge wires it is unverified | shutdown | nothing: `/agent-token` goes with the Keycloak login (#151) |
| the registration secret on hosts and in the harness plugins | `MM_REG_SECRET`, `_GOPASS`, `_CMD`, `_FD`; `opencode/plugin/…`, `pi/mattermost-agent-bus.ts` | two weeks after the shutdown (decided 2026-11-27) | nothing |

Stays: the turn hooks and the hook API (RT-6.17), the shell clients that ask the
MCP server, the keystore, the bridge's signing key and commands
(`services/bridge/internal/signing`, `commands`, which reuse the copied
`registrar/core`). Docs that change with the steps: RT-6.1, 6.10, 6.17, 6.18,
6.24, 6.28 to 6.32, 6.34 and 6.44 in Section 6; the deployment view; ADR-011
(ends); ADR-014 item 14 (the hook gap above); `adapters/corral.md` and the other
adapters' docs; `docs/env-names.md`; and `site/index.html` (below).

**The website.** Today it promises what goes: "the registrar creates each bot and
signs a claim", the harness matrix rows "Registered by the host", "Token out of
reach", "Secret out of the shell" and "Signed-release updates" (footnotes 1, 6
and 8), the security answer that under corral the agent has no bot token, and the
quick start "Have a Mattermost server and the registrar". With the cutover release a sentence that
a home holds no bot token at all, the matrix loses the
host rows, the quick start names the login service and the bridge, updates go
through the marketplace, and the security answers say the new price: the
Keycloak refresh token and host key are readable in the sandbox (RISK-26), and a
steered agent reaches the sessions that share its plugin tree (RISK-37).

## 3. Gates, in order, and the cutover checklist

What #244's switch-on lists and the ADR's acceptance ask for, ordered so that
each item only needs what is above it. "Before the cutover release" can land with every switch
off; "lab" needs the lab, at the latest with the switch-on of the cutover checklist (step c).
Gates 16 to 21 are dropped (the cutover above); the checklist below the table takes their place.

| # | Gate | Source | When |
|---|---|---|---|
| 1 | piece 5b merged (done: #295, 1ca76c91); its follow-ups fixed (done in #305, 5fa89883: unknown post ids asked at the bridge first, each reply's root checked, own copies recognised after a restart); still open: attachments through the file request | #295, #305, #244 comment on #295 | before the cutover release |
| 2 | piece 10 merged (permission prompts by DM; done: #300, 81ba3075) | #300 | before the cutover release |
| 3 | piece 8: no Mattermost fallback while the bus is down (raise, or document in RT-6.33); a file id no seen post named and files of a DM still take Mattermost: solved or accepted | #286 comment | before the cutover release |
| 4 | piece 7: the synchronous posts (`post_sync`, `post_by_name_sync`, the BYE) moved off Mattermost; a refused reaction told to the agent | ADR-014 item 7 "Built", #294 comment | before the cutover release |
| 5 | piece 11: the inbox's backfill at a Mattermost WebSocket connect, and the WebSocket itself, gone for a fully switched session; an outage longer than the bridge's 10-minute catch-up recovered (history replay or longer catch-up) or accepted | ADR-014 item 11, #293 comment | before the cutover release |
| 6 | piece 4 and 5: the mandate's read of pinned posts and its "no bot" check, the ask mode and debug card reads, off Mattermost | ADR-014 items 4 and 6 | before the cutover release |
| 7 | piece 13: STATS read on NATS (needs the owner's call on the right, RISK-22), the last part no longer posted to the chat | ADR-014 item 13 | before the cutover release |
| 8 | piece 2: the sweep cleans name-keyed files, or the docs name the manual clean-up | #253 comment | before the cutover release (14c adds more such files); **done**: `mm-agent-sweep.sh` removes them by pattern, kept while a running MCP server names it in its endpoint file (`CB_STATE_KEY`, also for sessions without a bot id) or `copied-<name>.env` names a bot with a session (RT-6.1 step 7) |
| 9 | an audit test: every tool, hook API route and shell client in a fully switched session makes zero Mattermost calls (section 6) | ADR-014 "every piece alike" | before the cutover release |
| 10 | lab rights and config: `bridge_members`, `bridge_crown`, `bridge_planb`, `CB_BRIDGE_HOMES` for every switched channel; every channel's `chat_ids` in the directory | #270, #279, #280, #293 comments | lab |
| 11 | piece 6 accepted by the owner: mentions only of bound persons, no fallback while the bridge is down, non-members only logged; the console's ask record takes NATS ids; a reminder test after a restart | #281 comment | lab |
| 12 | piece 9 accepted: a foreign agent can show the crown until the bridge knows memberships and the owner rule; testrig timing of a crown change; a hard stop of the coordinator | #279 comment | lab |
| 13 | piece 5: the tombstone at a revocation, or the owner accepts the 7 h | #270 comment | lab |
| 14 | piece 12: switching transport at a renewal, or the owner accepts that a flag takes effect at the next start | ADR-014 item 12 | lab |
| 15 | the old bridge copies of #297: open asks with a reacted copy asked again after the bridge update | #298 comment | lab |
| 16 | 14c merged (until then 14a's predicate always names a reason, the crown needs the bot's identity, and every session registers; the corral start hook defers none, `CB_CROWN_NEEDS_BOT` in `bin/mm-agent-lib.sh`, which 14c flips in the same commit as `nobot.CROWN_NEEDS_BOT`; 14c also deletes `nobot.decide`'s early return with its docstring sentence and test, and the sentences about it in RT-6.10 step 8 and RT-6.18 step 13); the acceptance: the phase 4 rows again with `testrig`; phase 5's criterion (ask, approval by reaction, mention, file, change of coordinator) with no agent holding a Mattermost token; the identity rows over the bridge (forged verdict, replayed approval and ask, an approval read from history with and without a fresh record) | ADR-014 "Acceptance" | dropped by the cutover (2026-11-28) |
| 17 | the signed records and release record from the bridge, keyed by the bound channel's Mattermost id from the directory | ADR-014 acceptance, #151 steps 3 and 4 | dropped by the cutover (2026-11-28) |
| 18 | the console's check of the release signature (#192, open) | ADR-014 acceptance | dropped by the cutover (2026-11-28) |
| 19 | the offline token's maximum lifetime and DPoP built, or the owner's word to go without (RISK-26) | ADR-014 acceptance | dropped by the cutover (2026-11-28) |
| 20 | every home switched, test channel first, this project's channels last (ADR-014 point 14); every host in each home on the decision release | ADR-014 point 14 | dropped by the cutover (2026-11-28) |
| 21 | the owner's approval of the last piece | ADR-014 acceptance | dropped by the cutover (2026-11-28) |

The issues behind the dropped gates stay open; none of them holds the cutover back.

### The cutover checklist, in order

- **(a) The conditions from #244 fixed** (14c-1b, #337, merged in 0.105.0, so they hold
  before piece 2 is on). The two not to switch on without: the sweep's lock probe
  takes the exclusive lock, so a server starting in that instant stays unlatched; a
  kill between the counter move and the marker strands the counters. The strict
  state key (exactly 26 `[a-z0-9]` or 25 `[a-z2-7]`) refuses a NATS name made with a
  configured `CB_AUTH_NAME_PREFIX`: such a session keeps the bot-id key and never
  latches, which fails safe; a deployment that sets a name prefix widens the key rule
  and the sweep's name pattern first (the lab sets none, #274). The rest:
  [the list on #244](https://github.com/dembaca/ai-agent-bus/issues/244#issuecomment-7241115154).
  Also open there: [how the theft notice reaches a human without a bot](https://github.com/dembaca/ai-agent-bus/issues/244#issuecomment-7239188497).
- **(b) Release 0.105.0** (pieces 1 to 13, 14a, 14b, 14c-1 and its follow-up 14c-1b,
  every switch off) signed; the lab's bridge and login service on images built from
  its main commit, with the NATS rights the newer images' `Needs:` lines name.
- **(c) The lab switched on.** The login service's directory admin API on
  (`CB_AUTH_ADMIN_LISTEN_ADDR` with its certificate, `CB_AUTH_ADMIN_TLS_CERT_FILE` and
  `CB_AUTH_ADMIN_TLS_KEY_FILE`; through `~bgl-infra`, dembaca/bgl-infra#463),
  reachable from the VPN as well as the management networks (the owner's answer of
  2026-11-28 17:02, ask `w6f64z5qc38p5ehdkmhzsmgitw`). Then the one-time import
  (`POST /v1/directory/import`, RT-6.40 step 6), run once on the new login-service
  image, and `CB_AUTH_DIRECTORY_IMPORTED=true` set, before `authd dir planb`. The
  owner decided on 2026-11-28 (ask `6fybr8iemffcibp53j7oh7rfqy`) that the lab's
  import may run with the emergency brake by name: the rename procedure goes into the
  lab runbook (dembaca/bgl-infra#476), and the brake by anchor (#336) comes before the
  bus leaves the lab. Then
  every plan B piece, 1 to 13, switched on in every home's directory record
  (`authd dir planb`, RT-6.40 step 5), with gate 10's rights and config.
- **(d) A smoke test in the lab** on the head of `cutover/plan-b`, for Claude Code,
  OpenCode and pi, under corral too (network to NATS and the login service, a
  writable keystore in the sandbox's home): the login, the crown's election and
  hand-over without a bot, an ask with its answer, a person's history, a file, the
  bridge's copies, the console's views, and a restart that takes over its state.
- **(e) The release cut.** First the registrar's release record frozen at the current
  version, so no old host copy's self-update (`--auto`, RT-6.32) installs the cutover
  release; then the integration branch merged into main; the owner signs and pushes
  the tag.
- **(f) The owner switches all agents** to the cutover release within an hour, with
  `CB_*` setting names only, since the old `MM_*` names go with that release
  (section 4), and removes corral's exec entries (`preStart`, `postEnd`) on each host.
- **(g) The registrar keeps running one day as the way back:** an older release
  still registers. 14d deletes nothing on the hosts (`releases/`, the pinned signers
  and `MM_REG_SECRET` stay), so a host goes back with the plugin version before and,
  under corral, the signed host copy with corral's hooks.
- **(h) Then the shutdown (phase 6):** the registrar off, and the registration code
  removed from the MCP server in the shutdown release (what stays until the shutdown,
  the cutover above); the registration secret goes two weeks after it (sections 1
  and 2).

With all switches off, the releases before the cutover change nothing for anyone: the
decision (14a) skips registration only when every switch is on, and until the flip
(14c-2c) not even then; the release notice (14b) changes its source only with
`CB_RECORDS_URL` set; identity and state (14c-1 to 14c-3) are inert or copy state.
The cutover release removes what a session needs while any piece is off (14d), so the
lab switches every piece on first (step c) and every agent moves to it at once (step f).

## 4. Releases, `Needs:` lines and migration

The `Needs:` lines below pass `NEEDS_PATTERN` (`release.py`, checked). Release
0.105.0 is merged (#338: pieces 1 to 13 and 14a to 14c-1b, every switch off); the
cutover release is 0.106.0 (the owner's decision in its row); the shutdown release's
number is a placeholder. They name no `CB_*` name: a host
before 0.103.0 drops such a line ([env-names.md, the needs rule](env-names.md#the-needs-rule));
the `CB_` names go into the entry's text.

| Release | Content | SemVer | `Needs:` line |
|---|---|---|---|
| 0.105.0 (#338) | pieces 1 to 13, 14a, 14b, 14c-1 and its follow-up 14c-1b, every switch off | as its other content asks; with the switches off nothing changes | none for piece 14 |
| After 0.105.0, in 0.106.0 at the latest | 14c-2a, 14c-2b and 14c-3, inert; the first step of the host side's removal (the MCP entry point); the rest of gates 1 to 9 | as their content asks; with the switches off nothing changes | none for piece 14 |
| Cutover release, 0.106.0 | the flip (14c-2c) and the removal of the host side (14d), merged from `cutover/plan-b`; the old `MM_*` setting names (the fallbacks to `CB_*`, #169) go with it, the owner's decision of 2026-11-28 17:03 (ask `6srsrf7unjf6ixk4i8616w9fzo`) | minor, `### Breaking`: no bot in a fully switched home, corral's hooks and the host copy are gone, an old name alone refuses the start | `Needs: every channel on nats with every piece on, bridge and login service at 0.105.0 or newer, new names set.` |
| Shutdown release, e.g. 0.107.0, after the registrar's day | registration code and registrar names out of the MCP server and the harness plugins | minor, `### Breaking` | `Needs: registrar off, secret kept two weeks, then deleted.` |

**What the hosts do.**

- **Under corral, the cutover release:** the owner removes the `preStart`/`postEnd`
  entries from each host's corral configuration (`adapters/corral.md`) and installs
  the plugin through the marketplace in the sandbox. The login store is already in
  the sandbox's home (RT-6.34 step 8), so no new login. The host copy (`releases/`),
  the host state's `hosted/` directories, the pinned signers and `MM_REG_SECRET`
  stay for the registrar's day; they go after the shutdown. The way back on that
  day: the plugin version before, the signed host copy installed again
  (`mm-agent-host-install.sh --release`, RT-6.29) with corral's hooks; the state
  keyed by the bot id is still there.
- **Without corral (Claude Code on a lab host, Cursor, OpenCode, Codex, pi):** they
  take the cutover release through the marketplace; the MCP server decides whether
  to register (14a) itself after its login, which it already holds (decision 10,
  #154), and in a fully switched home it registers none. After the shutdown and its
  two weeks they delete `MM_REG_SECRET` and its source.
- **Parity:** after the cutover release a corral and a corral-less session are the same code path:
  the MCP server holds the NATS login wherever it runs, nobody holds a bot token.
  Corral then protects the host from the agent, no longer a bus secret from the
  agent (ADR-014, "nothing runs outside the sandbox at the end").

## 5. Questions for the owner (DECIDED)

Decided by the owner on 2026-11-28 08:41, ask qbsb7zr1sbnmx8sm8ca9xto48a: all eight recommendations stand as written.

Read with the cutover (above): where these answers name the decision release, that is
now the releases before the cutover, which carry 14a, 14b and the inert parts of 14c;
the removal release (the old plan's release for the host side's removal, 14d) is now
the cutover release. The host process that answer 1 lets decide goes with 14d in that
release. Answers 5 and 6, which held that release back
for the offline token's maximum lifetime and for the console's signature check
(#192), are superseded with gates 18 and 19: neither holds the cutover back. Answer
8's four pull requests became the steps of the table above.

1. **How does a corral session decide not to register, when its start hook cannot
   see the switches?** (a) The hook starts the host process without a bot, and the
   host process registers only once its own NATS read (it already runs the NATS
   adapter on the relayed login, RT-6.18 step 12) shows a channel or piece off;
   (b) a host setting listing fully switched homes; (c) the relay's word from the
   sandbox. Recommendation, decided: (a). It reads the login service's records, needs no
   host setting that must match, and ADR-014 item 14 is amended to say so.
2. **The stats channel**: does it count as "over NATS" once STATS are written only
   to NATS (piece 13), or only once they are read there too, which needs a STATS
   right that today would show every agent's lines (RISK-22)? Recommendation, decided: once
   written to NATS only; the report reads them through the console or a filtered
   right later.
3. **Which known gaps does the owner accept rather than have fixed before the removal release?**
   Recommendation, decided: fix the two that lose a person's words (the outage beyond the
   bridge's 10-minute catch-up, piece 11; unknown post ids, piece 5b, fixed in #305); accept the
   foreign crown (piece 9), the 7 h of a revoked membership (piece 5) and a flag
   taking effect at the next start (piece 12).
4. **The pinned keys of the release record**, `MM_REGISTRAR_KEYS` today: its new
   name is open (ADR-014, "the hosts' setting for the bridge's address").
   Recommendation, decided: decide it with the decision release, beside `CB_RECORDS_URL`, which the decision release needs.
5. **The offline token's maximum lifetime and DPoP before the removal release?** Recommendation, decided:
   the maximum lifetime before the removal release (a realm setting, after its lab check, RISK-26);
   DPoP may follow.
6. **Must the console's signature check (#192) be built before the decision release or only the removal release?**
   Recommendation, decided: the decision release reads the record as the bridge serves it; the removal release waits for #192.
7. **the decision release's release notice** for hosts that still run the host release check: show
   both until the removal release, or only the record? Recommendation, decided: only the record, so there is
   one source.
8. **Steps as four pull requests** (14a to 14d) plus the shutdown, each reviewed as
   usual? Recommendation, decided: yes; 14a and 14c touch `server.py`, so they are
   serialised.

Later decisions of 2026-11-28 (ask `ky9wn44pwtgftqu5jht99bktxc`, 09:51), on 14b's record:

- **14b is version-only.** The bridge's record as built carries `signers`, `version`,
  `set_at_ms`, `iat` and `exp`, not the `Needs:`/`Withdrawn:` lines; the notice
  therefore names the version and the record, and 14d extends the bridge's record
  with `needs` and `withdrawn` when the host check goes (the verifier already
  accepts them, tested).
- **An exact version.** The record must carry `v<x.y.z>`; `latest` cannot be
  resolved without the host check and gives no notice, so the decision release's
  record names the version.
- **Plain HTTP only to loopback.** No host allow-http setting: a bridge off the
  loopback is reached over `https` (TLS in front of it), as the read implements.

## 5a. Decisions on 14a's own questions (PR #313)

Six questions came with PR #313, the decision (14a). Question 5 was decided by the
owner, the others by the coordinator after Fable's security review (post
n1:ai-agent-bus-dev:890 in `~ai-agent-bus-dev`):

- **Question 5, no late registration: decided by the owner on 2026-11-28 10:00, ask
  x65nr69n7iftbrfxsc7h41eype: until restart.** A session started without a bot never
  registers one later; a session that lost a switch keeps running over NATS and
  needs a restart to register.
- **Questions 1 to 4 and 6: decided by the coordinator.** (1) The deferred path
  (host process registers, not byte-identical to today) is accepted; (2) the
  registration secret goes to the host process on stdin until the decision, for up to
  60 s plus the registration, unreachable from the sandbox (follows from the owner's
  decision on question 1 of section 5); (3) the wait stays 60 s; (4) the strict
  reading stays: every piece in every channel's record; (6) one extra deprecation
  line for a host on the old `MM_NATS_HOMES` is accepted.

## 6. Risks and the test plan

**Risks.**

- **The bridge is the only way to people** (RISK-21, RISK-36): a home over NATS
  never falls back to Mattermost, so a bridge outage cuts its people off from its
  agents until it is back.
- **A shared plugin tree** (RISK-37): without the signed host copy, a steered
  agent can change the code its neighbours' sessions run and hand them a forged
  `from_trusted_human` or `mandated`.
- **The registrar's bots after the switch:** a DM to one or a mention of one reaches
  no session; people must write to the bridge's bot (ADR-014 item 14).
- **Mixed versions:** an older host in a switched home misreads replies and drops
  reactions (piece 7) and new-named agents (#274); ADR-014 point 14 gives no grace
  period. The cutover keeps that window short: the lab's switches go on just before
  the smoke test (checklist step c), and the owner then moves every agent within an
  hour (step f).
- **State moved by key:** a missed file in 14c loses a thread the agent is in, or a
  watermark, so messages come again; the copy leaves the source for the way back.
- **The way back after the cutover release** lasts the registrar's one day: the plugin
  version before on every host, under corral the older signed tag installed again with
  corral's hooks; it is slower than a switch, and after the shutdown it needs the
  registrar on again.

**Tests.**

- **Hook tests** (`tests/hooks/test_session_lifecycle.py`,
  `test_keyless_session.py`): a fully switched session makes no
  `POST /register/v1/agents` and its contribution carries no token; a mixed one
  registers; the cutover release removes the host tests with the code they test
  (`test_host_install*.py`, `test_self_update.py`, `test_release_check.py`,
  `test_sweep.py` at the shutdown).
- **MCP tests** (`mcp/tests/`): a session with every switch on and a Mattermost
  adapter that fails on any call and opens no WebSocket runs every tool, every
  hook API route and the shell clients, against a real `nats-server`; the identity
  is the NATS name; a restart with the state keyed by the name finds its threads.
- **The release notice** from the bridge's record, checked against pinned keys
  (`test_release_record.py`).

**Lab: a test home that registers nothing at the registrar.**

1. A test host whose environment has no registration secret at all, so
   registration cannot happen, with every switch on for its home, its guest
   channel, the lobby and the stats channel.
2. The session starts under corral and without it; resumes; restarts.
3. Over the run: zero `POST /register/v1/agents` from that host in the
   registrar's log, no new bot with the registrar's prefix in Mattermost, and no
   Mattermost request in the MCP server's log.
4. The phase 5 criterion in that home: an ask answered by reply and by 👍, a
   mention, a file each way, a permission prompt by DM, a change of coordinator
   with a hard stop; `testrig` runs with the bridge stopped under load.
5. The mixed case: one switch off, the next start registers as before.
6. The way back: switches off, the session registers again; after the cutover release,
   the older host copy installed again registers while the registrar still runs.

The registrar retires one day after the owner switched every agent (the cutover,
checklist steps g and h), not after #151's two weeks without a registration, which
the cutover supersedes. #151's other conditions stand: every home over NATS, the
bridge serving each of its routes, and the owner's approval of the list of old bots
and tokens; then the steps #151 lists, through `~bgl-infra`.
