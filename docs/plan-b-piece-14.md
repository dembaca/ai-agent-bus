# Plan B piece 14: registration ends, all or nothing

A plan, not a decision, for the last piece of plan B (#244; #151 step 5). The
piece itself is specified in [ADR-014](arc42/09-architecture-decisions.md),
phase 5 as plan B, item 14 of "The pieces, in order"; this file says how to get
there and what the owner decided (section 5). Written 2026-11-28 against `origin/main`
8b5977f7. Where a fact could not be checked in the code or the docs it says
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
  own (question 1 below, decided: the host process decides).

## Four steps and the shutdown

The wave table of #244 already splits the piece. Each step is one pull request.
The three releases are named after what they do: the decision release, the
removal release and the shutdown release.

| Step | What it does | Release |
|---|---|---|
| 14a, the decision | A session whose home, guest channels, lobby and stats channel all run over NATS with every piece on registers no bot and holds no Mattermost token; any channel off: registers as today. Inert until 14c: until then its predicate always names a reason, the crown needs the bot's identity (RT-6.6 step 13), so every session still registers | the decision release |
| 14b, the release notice | The MCP server reads the release record the bridge signs (`GET …/register/v1/release`, already routed in `services/bridge/cmd/bridged/main.go`) and tells its human; no host release check | the decision release |
| 14c, identity and state | The session's own identity is its NATS name, not the bot's Mattermost id (`_bot_id`, `_bot_name`, `ownpost.py`, `unanswered.py`); the state files still keyed by the bot id move to the name (built in 14c-1, behind piece 2's switch, RT-6.2 step 9: every per-bot file of the MCP server and the scripts, copied once at the latch, one process per name by `key-<name>.lock`, a restart takes over its predecessor's state by the session's tie (`CB_TIE`); 14c-2 in three PRs: **14c-2a, the identity, built**: every site that decides "is this me, my state, may I act" asks `identity.py`, the bot's id and username with a bot, unchanged, else the NATS name and the bridge's bot of it, the crown's gates take any state key (`session_file.key_ok`), the home's id and the inbox's events without a bot from NATS, a guard test lists every read of users/me (RT-6.6 step 13); inert, every session still registers; **14c-2b**: the hook API, the status and the heartbeat without a token, `bus_info` naming the refused latch beyond its warning; **14c-2c**: the flip, `nobot.CROWN_NEEDS_BOT` and `CB_CROWN_NEEDS_BOT` deleted in one commit; 14c-3: the login approval); the login approval loses its DM and its ask (`login_notice.py`) | the decision release |
| 14d, the host side goes | Once every home runs every piece: corral's hooks, the host process, the relay, the signed host copy and its updater are removed | the removal release |
| Shutdown (phase 6) | The registrar retires on #151's conditions; the registration code leaves the MCP server | the shutdown release |

## 1 and 2. What goes, and what replaces it

| What goes | Files | Step | Replaced by |
|---|---|---|---|
| corral's start and end hooks and their dispatcher | `adapters/corral/10-mm-session-start.sh`, `90-mm-session-stop.sh`, `mm-hook-dispatch.sh` | 14d | nothing: the plugin's MCP server in the sandbox starts as without corral; the turn hooks (`hooks/`) stay |
| the host process of the keyless session (RT-6.18, RT-6.1) | `mcp/src/…/hosted.py`, the `--host` entry point | 14d | the MCP server in the sandbox, which already holds the NATS login (decision 10) |
| the relay and its login channel | `hosted.relay`, `login_relay.py`, `login.start_relayed` | 14d | the MCP server's own login (`login.py`, `keystore.py`, RT-6.34), as without corral today |
| the signed host copy, its installer and switch (RT-6.28, RT-6.29) | `bin/mm-agent-host-install.sh` | 14d | the harness's marketplace (ADR-014 piece 14, the owner's decision of 2026-11-27) |
| the host release check and the notice from `release.env` (RT-6.30) | `bin/mm-agent-release-check.sh`, the file part of `release.py` | 14b reads the record, 14d removes the check | the release record the bridge serves (#151 step 4), signed with the bridge's key; the version the console pins after its signature check (#192) |
| self-update between sessions (RT-6.32) | `selfupdate.py` | 14d | nothing: the marketplace updates the plugin |
| the block on self-registration in the sandbox (RT-6.10 step 1) | `sandbox.py` and its callers | 14d | nothing: no one registers in such a session |
| the channel join in the host hook | `bin/mm-agent-channels.sh` (piece 13 left it, it runs before a server exists) | 14d | channels from the directory (piece 1); the bridge's bot is joined by the bridge (RT-6.44) |
| the login approval's DM and ask (RT-6.34) | `login_notice.py` (`DMNotifier`, the ask and its settling) | 14c | the approval at this machine only: browser, MCP log and the terminal through the harness's hooks (#198, decided 2026-11-27) |
| the bot's identity in the session | `_bot_id`, `_bot_name`, `ownpost.py`, `unanswered.py` | 14c | the NATS name (the roster, piece 2) |
| state files keyed by the bot id | `listen-`, `update-`, `presence-`, `coord-`, `turn-`, the asks, watermark, reminders, gate, stats, crown log (ADR-014 piece 2b "left keyed by the bot id") | 14c | the NATS name; copied once, the source left for the way back, as piece 2b did (built in 14c-1: `nonce-` stays keyed by the bot id, the name is made from it; `listen-`, `update-`, `turn-` follow the key uncopied) |
| the shell clients of registration | `bin/mm-agent-register.sh`, `-unregister.sh`, `-registrar-keys.sh`, the stop hook's unregistration | 14d | nothing: gone |
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
quick start "Have a Mattermost server and the registrar". With the decision release a sentence that
a fully switched home holds no bot token at all; with the removal release the matrix loses the
host rows, the quick start names the login service and the bridge, updates go
through the marketplace, and the security answers say the new price: the
Keycloak refresh token and host key are readable in the sandbox (RISK-26), and a
steered agent reaches the sessions that share its plugin tree (RISK-37).

## 3. Gates, in order

What #244's switch-on lists and the ADR's acceptance ask for, ordered so that
each item only needs what is above it. "Before the decision release" can land with every switch
off; "lab" needs the switch on in the test home; "before the removal release" needs every home.

| # | Gate | Source | When |
|---|---|---|---|
| 1 | piece 5b merged (done: #295, 1ca76c91); its follow-ups fixed (done in #305, 5fa89883: unknown post ids asked at the bridge first, each reply's root checked, own copies recognised after a restart); still open: attachments through the file request | #295, #305, #244 comment on #295 | before the decision release |
| 2 | piece 10 merged (permission prompts by DM; done: #300, 81ba3075) | #300 | before the decision release |
| 3 | piece 8: no Mattermost fallback while the bus is down (raise, or document in RT-6.33); a file id no seen post named and files of a DM still take Mattermost: solved or accepted | #286 comment | before the decision release |
| 4 | piece 7: the synchronous posts (`post_sync`, `post_by_name_sync`, the BYE) moved off Mattermost; a refused reaction told to the agent | ADR-014 item 7 "Built", #294 comment | before the decision release |
| 5 | piece 11: the inbox's backfill at a Mattermost WebSocket connect, and the WebSocket itself, gone for a fully switched session; an outage longer than the bridge's 10-minute catch-up recovered (history replay or longer catch-up) or accepted | ADR-014 item 11, #293 comment | before the decision release |
| 6 | piece 4 and 5: the mandate's read of pinned posts and its "no bot" check, the ask mode and debug card reads, off Mattermost | ADR-014 items 4 and 6 | before the decision release |
| 7 | piece 13: STATS read on NATS (needs the owner's call on the right, RISK-22), the last part no longer posted to the chat | ADR-014 item 13 | before the decision release |
| 8 | piece 2: the sweep cleans name-keyed files, or the docs name the manual clean-up | #253 comment | before the decision release (14c adds more such files); **done**: `mm-agent-sweep.sh` removes them by pattern, kept while a running MCP server names it in its endpoint file (`CB_STATE_KEY`, also for sessions without a bot id) or `copied-<name>.env` names a bot with a session (RT-6.1 step 7) |
| 9 | an audit test: every tool, hook API route and shell client in a fully switched session makes zero Mattermost calls (section 6) | ADR-014 "every piece alike" | before the decision release |
| 10 | lab rights and config: `bridge_members`, `bridge_crown`, `bridge_planb`, `CB_BRIDGE_HOMES` for every switched channel; every channel's `chat_ids` in the directory | #270, #279, #280, #293 comments | lab |
| 11 | piece 6 accepted by the owner: mentions only of bound persons, no fallback while the bridge is down, non-members only logged; the console's ask record takes NATS ids; a reminder test after a restart | #281 comment | lab |
| 12 | piece 9 accepted: a foreign agent can show the crown until the bridge knows memberships and the owner rule; testrig timing of a crown change; a hard stop of the coordinator | #279 comment | lab |
| 13 | piece 5: the tombstone at a revocation, or the owner accepts the 7 h | #270 comment | lab |
| 14 | piece 12: switching transport at a renewal, or the owner accepts that a flag takes effect at the next start | ADR-014 item 12 | lab |
| 15 | the old bridge copies of #297: open asks with a reacted copy asked again after the bridge update | #298 comment | lab |
| 16 | 14c merged (until then 14a's predicate always names a reason, the crown needs the bot's identity, and every session registers; the corral start hook defers none, `CB_CROWN_NEEDS_BOT` in `bin/mm-agent-lib.sh`, which 14c flips in the same commit as `nobot.CROWN_NEEDS_BOT`; 14c also deletes `nobot.decide`'s early return with its docstring sentence and test, and the sentences about it in RT-6.10 step 8 and RT-6.18 step 13); the acceptance: the phase 4 rows again with `testrig`; phase 5's criterion (ask, approval by reaction, mention, file, change of coordinator) with no agent holding a Mattermost token; the identity rows over the bridge (forged verdict, replayed approval and ask, an approval read from history with and without a fresh record) | ADR-014 "Acceptance" | lab, before the removal release |
| 17 | the signed records and release record from the bridge, keyed by the bound channel's Mattermost id from the directory | ADR-014 acceptance, #151 steps 3 and 4 | before the decision release |
| 18 | the console's check of the release signature (#192, open) | ADR-014 acceptance | before the removal release |
| 19 | the offline token's maximum lifetime and DPoP built, or the owner's word to go without (RISK-26) | ADR-014 acceptance | before the removal release |
| 20 | every home switched, test channel first, this project's channels last (ADR-014 point 14); every host in each home on the decision release | ADR-014 point 14 | before the removal release |
| 21 | the owner's approval of the last piece | ADR-014 acceptance | before the removal release |

With all switches off, the decision release changes nothing for anyone: the decision
(14a) only skips registration when every switch is on, the release notice (14b)
changes its source, identity and state (14c) copies state. Only the removal of the
host side (14d) removes something a session can still need, so it waits for gates 16 to 21.

## 4. Releases, `Needs:` lines and migration

The `Needs:` lines below pass `NEEDS_PATTERN` (`release.py`, checked). The
version numbers assume the next release is 0.105.0 (the Unreleased NATS-name
change, a breaking minor) and are placeholders. They name no `CB_*` name: a host
before 0.103.0 drops such a line ([env-names.md, the needs rule](env-names.md#the-needs-rule)),
and the removal release and the shutdown release are exactly the releases an old host may jump to; the `CB_` names
go into the entry's text.

| Release | Content | SemVer | `Needs:` line |
|---|---|---|---|
| Decision release, e.g. 0.106.0 | 14a, 14b, 14c, gates 1 to 9 and 17 | minor: changed behaviour (no bot in a fully switched session), no Breaking with switches off | `Needs: bridge and login service at 0.106.0 or newer, release record from the bridge.` |
| Removal release, e.g. 0.107.0 | 14d | minor, `### Breaking`: corral's hooks and the host copy are gone | `Needs: every channel on nats with every piece on, bridge at 0.107.0 or newer.` |
| Shutdown release | registration code and registrar names out of the MCP server and the harness plugins | minor, `### Breaking` | `Needs: registrar off, secret kept two weeks, then deleted.` |

**What the hosts do.**

- **Under corral, the decision release:** nothing; a fully switched session simply registers no bot.
  The host copy is still installed and is the way back.
- **Under corral, the removal release:** remove the `preStart`/`postEnd` entries from the corral
  configuration (`adapters/corral.md`), the host copy (`releases/`) and the host
  state's `hosted/` directories; install the plugin through the marketplace in the
  sandbox. The login store is already in the sandbox's home (RT-6.34 step 8), so
  no new login. The way back: the plugin version before, the signed host copy
  installed again (`mm-agent-host-install.sh --release`, RT-6.29) with corral's
  hooks, and the registrar on; the state keyed by the bot id is still there.
- **Without corral (Claude Code on a lab host, Cursor, OpenCode, Codex, pi):** they take the decision
  release through the marketplace; the MCP server decides whether to register (14a)
  itself after its login, which
  it already holds (decision 10, #154). The removal release changes nothing for them. After the
  shutdown and its two weeks they delete `MM_REG_SECRET` and its source.
- **Parity:** after the removal release a corral and a corral-less session are the same code path:
  the MCP server holds the NATS login wherever it runs, nobody holds a bot token.
  Corral then protects the host from the agent, no longer a bus secret from the
  agent (ADR-014, "nothing runs outside the sandbox at the end").

## 5. Questions for the owner (DECIDED)

Decided by the owner on 2026-11-28 08:41, ask qbsb7zr1sbnmx8sm8ca9xto48a: all eight recommendations stand as written.

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
  period, so every host in a home must be on the decision release before its switches go on.
- **State moved by key:** a missed file in 14c loses a thread the agent is in, or a
  watermark, so messages come again; the copy leaves the source for the way back.
- **The way back after the removal release** needs an older signed tag installed again on every
  host; it is slower than a switch.

**Tests.**

- **Hook tests** (`tests/hooks/test_session_lifecycle.py`,
  `test_keyless_session.py`): a fully switched session makes no
  `POST /register/v1/agents` and its contribution carries no token; a mixed one
  registers; the removal release removes the host tests with the code they test
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
6. The way back: switches off, the session registers again; after the removal release in a second
   test home, the older host copy installed again works.

The registrar retires only on #151's conditions: every home over NATS, no host
registered through it for two weeks, the bridge serving each of its routes, and
the owner's approval of the list of old bots and tokens; then the steps #151
lists, through `~bgl-infra`.
