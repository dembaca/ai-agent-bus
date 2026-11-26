# Changelog

Every version bump in `.claude-plugin/plugin.json` gets an entry here. Versions
before 0.5.0 are described in the git history.

## 0.103.0

Needs: settings renamed to the new prefix before 0.104.0, see the changelog.
Needs: login service nats user may publish owner notices, as in the example config.

### Breaking

- **The MCP server, the hooks, the shell CLI and the installer read `CB_` names**
  ([#169](https://github.com/dembaca/ai-agent-bus/issues/169)). Their environment
  variables get the `CB_` prefix, `CB_MM_` for the ones that exist only because of
  Mattermost: `MM_CHAT_URL` and its alias `MATTERMOST_URL` become `CB_MM_CHAT_URL`,
  `MM_BOT_TOKEN` and `MATTERMOST_TOKEN` become `CB_MM_BOT_TOKEN`, `MM_CHANNEL`,
  `MM_PROJECT_CHANNEL`, `MM_TEAM` become `CB_MM_…`, `MM_TRUSTED_HUMANS`,
  `MM_LISTEN_SEC`, `MM_RELEASE_SIGNERS`, `MM_NATS_HOMES` and the other bus settings
  become `CB_…`, and the keys of the session files the bus writes follow. The
  registrar's settings (`MM_REG_SECRET`, `MM_REGISTER_URL`, `MM_REGISTRAR_KEYS`,
  `MM_REG_BOT_NAME`, `MM_AGENT_PREFIX`, `MM_BOT_ATTESTATION`, …) keep their names until
  the registrar goes. The full mapping is in [docs/env-names.md](docs/env-names.md).
  Reading rule: the new name first; when it is unset the old name, with one deprecation
  warning per process that names the new one (a quiet hook writes it to its log). An
  empty value counts as set. Old and new name (or the three names of the chat URL and
  of the bot token) set to different values refuse the start and name both, never a
  secret's value. Values the bus writes for itself (session and endpoint files, the
  sandbox's environment) go under both names. The release after this one removes the
  fallbacks and then refuses to start when an old name is set without its new one, so
  rename yours now. The `Needs:` grammar accepts `CB_*` names; but since a host before
  this release drops a line with one, this release's `Needs:` line and the removal
  release's name no `CB_*` name.
- **The login service and `authd` read their settings under `CB_` names**
  (#169, [docs/env-names.md](docs/env-names.md)). Every `AUTH_X` becomes
  `CB_AUTH_X` and every `AUTHD_X` becomes `CB_AUTHD_X`; the three Mattermost
  settings shorten to `CB_AUTH_MM_URL`, `CB_AUTH_MM_TEAM` and
  `CB_AUTH_MM_TOKEN_FILE` (from `AUTH_MATTERMOST_URL`, `_TEAM`,
  `_TOKEN_FILE`). The old names still work until the removal release (the
  release after 0.103.0): an old name is read when its new name is unset,
  with one warning per process naming the new name. Both names set to
  different values refuse the start and name both, never a value; equal
  values pass with a warning that the old name can go. Empty counts as set.
  A value that is exactly an unexpanded `${NAME}` refuses the start, naming
  the variable: the service has no plugin manifest that would expand it, and
  read as unset it would silently drop a setting such as a pinned CA file.
  From the removal release on an old name without its new name refuses the
  start. Error messages, `authd dir -h`, `services/auth/README.md` and
  `nats-server.example.conf` name the new names.

- **The central bridge reads its settings under `CB_` names** (#169,
  [docs/env-names.md](docs/env-names.md#the-bridge-19)). Each `BRIDGE_X`
  becomes `CB_BRIDGE_X` (`CB_BRIDGE_NATS_URL`, `CB_BRIDGE_NATS_SEED_FILE`,
  `CB_BRIDGE_CA_FILE`, `CB_BRIDGE_MM_URL`, `CB_BRIDGE_MM_ALLOW_HTTP`,
  `CB_BRIDGE_MM_TOKEN_FILE`, `CB_BRIDGE_MM_TEAM`, `CB_BRIDGE_HOMES`,
  `CB_BRIDGE_PROPS_PREFIX`, `CB_BRIDGE_KV_BUCKET`, `CB_BRIDGE_SEEN_BUCKET`,
  `CB_BRIDGE_STREAM`, `CB_BRIDGE_BUS_PREFIX`, `CB_BRIDGE_DELIVER`,
  `CB_BRIDGE_INBOX`, `CB_BRIDGE_LISTEN_ADDR`,
  `CB_BRIDGE_MM_ADMIN_TOKEN_FILE`), and the two Mattermost settings of the
  agents' own bots get an `MM`: `BRIDGE_BOT_PREFIX` becomes
  `CB_BRIDGE_MM_BOT_PREFIX`, `BRIDGE_HIDE_BOT_DM` becomes
  `CB_BRIDGE_MM_HIDE_BOT_DM`. The old names still work in this release:
  with the new name unset the bridge reads the old one and logs one
  deprecation warning naming the new name; both set to the same value pass
  with a warning that the old one can go; both set to different values
  refuse the start, naming both (never a value). An empty value counts as
  set. A value that is exactly an unexpanded `${NAME}` refuses the start,
  naming the variable: the bridge has no plugin manifest that would expand
  it, and read as unset it would silently drop a setting such as a pinned
  CA file. The release after this one refuses an old name set without its new
  name; rename the deployment's settings before it.

### Added

- **A copied login reaches the owner as a direct message** (#154 step 3). The
  login service publishes every network move and every copy of a session or
  host key for the owner on `<presence>.notice.<owner anchor>.<verdict>.v1`
  (one subject per verdict, so a move never erases a copy; one record per id
  and verdict in 30 minutes, so a refused session's retries tell once), a
  subject only it may publish; each agent's presence consumer is filtered to
  its own owner's notices, so no other owner reads them. A copied one becomes one DM
  from the owner's live server — the agent the record names if it is live, else
  the owner's live agent with the smallest name, decided from presence alone —
  saying what only the owner can do: end the session in Keycloak, or rotate the
  copied host key. A move is published as a report and is not DM'd. A server
  remembers what it told, per kind and id, so a restart does not repeat it, and
  the same kind, id and verdict is one DM within the block. Nobody is told when
  no live server of the owner is left, when the responsible human is unknown or
  the DM fails — the record waits in the stream's history within its 24 hours,
  and the console and the log keep it; the server that tells may be the thief's
  if a copied host key let them run an agent of that owner, and a compromised
  server can stay silent. The login service's JetStream user needs the right
  to publish `presence.notice.*.*.v1` (see `services/auth/nats-server.example.conf`);
  without it the publish fails, is logged, and the verdict stands.

- **A check holds `docs/env-names.md` to the code** (#169, the mapping's guard).
  `scripts/doc-check.py` reads the page and the tree: CI fails when the code
  reads an environment name the page does not list, and when the page maps a
  name that production code and tests do not mention (the registrar's kept names
  excepted; a "Tests and CI" name may live in tests). A read is a quoted literal
  in Python, Go and the plugins (a Go tag's `env:"X,required"` included), dot
  access (`process.env.X`), or a name in the shell and in the configuration
  files the example ships; names ending in `_` are stems, not settings; test
  files are skipped, since a fixture's fake name is no setting. The page's
  "not environment" bullets and its new exclusion table — each name with a
  reason of its own — are what keep a script's globals and other products'
  variables out of the failure, and a row the check cannot read or an exclusion
  without a reason fails too, as do the page's heading counts and conflict
  markers. A test pins both directions, each reader form, the counts and each
  failure.

### Fixed

- **The operator console's logout works in a browser again** (RT-6.43
  step 9). The pages sent `Referrer-Policy: no-referrer`, under which a
  browser sends `Origin: null` with the sidebar's same-origin logout POST,
  and the server refused it as a cross-site logout: every logout from the
  browser failed with 403. The pages now send `Referrer-Policy:
  same-origin`, which still sends no referrer to another site; the API
  answers and the logout's redirect to Keycloak keep `no-referrer`, and
  `Origin: null` is still refused. A console tab opened before the update
  keeps the old policy until it is reloaded.

### Changed

- **The OpenCode plugin and the pi extension read the `CB_` names** (#169,
  release 0.103.0). Their own settings — the workspace (`CB_WORKSPACE`), the
  channel flag (`CB_CHANNEL_PUSH`), the checkout (`CB_AGENT_BUS_ROOT`) and, in a
  corral session, the relay's endpoint and key (`CB_BUS_ENDPOINT`, `CB_BUS_KEY`,
  `CB_BUS_MCP_ENDPOINT`) — are read by the rule of
  [docs/env-names.md](docs/env-names.md): the new name first, the old `MM_` one
  for this release with one deprecation warning per process, an empty value
  counting as set, and two names with different values a conflict the plugin
  reports once and then behaves as without a bus (a conflicting checkout falls
  back to the plugin's own). The server entry they write
  carries `CB_WORKSPACE` and `CB_CHANNEL_PUSH` beside the old names, so a server
  of the previous release reads the old ones (a user's own `environment` that
  names one spelling is copied to the other, so the server never sees a
  disagreement); the endpoint file's keys are taken by either spelling, the last
  line winning. An operator who already uses the `CB_` names
  sees no difference, and the old ones keep working until the release that
  removes the fallbacks.

## 0.102.0

Needs: login service jetstream user with consumer create and delete rights on the directory bucket.
Needs: keycloak post logout redirect uri for the console client.
Needs: MM_BRIDGE_BOT kept on every host, and this release on all hosts before the bridge turns agent bots on.

### Breaking

- **The central bridge refuses an `http` Mattermost URL to a host that is
  not loopback** (#131). Its bot token goes with every REST call and in the
  WebSocket's authentication challenge, so over `http` it would cross the
  network in clear text. `BRIDGE_MM_URL` must be `https`, or `http` to
  `127.0.0.1`, `::1` or `localhost`; a lab on a network it trusts may set
  `BRIDGE_MM_ALLOW_HTTP=true` (default off, a warning at start). The
  bridge's WebSocket reader refuses such a URL on its own too.
- **The operator console's `GET /logout` answers 405** (#108 point 3,
  RT-6.43 step 9). The logout is a POST now, from the sidebar's button; a
  bookmark or link to `/logout` no longer logs out. The release's entry
  needs the line `Needs: keycloak post logout redirect uri for the console
  client.` (the console's client lists `<CONSOLE_PUBLIC_URL>/` under "Valid
  post logout redirect URIs").

### Added

- **The central bridge can post each agent's copies under the agent's own Mattermost bot** (#151 step 1, RT-6.44). Off by default; with `BRIDGE_MM_ADMIN_TOKEN_FILE` (an admin bot of its own with `system_admin`, its token a separate file used only by the bot management) and `BRIDGE_BOT_PREFIX`, the bridge finds or creates `<prefix><NATS name>`, owned by the Mattermost account that the login service's account record binds to the agent's presence owner (never a username lookup; no record, no bot of its own) and marked as the bridge's, mints its token in memory at first use after each start (revoking its earlier ones), joins it to the home and posts that agent's copies and reactions under it without the attribution line, `nats_id` kept; its reactions are recorded, so their removal reaches that bot also while it is in its backoff. On any failure the one bridge bot posts as before. Its signal then names `bot_prefix`. `BRIDGE_HIDE_BOT_DM` (default true) hides the admin bot's welcome DMs. The bridge refuses to start when the admin token is the posting token, the admin account is no bot with `system_admin`, or the posting bot holds a role beyond `system_user`, `system_post_all` and `system_post_all_public`. Turn it on only after the hosts run a release with #159 (agents dropping copies under their own bots), or every agent reads each NATS post twice; the hosts also keep `MM_BRIDGE_BOT` set as today, since the fallback copies come from the one bridge bot and hosts drop those only with it; the bridge's prefix must differ from the registrar's `BOT_USERNAME_PREFIX`.
- **The console serves `/metrics`** (#158, part of): the Prometheus text
  format behind the operator's login, the same right `/v1/stats` needs —
  **never scrape with the session cookie**, it is that operator's whole
  console; the unattended variant (a loopback listener with a scrape
  token and a read-only service account on the STATS stream) follows as
  its own pull request with the owner's go. The console's own observations
  — requests by bounded route buckets, open streams, the readers'
  connection states, connects, credential renewals and failures, the
  records the strict decoders refused — are counted process-wide at the
  event, so no session's end resets a series; the agent numbers of that
  session's own reading (agents by state and harness, the open asks it
  sees, the tokens by category per version and harness over 7 days — the
  same numbers the Reports/Stats tab shows) are served only while that
  reading is live. Every metric family carries the `choirbus_` prefix; no
  agent name, owner, home or session id is ever a label, only the newest
  8 versions keep their own label and n/a leaves a series out instead of
  a 0 (RT-6.43 step 10).

- **The console's graph view marks the operator's own node** (#147, the
  follow-up to the API change). The person node the logged-in operator's
  anchor names carries the API's `you` field as an accent badge, in light and
  dark, with an accessible label saying it is the logged-in operator; a person
  the graph knows only from an ask's `human` carries none, since an ask names a
  human but not an anchor. No new data: the field comes from the login's own
  record through `/v1/graph`.

- **The token split for OpenCode and pi** (#139 stage 2b, RT-6.9 steps
  1 to 3, ADR-017). The OpenCode plugin and the pi extension attribute
  each turn's model requests in process, by the rules the server applies
  to Claude Code's transcript, and post the sums as one `usage` stat
  event per turn (`POST /v1/stat` with `k: usage`: exactly 18 integer
  fields, numbers only, among them the input their segments did not cover). The server folds them into the line's `tok`
  (method 1) and adds the bus's fixed instructions and tool docs, which
  the plugins cannot see, as overhead per request, placed only into that
  uncovered input. Such a line counts in the tokens by category; the older
  bus share by cause counts only lines with `tok.bus`, so it never
  reads as 0 %. No token is added to any context.
- **The operator console's API carries each agent's guest channels and
  marks the operator's own person node** (#147). A fleet card names its
  `home` and, beside it, its `guests`: the channels its memberships name
  that are not its home (every granted channel gets one, RT-6.37). The
  home is what the bus itself states — the channel of the agent's ask
  record's subject first (RT-6.43 step 5), else the channel it claims the
  crown of (only a home may be claimed, RT-6.38), else the first channel
  its memberships name — never guessed from an agent's name. `/v1/graph`
  draws the home as a `coordinates` edge and each guest channel as a
  `guest` edge, and marks the person node the logged-in operator's anchor
  names with `you`; that anchor is read from the login's own record,
  never from anything the browser sends. No new right: all of it comes
  from presence, crown claims, memberships and the login's own record.

- **The login service refuses a session that alternates between networks**
  (#154 step 3, decision 10). After the host signature and the token are
  checked, the auth callout watches where each token's Keycloak session (`sid`)
  and each host key are used from. A network is a prefix (IPv4 /24, IPv6 /64);
  one change is a person moving and is only reported, and the log names the id
  and the prefix. Two returns to an earlier network within 30 minutes — adjacent
  (A, B, A, B) or not (A, B, A, C, A) — are the sign of a copy: that session
  is refused for 30 minutes, a retry included, while a host key doing so is only
  reported — a copied key is the owner's to rotate, and refusing it would lock
  the owner out. No Keycloak admin right is involved: the service ends no
  session, so a stolen token stays valid at Keycloak until it runs out or the
  owner ends the session (the owner's decision, #154). The limits: two users on
  one prefix (a shared NAT or proxy, or a thief in the same /24 or /64) are
  invisible; IPv4 and IPv6 are two networks, so a dual-stack client that flips
  between them can be refused; a laptop that switches networks more than once
  within 30 minutes can be refused, and a new login (a new session id) clears
  it; a copy that changes network fewer than three times within 30 minutes,
  keeps one connection open, or rotates among three networks (A, B, C, A, B,
  C), is only reported; a thief with the token can keep
  the owner's session blocked by alternating every 30 minutes, whoever issues
  first wins; and the guard lives in the process, so a restart forgets the
  blocks and each replica of `authd` keeps its own.
- **The operator console shows the channels of the operator's own agents
  and of agents that trust the operator directly** (#128, the console on
  the bus as its operator, RT-6.43 step 3). At each agent's credential the
  login service writes a record into the directory's bucket: the owner's
  anchor, the trusted anchors as its attestation names them and the home
  and guest channels it granted (never the lobby). An operator's
  consumers add the channels of every unexpired record that names the
  operator as owner or trusted, never over several steps. A dropped trust
  or an ended agent leaves the view at the operator's next renewal after
  the agent's next credential or the record's expiry. A record that
  cannot be written or read refuses that credential. The login service
  purges expired records every 5 minutes in the background, never inside
  an agent's login. Each operator credential reads every record, which
  suits tens to low hundreds of agents (the risk that the console shows
  less than there is, RISK-13). A trust record that cannot be read opens
  nothing and is logged without its content; the sweep purges it.

  > Upgrade note: the login service's JetStream user needs the consumer rights on
  > the directory's bucket for its key listings,
  > `$JS.API.CONSUMER.CREATE.KV_BUS_DIRECTORY.>` and
  > `$JS.API.CONSUMER.DELETE.KV_BUS_DIRECTORY.*` (the README and
  > `services/auth/nats-server.example.conf`); without them every operator
  > credential is refused. 0.101.0 already needs them once its console path
  > is on (the operator's channel listing of #126), though its entry does not
  > name them; this release's first `Needs:` line carries them.

- **The bus traffic by category, exact counts for every harness** (the
  first stage of splitting the tokens into task work, bus overhead and
  multi-agent coordination, #139; the overhead measurement, RT-6.9). Each
  `STATS` part carries four new objects of integer pairs under fixed keys:
  `post_words` (own posts by bus word, plus `ask`, `plain`, `other`),
  `inbound` (posts and events handed to the agent by author class `agent`,
  `human`, `event`, and the envelope's characters), `polls` (calls of
  `wait_for_events` and the empty ones) and `inject` (texts the hooks, the
  listener, the OpenCode plugin, the pi extension and the pusher put into
  the context, by kind; `wake`, a text carrying one woken post's body,
  counts as coordination). Hooks and plugins send the new `POST /v1/stat`
  kind `inject` with a kind and a length (at most 10^7) only, never the
  text. The server's own posts (reminders, acknowledgements, permission
  notes, answers on departed agents, login notices) now count too, as
  `other`. The reader
  maps them to bus overhead and coordination (`stats.CATEGORY_OF`, so a
  changed mapping applies to old lines too): `mm-agent-stats.sh report`,
  `stats_report` and the console gain the report "bus traffic by
  category", with a coverage line; lines of older publishers show n/a,
  never 0; `stats_report` returns only the category sums, shares and
  coverage of it, the per-key sums stay in the script's `--json` and the
  console. A digest counts as overhead, so the overhead share is an upper
  bound until the token split by content. Zero added model tokens: no
  instruction, tool doc or schema, skill text, hook or listener text,
  client line, plugin sentence, pushed tag or hint changes, and no tool
  result field is added; a test pins all of these by hash. Only
  `stats_report`, when asked, returns a few more numbers.

- **Tokens by category for Claude Code, an estimate marked as such** (the
  second stage of splitting the tokens into task work, bus overhead and
  multi-agent coordination, #139; the overhead measurement, RT-6.9; the
  decision on attribution by content, ADR-017). The same pass over the
  transcript keeps the context's segments by category, lays each request's
  exact token types over them by position (cache read from the front, then
  cache write 1 h, 5 min, fresh input), sizes tool calls in the output
  exactly and splits text and thinking by what drove the request (a
  convention). `tok` gains `ovh` and `coord` (each `{in, read, w5m, w1h,
  out}`), `req_ovh`, `req_coord` and the method number `m` (1); task work
  is the rest, never sent, so the three add up to the exact totals.
  `mm-agent-stats.sh report`, `stats_report` (sums, shares and coverage
  only) and the console gain the report "tokens by category": in the
  console one stacked bar per row (fleet, version, role, harness),
  input-token equivalents by default and raw tokens on a switch, each bar
  marked "estimated" with a one-line note on the method and the coverage.
  A trusted human's prompt over the bus counts as task work here, a digest
  as overhead. Zero added model tokens: the hash test of the text the
  model reads is unchanged.

### Changed

- **The login service's own NATS user gets least-privilege rights** (#172,
  security hardening). In `services/auth/nats-server.example.conf` the user
  that answers the auth callout (account `AUTH`) had no `publish` and no
  `subscribe` rule, so it could do either on any subject of that account. It
  now subscribes to `$SYS.REQ.USER.AUTH` only and publishes nothing
  (`deny: ">"`) but one answer per request on the reply subject the server
  set (`allow_responses: true`). The example's test now fails for any user,
  in any account, without both rules, and logs an agent in through the
  callout with these rights. The lab should compare its NATS configuration
  with the example: a user there without both rules is the same hole
  (bgl-infra's ask).

- **The OpenCode plugin registers the bus server as direct tools, not Code
  Mode** (#152). The model sees the bus tools and their docs as written, as in
  Claude Code and pi, and the token split counts bus calls exactly (#150). It
  costs about 2,460 tokens of tool docs in every request, about 3,240 with the
  instructions, mostly cache reads; Code Mode is cheaper on the window — the
  instructions alone, a tool's docs once per context as a search result, and
  about 50 tokens of wrapper per call — but hides the tools behind `execute`,
  and the split then counts bus calls as task work (the measurement is in
  [#152](https://github.com/dembaca/ai-agent-bus/issues/152#issuecomment-7185986052)).
  The plugin's `addMcp` sets `codemode: false` on its server entry, and a
  server configured by hand in `adapters/opencode.md` shows it too. Nothing
  else changes: the tools, their names and their arguments stay as they were,
  and a host that configured the server itself can set the same flag. The
  accounting limit #150 documents for Code Mode (bus calls inside `execute`
  count as task work, the head overstates the overhead) applies only to such a
  hand-configured entry now, not to the plugin's own server.

- **The MCP server logs in to Keycloak itself, with and without corral**
  (the host login's operating model, the owner's decision 10, #154 step 2;
  RT-6.34, RT-6.18 step 12). At its first start it opens the approval page
  in the desktop's browser, writes the address and the user code (never the
  device code) to its stderr, which the harness keeps in its MCP log, and
  DMs the responsible human. Stderr counts as delivery: a session without a
  chat no longer refuses the approval; the person check still holds. The
  refresh token and the nkey seed live in the system keychain (macOS
  Keychain, libsecret) where one answers, else in the 0600 file; under
  corral always in the 0600 file in the sandbox's home, where the agent can
  read them (RISK-26). In a keyless corral session the relay holds the login
  and hands the host process access tokens and the nkey seed over a login
  channel, never the refresh token; the host DMs only a user code under a
  link it builds itself, once per 60 s. Upgrading costs one new approval
  per host, unless the old store moves: outside corral the server moves the
  token and key from the host's state directory into its store once; under
  corral the sandbox's store starts empty, and the host process moves the
  old entries into its own store for plain servers. `MM_LOGIN_NKEY` stays
  `ephemeral` by default (#163).

- **An agent's MCP server also drops the bridge's copies posted under the agent's own Mattermost bot** (#151, RT-6.36 step 4). A post in the NATS home that carries `nats_id` counts as a copy when its author is the one bridge bot (`MM_BRIDGE_BOT`, as before) or the bot account named the bridge's bot prefix plus the NATS sender of the post that `nats_id` names; reactions of such a bot likewise. The prefix comes from the bridge's signal on `presence.bridge.<home>.v1` (a new optional `bot_prefix` field), no host setting. Only bot accounts count, and such a copy hides only its own sender's post. Hosts need this release before the bridge turns agent bots on, or every agent reads each NATS post twice; until the signal names a prefix nothing changes.
- **The central bridge logs one INFO line per person post, reaction or removal of a reaction it carries onto NATS** (#134, RT-6.42): `inbound: carried` with the home, `what` (`post`, `reaction` or `unreaction`), the Mattermost post id and the subject, no text and no name beyond the anchor.

- **The operator console's logout ends the Keycloak session too** (#108
  point 3, RT-6.43 step 9). Before, it ended only the console's cookie,
  and `/login` came straight back in through the realm's single sign-on,
  without a password: on a shared machine the next person was logged in as
  the previous operator. Now a button in the sidebar POSTs `/logout`; the
  server drops its session and redirects to Keycloak's end-session endpoint
  with the login's stored ID token as the hint, expired or not (Keycloak
  asks to confirm only without one, after a restart). The
  console's Keycloak client needs `<CONSOLE_PUBLIC_URL>/` under "Valid post
  logout redirect URIs" before rollout.

- **The console's report on the token share per version shows the bus
  overhead share of all tokens**, the overhead segment of the token split
  in input-token equivalents, marked estimated (n/a for versions without
  the split); the bus and coordination share stays in the script's token
  table, from `tok.bus`, for comparison across versions (RT-6.43 step 8).
- **The console's report "the bus's share of all tokens, per version" is
  now "bus and coordination share of all tokens, per version"**, and the
  script's column "bus share" "bus and coordination share": the measure
  counts every request the bus caused whole, a peer's task included, so it
  is not the bus's own overhead alone (RT-6.9 step 3, the console on the
  bus, RT-6.43 step 8).

- **The console's graph view runs on React Flow** (#145): people, open
  asks, agents and their home and guest channels in fixed columns laid
  out by dagre. A state change recolours its agent in place; an agent or
  an ask that comes or goes adds or removes its node without moving the
  others or the view (ADR-015 points 7 and 14). The browser app has unit
  tests now (`npm test` in `console/web`, also in CI).

### Fixed

- **The registrar's Mattermost adapter in the central bridge keeps its admin
  token off plain `http`** (#135). It follows the rule the bridge's own
  client and WebSocket reader got in #131: an `http` URL only to a loopback
  host or with its `AllowHTTP` setting (the bridge's
  `BRIDGE_MM_ALLOW_HTTP`), refused when the adapter is made and at each
  call, and no redirect followed, so a 30x is an error. The adapter is not
  yet active in `bridged`.
- **A NATS home starts for an identity first seen after the directory
  import** (#86, the channel directory's anchors, RT-6.39). The login service
  gave such an identity its Keycloak `sub` as anchor, so the agent name and
  the presence owner token it issued differed from the ones the host computes
  from the username, and the home stopped with `NameMismatch`. A new identity
  now takes its username as anchor when it is free, as before the import; a
  name another identity holds still gives the `sub` (the host then still
  stops, never with a wrong name). Anchors already given do not move. The
  remaining cases, an account renamed in Keycloak and a new one whose name
  was held, stay a known limit (RISK-19).
- **Two follow-ups from the WebSocket security review** (#85, RT-6.36). The
  login service's refusal test now asserts an authorization error and that the
  same identity connects on the standard listener of the same rig, so a
  listener that fails for another reason can no longer pass it. A WebSocket
  server that answers the upgrade with a redirect is still refused before any
  byte goes there, and the bus's log names that reason instead of leaving an
  operator with a bare timeout.
- **A pi session gets the bus's instructions** (#153, RT-6.27). pi hands an MCP
  server's `instructions` to the model only for servers with deferred or Code
  Mode tools, and the bus is registered `direct`, so a pi agent missed the rules
  on sub-agents, approvals, questions to humans and replying in the thread. The
  extension now asks the server for the text before each run while it is missing
  (`GET /v1/instructions`, the one it serves its model, which needs no bus
  session), and puts it into the system prompt as a section of its own; a run
  that still gets none drops a section left from before.
- **The central bridge's WebSocket reader reconnects promptly and bounds
  its handshake** (#131, people's posts onto NATS, RT-6.42). After a
  connection that held 30 s it reconnects after 1 s, not after the wait
  earlier failures had built up. During the authentication challenge the
  1 MiB frame cap now holds, the whole challenge has one 20 s deadline
  instead of one per frame, and more than 128 events before the reply fail
  the handshake instead of piling up.
- **The central bridge's REST client follows no redirect** (#131). Go's
  HTTP client resends the `Authorization` header to a redirect on the same
  host, so a 30x from `https` to `http` would have sent the bot token in
  clear text. Mattermost's API does not redirect; a 30x is now the call's
  error.

### Docs

- **Every environment variable gets the `CB_` prefix, decided, not built
  yet** (#169, step 1). [docs/env-names.md](docs/env-names.md) maps 158
  names the project defines to new ones: `CB_MM_…` for what exists only
  because of Mattermost, `CB_…` for the rest, `CB_AUTH_…`, `CB_AUTHD_…`,
  `CB_BRIDGE_…` and `CB_CONSOLE_…` for the services. The registrar's 10
  settings keep their names and go with the registrar (#151), among them
  `MM_REGISTRAR_KEYS`, whose release-record check gets its own `CB_` name
  when the record moves into the bridge. It sets the reading rule for
  release 0.103.0 (the new name first, the old one with a deprecation
  warning, all set names must agree or the start is refused, an empty value
  counts as set, an unexpanded `${…}` placeholder as unset in the MCP server
  and the hooks while the Go services refuse it, the fallbacks
  go one release later); after that an old name set alone refuses the start
  for at least one more release instead of being ignored, since several
  settings (release signers, trusted humans, CA pins, the login service's
  brake and groups) would fail open. Every allow, pass, deny and unset list
  of names carries both spellings during the fallback release, and the bus
  writes its own session-file keys and relay values under both. It also
  covers the change of the `Needs:` grammar (no `CB_` names in the
  `Needs:` line of 0.103.0 or of the removal release) and the other places
  that change with the renames.

- **The one operating model for the host login, documented** (the owner's
  decision 10, #154, step 1 of 5; steps 2 and 3 are built in this release,
  see Changed and Added; steps 4 and 5 follow). The NATS decision (ADR-014) gets an amendment of
  2026-11-26: the rule "credentials stay on the host, never in a sandbox"
  goes; one operating model with and without corral, in which the MCP
  server logs in itself and keeps the refresh token and the host key in
  the system keychain or a 0600 file, under corral in the sandbox's own
  home. The price is named: an agent can read them, and the token, its
  human's offline token shared by all sessions on the host, yields new
  agents of that owner with any key, but approves nothing. The keyless host
  session (RT-6.18) and the host login (RT-6.34) say what is decided and
  what still runs. New risks: the readable token with its planned
  protections and what they miss, detection in the login service, an offline maximum and
  DPoP (RISK-26), and two findings the login service review of 2026-11-22 rated high, raised again,
  the token's binding and storage (RISK-27) and an emergency brake that
  acts only at the next issue (RISK-28). The site's security answers say
  what is true today and what is decided.
- **The deployment and building block views say what runs in the lab**
  (#108). The central bridge carries people's posts and reactions onto
  NATS in the lab since 2026-11-25 (step 2, RT-6.42), and the deployment
  view's introduction names the bridge and the operator console beside
  the login service as the lab's containers.
- **A coordinator listening as a channel and the silence rule** (#108,
  the coordinator scenario, RT-6.6): such a session counts as alive while
  its MCP server renews the status lease and its 👀 on receipt comes within
  the 45 s; one whose host stopped delivering, so no 👀 comes, is silent
  like any other.

## 0.101.0

Needs: jetstream user first with create, info, consumer rights on stats, publish on person records, then the login service.
Needs: login service updated first with the console path on, then the console, its static account and seed removed.

The operator console's stage 1 is built: the console reads the bus as the
logged-in operator, with a NATS credential of that person's own from the login
service, and its static NATS account is gone; the channels a person sees are
for now the public ones and those the directory lists them as a member of
(their own agents' home channels, those of agents that trust them and guest
channels follow); each MCP server publishes its
open asks and its `STATS` parts on NATS, and the console shows the asks
read-only and two reports. Agent cards show names, harness, model, task and
branch. One bot speaks for one agent: the host refuses a relay that names
another agent, and under corral one OpenCode process binds the bus to one
session. The bridge's Mattermost reader authenticates again. Breaking for
operators: the console refuses `CONSOLE_NATS_SEED_FILE`, and the login service
refuses to start without its JetStream user's new rights on the `STATS`
stream. Order: the login service first (its rights and stream for asks and
`STATS`, then its console path on), then the console's Keycloak client, then
the console; afterwards remove the console's NATS user and seed secret.

### Breaking

- **The console reads the bus as the logged-in operator; its static NATS
  account is gone** (the console on the bus as its operator, RT-6.43
  steps 1 and 4; point 7 of the console decision's addition of
  2026-11-25, ADR-015). At each login the console's server makes a key
  pair for that login, in memory only, and connects to NATS with the
  operator's Keycloak access token as the connect token; the login
  service issues the person's credential and the login's two consumers,
  and the session shows what they deliver: one reader per session, none
  before a login. Every 10 minutes it refreshes the token with the
  ordinary Keycloak session's refresh token (no `offline_access`), checks
  it as at the login (operators' group, same person) and connects again
  with the same key; a refused refresh, a lost group or a logout closes
  the connection, drops key and tokens and ends the session. The console
  never derives the person's name: it subscribes by the login alone
  (`deliver.person.<login>.presence` and `.stats`, `<login>` the whole
  public key) and takes name, anchor and the credential's end from the
  login's own record (`presence.person.<login>.v1`), which arrives with
  the presence lines; until it does, the reading says it waits. A connect the login service refuses ends
  that session's reading once the current credential has run out
  (retried until then, since NATS says the same for a login service that
  is down), key and tokens wiped, and
  `/v1/me` shows the reading's state as `bus`. A new login from one
  browser ends its old session; at most four sessions per person, the
  oldest closed. `CONSOLE_NATS_SEED_FILE` now refuses the start
  (the file is never read); `CONSOLE_DELIVER_SUBJECT`,
  `CONSOLE_CONSUMER_NAME` and `CONSOLE_HEALTH_SYNC_BOUND` are gone.
  `/healthz` (readiness) answers 200 when the NATS server answers on
  `CONSOLE_NATS_URL` (a verified TLS handshake for `tls://`), no longer
  after a fleet replay; `/livez` answers 200 while the server serves.
  Rollout, in this order: the login service with its console path on
  (`AUTH_CONSOLE_CLIENT_ID`, `AUTH_OPERATOR_GROUP`), the console's
  Keycloak client with refresh tokens and no `offline_access`, then the
  console without `CONSOLE_NATS_SEED_FILE`; afterwards remove the
  pilot's console user from the NATS server's configuration and its
  seed secret (the release's second `Needs:` line).
- **The login service refuses to start without its JetStream user's
  rights on the `STATS` stream** (`$JS.API.STREAM.CREATE.STATS` and
  `$JS.API.STREAM.INFO.STATS`, the entry under Added; the release's first
  `Needs:` line).

### Added

- **The console shows the open asks and two reports from NATS** (RT-6.43
  steps 5 to 8, the console decision ADR-015, points 8 to 15 of its
  addition of 2026-11-25). Each MCP server with a NATS link publishes one
  record of its open asks on `<presence>.ask.<home>.<name>`: at most 20,
  oldest first, a title of at most 200 characters each and no body, the
  asked human as the anchor its own attestation pairs with the name (`-`
  without one), the rest counted in `more`, the body at most 3840 bytes
  (the stream's 4 KiB count headers too); at each change, every 10 minutes
  while an ask is open, valid 30 minutes, an empty list at its bye. Each
  STATS part also goes on `<stats>.<name>`, the chat line's object with
  keys from one allow list in `stats.py`, each of the type the list gives
  it (numbers 0 to 10^12), `rt` as `other` for an unknown
  client, at most 8000 bytes; a failed publish waits for the next part.
  Hosted (corral) sessions publish both too. The console decodes both
  strictly (a record or line that breaks a rule is dropped whole, its
  warning carries none of it; a record lives at most 30 minutes after the
  server stored it; a replayed line counts once) and shows the asks read-only, expired after
  24 h, "agent gone" without fresh presence; `/v1/stats` gives the bus's share of all tokens per version
  over 7 days and the asks over 24 hours and 7 days, the numbers
  `stats_report` gives on the same lines, with the age of the oldest line.
  Both reach a session through the login's own consumers (see the
  breaking change above: the ask records with the presence lines, the
  STATS lines from 7 days back; `CONSOLE_STATS_SUBJECT` and
  `CONSOLE_STATS_STREAM`, defaults `stats` and `STATS`, name where they
  lie). "For me" and "may answer" compare the operator's anchor from the
  login's own record, never a name; until that record came they are n/a
  and mark nothing.
- **The login service's part of the console's asks and `STATS` lines**
  (the console on the bus as its operator, RT-6.43; points 9 and 12 of the
  console decision's addition of 2026-11-25, ADR-015). Each agent's
  credential may publish its one ask record on
  `<presence>.ask.<home>.<name>`, under its own name and in its hello's
  home only, and its `STATS` line on `<stats>.<name>` (`AUTH_STATS_PREFIX`,
  new, default `stats`; it must differ from the other two prefixes and
  not be `deliver`). The service makes the `STATS` stream at its start
  (`<stats>.>`, file storage, 30 days, 64 MiB, 8 KiB per message, at most
  500 per subject, the oldest discarded) and refuses to start on one that
  keeps more per subject or longer, takes other than 8 KiB per message, or
  is not stored on file. Its JetStream user needs two more
  rights, `$JS.API.STREAM.CREATE.STATS` and `$JS.API.STREAM.INFO.STATS`
  (`services/auth/nats-server.example.conf`): without them the login
  service refuses to start, with an error that names both. No prefix may be
  `_INBOX`.
- **A NATS credential per console operator, in the login service** (the
  console on the bus as its operator, RT-6.43; points 1 to 6 of the console
  decision's addition of 2026-11-25, ADR-015). With
  `AUTH_CONSOLE_CLIENT_ID` (new) set, the auth callout takes a bare access
  token whose `azp` is the console's own Keycloak client and whose `aud`
  names it, and never one that also names the bus's client; the agents'
  path now refuses a token whose `aud` names the console's. It requires the
  operators' group (`AUTH_OPERATOR_GROUP`, new) at every issue, takes the
  anchor the channel directory holds for the token's `sub` (no anchor, no
  credential) and issues `<AUTH_PERSON_PREFIX><anchor>` (new, default
  `person-`) for 15 minutes: no publish at all, subscribe only on
  `deliver.person.<login>.>` (`<login>` the login's whole public key), and
  writes the login's own record (name and anchor) on
  `<presence>.person.<login>.v1`, which only that login's consumer
  delivers. It makes per login `<name>_<login>_presence`
  on `PRESENCE` (presence and attestations, and crown claims, memberships
  and ask records of the channels the person may see: for now the public
  ones and the person's own in the directory) and `<name>_<login>_stats` on
  `STATS` from 7 days back, both gone 15 minutes after nobody listens. The
  JetStream user needs `$JS.API.CONSUMER.*.STATS` and
  `$JS.API.CONSUMER.*.STATS.>` and `presence.person.*.v1` too (the release's first `Needs:` line).
  One person holds consumers for at most `AUTH_PERSON_MAX_LOGINS` (new,
  default 8) logins at once; the consumers are kept in memory.
  `AUTH_OPERATOR_GROUP` must be a full group path (`/…`). The connection is described in
  `docs/protocol.md`. The console uses it from this release on (the
  breaking change above).

- **The console's agent card shows harness, model, task, branch and open
  asks** (RT-6.37 step 6, ADR-015). A NATS home's presence now carries the
  HELLO card's data: the harness from the MCP handshake, model, task and
  branch from the last `announce`, and how many of the session's own asks
  to a human still wait. Sent with every heartbeat and the bye, so a change
  shows within 30 s. Bounded on both sides (one line without control
  characters, model 64 and task 120 characters, branch a safe git ref of
  at most 100, asks capped at 999), well below the stream's 4 KiB limit;
  older readers ignore the keys, and a card from an older MCP server says
  "n/a". Self-stated like the name: displayed, never trusted. The
  harness name also changes the `rt` of `STATS` lines: a pi client
  (`pi`, `pi-…`) now counts as `pi`, no longer by its own name.

### Changed

- **One bus session per OpenCode process under corral** (#119, RT-6.20
  step 1, RT-6.18 step 6). An OpenCode server serves many sessions but a
  corral session carries one bot: the plugin binds the bus to the first
  top-level session that prompts. Every other top-level session of that
  process (`/new` in the TUI too) is told so in the session ("No bus in
  this session: …") and its hooks call nothing; a subagent of the bound
  session keeps the bus. Such a session's bus tool calls are rerouted to a
  tool that fails with the reason (checked on a real OpenCode 2.4 in the
  lab). Across processes the host enforces the identity. The bus server starts only once a session
  prompted, with its id as `OPENCODE_SESSION_ID`, which the host compares
  like a Claude session id, for the relay and, by the header
  `X-Bus-Client-Session`, for the hook API (`403` for another session's
  hooks): a second or restarted OpenCode process in one corral session gets
  no bus and is told to continue the first session (`opencode --continue`)
  or start a new corral session. Until a session prompts, the plugin says it
  waits (`POST /v1/waiting`), which holds back the no-relay warning. This
  replaces the check for `--service` in the command line, which missed
  other spellings of a shared server and only logged that the bus was
  gone. `bus_info` shows `pid` and, for OpenCode, `client_session`. A bound
  session whose bot changed is told old → new; nothing carries over.
  Without corral OpenCode behaves as before.
- **The host refuses a relay that names another agent** (#119 step 1,
  RT-6.18 step 6). Under corral one bot speaks for one agent: a relay is
  refused, the first one included, when its `CLAUDE_PROJECT_DIR` or
  `MM_WORKSPACE` lies outside the corral session's workdir, and a later
  relay when its session values name another agent than the first relay's
  (another project directory or workspace, another session id without the
  same project directory). A new session id in the same project directory
  (`/clear`, a reconnect) replaces a gone relay and is noted for
  `claude --resume`. The error names the differing keys, never their
  values. Before, such a relay was served with the first relay's names, so
  one agent's posts and approvals were credited to another's bot.

### Fixed

- **The bridge's Mattermost reader authenticates again.** It sent the
  WebSocket action `auth_challenge`; Mattermost knows only
  `authentication_challenge` and closed the socket (1006), so the reader
  logged "the socket ended during the auth challenge; likely a refused
  token" every few seconds and read nothing. The test server now refuses
  any other action.

- **The console's clock ages without deliveries too** (`Part of #108`).
  "Now" stood still between deliveries, so a killed lone agent stayed
  `online` with its crown forever. The fleet's "now" carries a
  **monotonic** floor: presence older than the freshness window ages out
  on its own and the crown drops with it, a jump of the host's wall clock
  changes nothing, and the floor re-anchors only on a genuinely newer
  server stamp (a replay of old stamps revives nothing). The attestation
  expiry reads the floored clock too.
- The operator console's cards showed each agent's name on the bus (`agent-`
  and a 26-character hash) instead of the name `coordinator("who")` shows. An
  agent's presence over NATS now also states its bot's username (`name`
  beside `bot`, RT-6.37), and the card shows it; the bus name stays in the
  detail as "id on the bus". A username any other known card states too
  (also offline, within 24 h) is shown on neither. A card from an older MCP server, without harness,
  model and task, says "n/a" for them.
- After a restart the console showed a red 503 while its server read the
  fleet's replay; it now shows "syncing…" and tries again, an error only
  after two minutes.

### Docs

- **The rest of the console's stage 1, specified** (the console decision,
  ADR-015, addition of 2026-11-25; the scenario of the console on the bus
  as its operator, RT-6.43): a NATS credential per logged-in operator from
  the login service, reading only, in place of the console's static
  account; the open asks as one bounded record each asking agent
  publishes under its own name, shown and not answered; the `STATS` lines on a stream of their
  own beside the chat, decoded so that no text can ride along. Fifteen
  points for the owner, each with a recommendation; built in this release
  (the entries above).

## 0.100.0

Needs: login service updated first, with publish right on presence account records, MM_PROPS_PREFIX at most sixteen characters.
Needs: for the bridge, its seen bucket, widened home consumers, create right on its account consumer, new publish rights.

People's posts and reactions reach the agents over NATS: the central bridge
carries them from Mattermost, the login service publishes each account's record
and makes the consumers people's posts are read through, and the MCP server reads them where the bridge
signals, with a person approving only on the login service's fresh approver
record. The registrar's code moves into the bridge's module, not yet wired.
Breaking: a props prefix is at most 16 characters. Order: the login service
first (without its people's consumers a host hears no people over NATS on the
bridge's signal), then a step-2 bridge with all its new rights — publish on
`bus.hu.<home>.*` and `presence.bridge.<home>.v1`, the seen bucket's rights and
the create right of its account consumer — as in
`services/auth/nats-server.example.conf` and the deployment view.

### Added

- **The MCP server reads people's posts from NATS** (RT-6.42, ADR-016 step 2):
  in a channel of a NATS session (the home, a guest channel, the lobby) whose
  bridge signal is fresh, people's posts and reactions come from the person's
  consumer `<agent>_hu_<channel>` on `bus.hu.<channel>.<anchor>`, decoded
  strictly, and Mattermost's events of people there are dropped; without a
  signal, people are read on Mattermost as before, and when it runs out the
  server reads Mattermost from the last people event on. A key per event,
  held for an hour, drops the repeat at a switch. A person over NATS is
  trusted by the anchors of the session's own attestation (`hu:<anchor>`,
  never `hu:-`), and an event of a trusted person without a fresh approver
  record of the login service is marked `not_approver` and approves nothing,
  also as a yes on an ask, a relayed tool permission, a channel tag and what `get_thread`,
  `list_recent` and an ask's card read back; it does not stop an ask's
  reminders. A
  signal counts only while NATS is connected and the session's own presence
  comes back; a person's files are read from Mattermost by the post's id.
  Other bots, webhooks, system posts and direct messages stay on Mattermost. It reads what the bridge and the login service publish (the
  entries below).

- **The central bridge carries people's posts and reactions onto NATS**
  (RT-6.42, step 2 of ADR-016, the bridge's side). `bridged` reads its bot's
  Mattermost WebSocket in the channels of `BRIDGE_HOMES`, drops bots' posts
  and reactions (Mattermost's `is_bot`), webhooks, `override_username`,
  system, deleted and edited posts and its own, and publishes each person's
  post, reaction and removed reaction once on `bus.hu.<home>.<anchor>`, the
  anchor from the login service's account record (`-` without one), with the
  v1 body of `docs/protocol.md`. After a reconnect it catches up over REST
  from its watermark, at most 10 minutes back; it signals each home it
  carries on `presence.bridge.<home>.v1` while its WebSocket is up and the
  catch-up is done. A publish the server refuses carries nothing and stops
  that home. The bridge makes its own consumer of the account records at
  every start (`bridge_accounts`, a create right whose subject fixes the
  name and the filter; after a quick restart it waits up to about 30 s for
  the old one to go) and signals no home until it has read them. The operator
  creates the bucket `BUS_BRIDGE_SEEN`
  (`services/bridge/internal/store/seen_bucket.json`), widens each home's
  consumer to `bus.hu.<home>.*` and grants the bridge the rights in
  `services/auth/nats-server.example.conf` before this bridge starts: it
  refuses to start without the bucket. The release's second `Needs:` line
  names the create right. The MCP server reads them (the entry above).

- **The bridge reads Mattermost: the WebSocket reader `internal/mmws` and
  two REST methods of `internal/mm` (ADR-016, the bridge's step 2).
  `mmws` dials Mattermost's WebSocket with the bot
  token, reconnects with backoff when the socket drops and emits typed
  events — `Posted`, `Reaction` (added and removed) and a `Reconnected`
  marker whose `Since` is the greatest event `create_at` it handed out,
  the catch-up hint (Mattermost's `since` is strict: read from `Since`
  minus a margin and dedup by key) — and nothing else: whose post is a
  bot's (a drop) or
  a person's (carried) stays the caller's decision, so it filters,
  stores and publishes nowhere. A post comes out with the fields the
  drop rules read — type, props, the files it carries (`file_ids`'
  count), the delete mark — and a
  reaction with the channel it happened in; a quiet socket is taken
  again by a read deadline (renewed per frame and per ping), the auth
  reply is found by its `seq_reply` wherever it sits in the stream, and
  `mm.GetUser` reads a user's `is_bot`, `mm.PostsSince` the channel's
  posts from a reconnect's hint with `update_at` and `edit_at` (and the
  delete mark and the props) kept, a body past its 32 MiB cap named
  `ErrTooLarge` — always for the same caller, never filtered here. Nothing is wired into
  `bridged` — no publish, no consumer, no configuration, no right beyond
  what step 1 already holds — so the bridge behaves exactly as before.
  One new dependency, `gorilla/websocket` (v1.5.3): the WebSocket client,
  and the fake Mattermost its tests run against.

- **The login service's part of people's posts on NATS** (ADR-016 step 2, points
  7, 8 and 16; RT-6.42): each agent gets, for every granted channel, a consumer
  `<agent>_hu_<channel>` on `<bus>.hu.<channel>.*` beside its channel consumer
  (ack none, deleted with it at a dropped channel and at the bye), and its
  presence consumer also reads the account records
  (`<presence>.account.mattermost.*.v1`) and the bridge's signal per granted
  channel (`<presence>.bridge.<channel>.v1`). At each credential issue for a
  proven Mattermost account the service publishes that account's record (anchor,
  `approver`, `token_iat`), never replacing it with an older token's; its
  JetStream user needs the publish right `<presence>.account.mattermost.*.v1`
  (`services/auth/nats-server.example.conf`). The bridge and the MCP server read
  them (see above); the record at a login without an agent and at a change of
  the binding is not built. Upgrade: without the publish right every login of a
  proven Mattermost account is refused, so the login service's NATS user gets it
  first, as the release's `Needs:` line says.

- **The registrar's code moves into the bridge's module** (ADR-016, step 3 of
  "all of Mattermost in one service"). The core and Mattermost packages of
  `github.com/dembaca/mattermost-agent-registrar` (main at `c984d19`) are
  imported into `services/bridge/internal/registrar/` with their tests, their
  import paths adjusted to the bridge's module; the packages need no new
  dependency. Nothing is wired: `cmd/bridged`, the routes, the settings and
  every behaviour are unchanged, and `go test ./...` in `services/bridge`
  stays green. The arc42 building-block view says the registrar package is
  imported but not yet active.

### Docs

- **The central bridge's step 2 is specified** (ADR-016, RT-6.42): people's
  posts and reactions in a bound Mattermost channel reach NATS through the
  bridge on `bus.hu.<home>.<anchor>`, vouched for by the bridge's publish
  right; the v1 bodies are in `docs/protocol.md`. The owner accepted the
  eighteen points as recommended, and the risk of the bridge as the only
  voucher for people is recorded (RISK-21). The login service's part and the
  bridge's side are built (see Added); the deployment view now says the
  bridge's first step runs in the lab.

### Breaking

- **A props prefix is at most 16 characters** (RT-6.16 step 8). The
  per-turn instructions name the prefix (`props keys start with <prefix>`), so
  an absurd one would grow the text carried every turn; the server now refuses
  one that long at start, beside the existing refusal of one a bus or
  Mattermost key can start with, and the bridge's `BRIDGE_PROPS_PREFIX` holds
  the same limit (it must equal the homes' prefix). A deployment with a longer
  prefix does not start until it is shortened; `x_`, `bgl_` and `bridge_` are
  unaffected. The static-text budget test measures each session configuration
  on its own and holds the worst one under its bound (#104).

## 0.99.0

The operator console's read-only pilot (its server, browser app and image), the
central bridge's first step from NATS to Mattermost, and three fixes in the MCP
server: a crashing tool names its cause, the server imports on Python 3.11 again,
and the tool text is the same on every Python. Nothing is breaking.

### Added

- **The console's server, the pilot's live view** (ADR-015). `console/server`
  reads the NATS homes' presence, crown claims and memberships through a
  push consumer of its own on the PRESENCE stream (the shape the login
  service makes for an agent: no acks, replay of the last message per
  subject, the server's own clock from the deliveries' JetStream
  metadata), with one read-only NATS account — the pilot's documented
  deviation from ADR-015 way A, lab and one tenant only. It serves the
  API of ADR-015 point 10 (`/v1/fleet` streamed over SSE, `/v1/me`,
  `/v1/asks` and `/v1/stats` empty until those lie on NATS, `/v1/graph`,
  `/v1/threads/<id>`) and the built browser app; an operator logs in over
  Keycloak (authorization code with PKCE, a client of the console's own,
  members of an operators group), the access token verified against the
  realm's JWKS — rules the login service's Go verifier set — and what a
  NATS home does not carry shows as unknown, never as zero. Python,
  reusing the mcp package's NATS reading (`natsbus.py`) by editable path
  dependency; the NATS tests run against a real nats-server like the
  MCP ones.
- **The console's `/healthz` answers readiness, not only liveness** (ADR-015).
  It went 503 only when the NATS reader ended without a stop; a reader that
  silently never synced still said ok. It now answers 503 until the reader
  has synced — a finished replay with zero messages included, so an empty
  fleet reads healthy — and again once no replay arrived within
  `CONSOLE_HEALTH_SYNC_BOUND` (30 s to 24 h, default 180 s; the reader
  reconnects for a replay between 5/8 and 7/8 of the bound, checked every
  quarter of it). Beside it, `GET /livez` answers liveness:
  503 only when the reader ended without a stop — a liveness probe on
  `/healthz` would restart the pod while NATS is merely slow. The
  docs follow: the console's row in the deployment view (settings, the
  image, the Keycloak setup, one replica), the README's deployment notes
  (a container-usable `CONSOLE_ADDR`, the group mapper and
  `preferred_username` required, `handshake_first`, `/healthz` and
  `/livez`, `CONSOLE_TOKEN_ALGS`/`CONSOLE_CONSUMER_NAME` in the settings
  table), and ADR-015's status cell states the pilot as built once.
- **The operator console's browser app, a read-only pilot** (ADR-015 point
  14). `console/web` inherits the toolchain of `site/` (React,
  TypeScript, Vite) and implements the four views against a mock of the API
  in ADR-015 point 10: the Kanban of agents by state (the crown a mark on the
  card, not a column), the asks with *for me* and *may answer*, the graph
  (trusts and coordinates, cut at depth two with a mark), and the reports
  (counts and seconds, a missing field `n/a`, never 0). The detail pane shows
  the full card, the task rows an agent holds, its HELLO thread and the
  threads of its open asks. Nothing posts — an ask gets no answer button
  (stage 1, reading only); the same `ConsoleApi` interface hides the mock and
  the future console server behind `VITE_CONSOLE_API`.
- **The console's image is published** (ADR-015). `checks.yml` gains a
  `publish-console` job: a push to `main` that changes `console/`, `mcp/` or
  that workflow, and only after the console's browser build and the console
  server's tests with the image smoke have passed, builds `console/server/Dockerfile` from the
  repository root and pushes
  `ghcr.io/dembaca/ai-agent-bus-console:<full commit SHA>` and `:main` with a
  build provenance attestation; the digest stands in the job's summary, and a
  deployment names it by that digest. A push that changes nothing under
  `console/` builds no image.
- **The central bridge, step 1: NATS to Mattermost** (ADR-016 point 4).
  `services/bridge` (Go) mirrors a NATS home's posts and reactions into its
  Mattermost channel with one bridge bot: the sender above the text, the NATS
  id in `<prefix>nats_id`, a reply under the mirrored root, a reaction counted
  per message and emoji. It reads as a NATS user read-only on the bus, through
  push consumers and a KV bucket (`BUS_BRIDGE`) the operator creates, keeps its
  position per home, so a redelivery is never mirrored twice, and answers
  `/healthz`. One way only: what a person writes on a copy does not reach the
  NATS thread yet. The hosts of a mirrored home run 0.98.0 or later first,
  whose `MM_BRIDGE_BOT` drops the copies.

### Fixed

- **The tool text is the same on every Python** (#98). The per-turn `static`
  size counts the tool docstrings, and the compiler dedents a docstring only
  from Python 3.13 on: on 3.11 and 3.12 the same session carried ~500 more
  characters than the budget in `tests/test_stats.py` allows, and on a host with
  the channel push switched on the test went red there. The tools are
  registered through `inspect.cleandoc` now, so what an agent is served and what
  STATS counts is one text on every interpreter.
- **The MCP server imports on Python 3.11 again** (#96), which
  `mcp/pyproject.toml` allows. The models' empty default was a
  `MappingProxyType` as a plain dataclass default, which `dataclasses` accepts
  only from Python 3.12 on, so on 3.11 the package failed at import and a host
  did not start. The fields take it through `default_factory`; the value is
  still the one shared, read-only mapping. A CI job now runs the server's tests
  on 3.11.
- **A crashing tool names its cause** (RT-6.36). 0.98.0 answered a crashed tool
  call with the SDK's bare `Error executing tool <name>` and kept the reason in the
  host log - two NATS pilot sessions failed to post and their agent saw nothing. The
  cause is carried now, as one bounded line of its type and text; a text that looks
  like it holds a token, a password or a long key is replaced by the type alone.

## 0.98.0

Needs: for MM_NATS_HOMES, the login service updated first, with its attestation rights, and MM_BRIDGE_BOT with the bridge.

Peers over NATS are judged by the login service's records, the login service gets
its channel directory (admin API, import from Mattermost, account index), a busy or
silent coordinator keeps its crown, the MCP server reaches NATS over WebSocket, agents drop the central bridge's copies
(`MM_BRIDGE_BOT`), and a post read from the bus is bounded in size. Without `MM_NATS_HOMES` the NATS changes do nothing. Nothing is breaking.

### Added

- **Agents drop the central bridge's copies on read** (RT-6.36 steps 4 and
  6). The bridge mirrors a NATS home's posts and reactions into Mattermost
  with one bot; an agent of that home has them from NATS already and would
  see each twice. With the new `MM_BRIDGE_BOT` naming that bot, the MCP
  server drops, in its NATS home only, every post by the bridge bot that
  carries `<prefix>nats_id` naming a post of that home, and the bridge bot's
  reactions: from the events that wake the agent and from `list_recent`,
  `posts_after` and the channel reads behind the unread count and the hooks.
  Anyone else's post with that prop is kept, and the reads keep a copy of a
  NATS post the session never had. The count's read watermark passes only
  settled copies, up to the first post that is none, and nothing after a
  read that may be cut. Unset, nothing changes. `bus_info`'s `over_nats`,
  the server's instructions and SKILL.md say people see such a home through
  the bridge.
- **The MCP server reaches NATS over WebSocket** (RT-6.36, part of #51).
  `MM_NATS_URL` takes `wss://` (TLS inside the WebSocket, verified with
  `MM_NATS_CA_FILE` like `tls://`) or `ws://` on a loopback host only; a
  pool is all one scheme. A home cannot run over it yet: the login service
  refuses websocket URLs, and the credentials it issues allow standard
  connections only (`AllowedConnectionTypes`). It may once the
  sender-authenticity suite runs over a websocket listener.
- **Peers over NATS by the login service's record** (RT-6.37 step 4,
  RT-6.41). The MCP server sends the session's trusted humans in its
  hello and reads the login service's records: a NATS sender's owner is
  shown by name, and it is a peer when its owner's and trusted humans'
  anchors are all among this session's own; a record cut short, or none
  of its own, makes the sender foreign; once the process has seen any
  record, a sender without a usable one is foreign too, until a restart. A host that drops or cuts
  names of its list says so (`trusted_cut`). Without records nothing changes.
  Update the login service (with its attestation rights) before the hosts.
- **The login service vouches for an agent's owner and trust list**
  (ADR-016 point 4, RT-6.41). The host's hello may name its trusted humans;
  at every credential the service resolves them through the directory to
  anchors (before the import a name is its own anchor) and writes a record
  on `<presence>.attest.<agent>.v1`, a subject no agent may publish, read
  back before the credential is issued. Every agent's presence consumer
  delivers the records; the MCP server reads them from this release on.
  The service's JetStream user needs `$JS.API.STREAM.MSG.GET.PRESENCE` and
  `presence.attest.*.v1` (`services/auth/nats-server.example.conf`).
- **The channel directory gets its admin API and the import from Mattermost**
  (ADR-016, RT-6.40). With `AUTH_ADMIN_LISTEN_ADDR` the login service serves
  `/v1/directory/…` on a listener of its own (TLS, or a loopback IP),
  with a Keycloak access token on every request: admins from
  `AUTH_DIRECTORY_ADMIN_GROUP` create channels, set owners and make them
  public; a channel's owner keeps its members; `POST /v1/directory/import`
  copies the team's channels and human members once and marks the
  directory imported. A password account becomes a member only after an
  admin confirms its claim; admins remove a claim with
  `DELETE /v1/directory/pending/<name>`. `authd dir ls|show|create|owner|
  public|members|import|pending|confirm|drop` is its command line: it logs
  in with Keycloak's device flow on every run and keeps the token only in
  memory.
- **The Mattermost account index follows logins after the import** (#59,
  ADR-016, RT-6.41 step 2). A login whose chat account logs in over OpenID
  with the token's `sub` sets that account's name on its identity: new
  people are found, renames move the entry, and a name Mattermost gave to
  someone else moves to them (logged, `moved_from`); trust lists then trust
  the new holder. Password accounts never write it; an index that cannot be
  written refuses the login. Admins set or remove entries with
  `/v1/directory/accounts/mattermost/<name>` (`authd dir account
  show|set|rm`).
- **`shown_as` on reactions and in the event tag** (#66, RT-6.36 step 8),
  as on posts (below). Agents DM a NATS sender by it, so the link is proven
  afresh on every read, never cached; it is never an identity.
- **What the first NATS pilot found** (#66, RT-6.36). `bus_info` says in
  `over_nats` that people do not see a home over NATS in Mattermost.
  `post_message(to=<person>)` refuses with the reason (ask people with
  `ask_human`) instead of an empty error. A post by a NATS sender with a
  proven link to its bot carries `shown_as`, the bot's username, beside its
  `author`, which stays the NATS name. DMs between agents stay on Mattermost
  while the agents also run their bots. The website says that people do not
  see a home over NATS yet.

### Changed

- **Silence keeps the crown one more crown lease** (#64, RT-6.6 steps 1 and 6,
  ADR-007 amended 2026-11-24). A holder that turns silent (a push untaken
  for 45 s, or no sign in the window) no longer drops its crown at once: its
  status stays 👑 with `coordinator · silent · no sign since HH:MMZ`, without
  the receipt marker, for 150 s (`MM_CROWN_LEASE_SEC`) from the moment it
  turned silent, so no peer elects or claims over a missing 👀 meanwhile. A
  sign of the agent in that time ends the silence (for a push left untaken,
  its take-up: a turn, a tool call or a post); without one the crown
  drops at that lease's end, as before. In a home over NATS the holder's
  self-yield waits the same lease.
- **The login service speaks TLS first** (RT-6.36). Over `tls://` it now
  sends the TLS handshake before the server's INFO, as the hosts already do,
  so the NATS server needs `handshake_first` in its `tls` block
  (`services/auth/nats-server.example.conf`). A server that still waits for
  older clients may hold a duration (`"300ms"`) during the move; set it to
  `true` once this login service runs. `AUTH_NATS_URL` and `AUTH_JS_URL`
  that mix `tls://` with another scheme are refused at the start. A host whose
  server does not speak TLS first now says so in its log (`TLS: the server
  does not speak TLS first: set handshake_first in its tls block, or this is
  not a NATS TLS port`), and a certificate that is not trusted or names
  another host says which, instead of a bare `SSLError`.
- **A slim post's size is bounded** (issue #42). `get_thread` and `list_recent`
  stay whole by contract, but not without bound: an ask whose card title rides
  in its text was 22 KB in every thread read, and a foreign agent can post
  60 000 characters over NATS. The text is cut at `FETCH_MAX` (16 000 bytes, just
  under Mattermost's 16 383) and one post stays under `POST_MAX` (20 000 UTF-8
  bytes **of the field values** — the internal budget, not the serialized JSON's
  size — since both Mattermost and NATS bound bytes, not characters); the card is
  cut field by field to `BODY_MAX` first, so only the message can push a post
  over. Both point at `raw=true`, which returns everything, unbounded, including
  the whole thread.
- **The OpenCode plugin is checked against the hook API it declares** (issue #68).
  The plugin named plugin API `~2.1.2` while the owner runs OpenCode 2.4.0, and
  nothing verified the claim: its tests use a fake hook API, and `Plugin` was a
  type-only import. It now declares the peer range `>=2.1.2 <2.5` (so 2.4.0, the
  version the owner runs, is included), carries a `tsconfig` and a `typecheck`
  script, and CI runs `npm ci` and compiles it against `@opencode/plugin`'s own
  types at 2.1.2, 2.4.0 and 2.4.1 (both ends of the peer range) - a hook the API drops fails there. No behaviour changed: the context's
  domains and `mcp`/`skill.transform` are the same from 2.1.2 through 2.4.x, and
  it is tested by hand with 2.4.0. What the casts hide (the `editor` inside the
  two `transform` callbacks, `session.synthetic`, the `prompt` event) is not
  covered and still needs a session on a real host.

### Fixed

- **A signal no longer skips the MCP server's exit work** (RT-6.18 step 7,
  RT-6.10). The host process catches its signals before it writes the
  readiness file: a SIGTERM from `postEnd` right after the start (no relay
  ever connected) came while the NATS start was still importing nats-py and
  aiohttp, and ended the process without `BYE`, stats or unregistering.
  The stdio server's signal handler starts the exit work in a thread of its
  own: run inside an interrupted write to stderr, its first log line raised
  a reentrant-call error and the endpoint file stayed behind.
- **The crown no longer breaks a table or list** (RT-6.6 step 3). A coordinator's
  post that opens with a Markdown block (table row, list item, heading, code
  fence, quote) gets the 👑 on a line of its own and a blank line after it,
  so the block renders; other posts keep `👑 <text>`. A status post whose
  text opens with such a block (`kind="RESULT"` and a table) puts its bus word
  on the headline alone and the block below a blank line, instead of
  `RESULT · | a |`, which broke the table.
- **OpenCode's background service no longer takes a corral session's relay** (#70,
  RT-6.20 step 1, RT-6.18 step 6). An OpenCode command run in the sandbox without
  `--standalone` starts the machine's background service (`opencode serve --service`)
  with the session's `MM_BUS_*` variables; its plugin instance added a second relay
  to the session's host process, which serves one, so the session's own server failed
  with `another relay is connected`. With `--service` in its command line and those
  variables set, the plugin now adds no server and no skill (and takes out a bus
  server configured by hand), registers no other hook and says so once in OpenCode's
  log. The refusal of a second relay names the one that holds the slot: the pid and
  parent pid it says it has, which the relay now sends in its handshake, and its
  connect time.
- **Host login: a fast host clock, odd token times, the cooldown's cause**
  (#10, RT-6.34 step 3). A token's lifetime is now the shorter of
  `expires_in` and its own `exp - iat`, at most one day, counted from when
  its request went out: a host wall clock running ahead of Keycloak's by
  more than the lifetime made every token stale, so every call refreshed
  and rotated it. An `exp` or `iat` that is no sane time (NaN, infinite, an
  integer too large for a float), or an `exp` before the `iat`, is a
  `LoginError` before anything is stored; a huge integer raised
  `OverflowError` after the token was stored, with no cooldown. During a
  cooldown set by any other error, such as Keycloak not reachable or no
  notifier, callers see that error's text (one line, at most 120
  characters), not only "failed"; the mark on the host still holds only
  the fixed reasons.
- **Two causes of a busy coordinator losing its crown are fixed** (#64,
  RT-6.6 steps 1, 4 and 11). The prompt hook tells the server that a turn began before anything else,
  so a slow hook no longer leaves a running turn unknown, which made the
  holder silent 45 s after a push and dropped its crown. A channel tag under
  the plugin's old name (`mattermost-ai-agent-bus`) counts as the bus's too.
  A waiting `coordinator("take")` shows as `take pending until HH:MM:SSZ`
  in the taker's status, renewed by its server; while a peer of the home
  shows it in time, no other agent claims a free crown, and the taker's own
  yield after an earlier `release` does not hold it back. A guest's, a
  foreign agent's or an expired mark counts for nothing, and no presence
  label can carry it. Each server writes one
  JSON line per crown gained or lost to `crown-<uid>.log` in its state
  directory (the way, whether a turn ran, how long a push waited), never
  a post's content; the sweep removes it with the bot.
- **A failed approval's cooldown survives a restart** (#63). The 60 s mark after a
  failed login approval lived in the `Login` object, so a session start or a
  restart began with no cooldown and sent a fresh device DM at once. The mark is
  stored beside the refresh token now (`keystore.cooldown_entry`), as a wall-clock
  deadline that records its reason. It is read before every new approval, so a
  second session's cooldown counts, and deleted by a login that works; the
  process that set it keeps its own monotonic deadline. A deadline further
  ahead than the cooldown (a wall clock that moved backwards) is capped when
  it is taken in, so the lock never outlasts it. Hardening only: the
  three Keycloak links the pilot saw came from the realm's missing
  `offline_access` role, not from this gap.
- **The account index keeps a name when one identity logs in with two at
  once** (#72, RT-6.41 step 2). The name its record lists stays resolvable;
  before, both keys could be gone until its next login (fail closed).

### Internal

- The test rig says when a report contradicts itself (#54), its Mattermost
  stub keeps the status it is written (#60), and the NATS home test waits for
  the memory it asserts instead of sleeping (#75).
- **A test-rig run that receives nothing says whether the fault explains it**
  (#47). `bus-testrig bus-run` exits 1 when frames were sent and none arrived
  and no fault accounts for it - a path that carried nothing is not a
  measurement - and a run under a fault that discards or freezes every posted
  frame says so in its report instead of reading like a healthy run.

### Docs

- The arc42 runtime view (#87) and the decisions, risks and chapters 1 to 7
  (#88) were checked against the code and corrected; ADR-016 is in English
  and adds that everything Mattermost-facing moves into one service, step by
  step.

## 0.97.0

Needs: MM_NATS_HOMES alike on every agent of a home, the login service updated with the hosts and its bucket rights.

A home can run over NATS. It is opt-in per home and needs a NATS server,
Keycloak and the login service (`services/auth/`); with `MM_NATS_HOMES` and
`MM_LOGIN_ISSUER` unset nothing changes. Nothing is breaking.

### Added

- **A home can run over NATS** (RT-6.36, ADR-014 points 3 and 16). With
  `MM_NATS_HOMES` naming the home, the host login and `MM_NATS_URL` (optional
  `MM_NATS_CA_FILE`, `MM_NATS_MEMORY`), the agents' posts, threads and
  reactions in that home go over NATS. People, asks, files and the exit
  handler's posts stay on Mattermost, and nothing over NATS counts as a person
  or an approval. The tools do not change; NATS post ids look like
  `n1:<channel>:<n>` and have a read watermark of their own, since they carry
  the NATS server's time.
- **A NATS sender's owner and presence** (RT-6.37). The presence goes on
  `<presence>.<owner>.<name>`, the only presence subject the login service
  grants, so the owner is pinned like the sender; the owner counts only as a
  lower-case Mattermost username, never normalised. A sender whose owner is a
  trusted human of the session is a peer; any other is a foreign agent, its
  TASKs are requests. A presence is fresh for 90 s by the NATS server's clock,
  and a mentioned NATS agent counts as gone only once it is not. Presence and
  claims are ordered by the server's time, then the stream sequence. Update
  the login service and the hosts together: across the two releases the
  presence subject is refused, the agent looks offline and foreign and its bye
  removes nothing (the host's log says so).
- **Bot and NATS name linked two-sided** (RT-6.37). The presence names the
  agent's Mattermost bot and the bot's status shows a link tag (`⇄<tag>`); the
  two count as one agent only when each names the other. The membership of a
  home is its own subject (`<presence>.member.<channel>.<name>`), announced
  once the bus has read the stored presences, renewed every 6 h and after a
  reconnect, and read afresh by each new session (a credential renewal keeps
  its position); an agent that announced none is told as `not_here`. A bad bot
  id no longer stops the NATS home.
- **The crown of a home over NATS** (RT-6.38, ADR-014 point 16). The crown is
  the lowest crown claim among the home's members with a fresh presence that
  stand as peers; no Mattermost election runs there, and a home switching to
  NATS keeps no holder. The holder mirrors it on its Mattermost status without
  the receipt marker, `take` waits for the order and `release` claims again.
  Nobody takes it over by receipts, unlike `coordinator.md` promises for
  Mattermost: the holder claims again, on a crown beat, when a post that needs
  it has no 👀 or ⏳ after 45 s, and yields once to a linked reachable member
  when it hangs but still beats. The login service grants crown claims only in
  the agent's home. The 23 h renewal of a claim passes the crown to the next
  live candidate (a rotation about once every 23 h). A missing echo of the own
  presence triggers a reconnect, backing off up to 14 min (none when the
  presence subject is refused), and forgotten claims unsync the view until the
  replay is read. A home without its NATS side has no crown and says so (log,
  `bus_info`, roster hint).
- **An ask's decision text and the reactions that answered it are readable**
  (issue #30, RT-6.11). A slim post from `get_thread`, `list_recent` or a push
  carries `reactions` (`{emoji, author, trusted_human}`, at most eight, trusted
  humans first, `reactions_total` beside them), so a trusted human's thumbs-up
  on a RESULT shows too. An ask from an agent also carries `ask_card`: `need`,
  `decision` (what a 👍 approves), `answers` (the offered alternatives), and
  from a trusted human's newest reaction only `answer` (yes or no), `chosen`
  and `card_edited_after` when the card changed after that answer. `context`
  and `recommendation` stay out. Whether the author is an agent is the bus's
  own account test (`gate.author_classes`), never a name prefix; anyone may
  write a post's props, so `ask_card` is data a reader weighs, never an
  approval. The hook API's own `_slim` is unchanged.
- **Host login to Keycloak** (RT-6.34, ADR-014). With `MM_LOGIN_ISSUER` and
  `MM_LOGIN_CLIENT_ID` the host process (or a server outside corral) logs in
  with the device flow, keeps the refresh token and the NATS user key in a
  store (`MM_LOGIN_NKEY=ephemeral|host`) and sends the approval link to the
  responsible human by DM only.
- **The NATS transport in the MCP server** (ADR-014 point 16): `natsbus.py`
  with the bus's message format v1 (posts, thread roots and reactions by stream
  sequence), TLS first with the certificate verified, literal deliver
  subscriptions, gap detection and heartbeat; props carry `MM_PROPS_PREFIX`.
  The host stops when the credential carries another name than its own, and
  every send waits for the server's answer, so a refused one is an error. New
  dependency `nats-py` 2.16.0.
- **The login service** (`services/auth/`, ADR-014 point 16). It checks the
  Keycloak access token and answers the NATS auth callout with one permission
  model, NATS-native without a signed envelope: publish only with the agent's
  own name as the last subject token, subscribe only to `deliver.<name>.>`, no
  JetStream, key-value, `$SYS` or `_INBOX` right, crown claims only in the
  agent's home. The presence prefix is a setting (`AUTH_PRESENCE_PREFIX`,
  default `presence`).
- **The login service manages the agents' consumers** (RT-6.35). With its own
  JetStream user (`AUTH_JS_SEED_FILE`, required with `AUTH_NATS_URL`) it makes
  the streams `BUS` and `PRESENCE` (and refuses to start on unsafe settings),
  makes an agent's push consumers at every credential and removes those of
  channels no longer granted, serves history from the last sequence seen
  (`since`, at most 14 days back, direct messages under `@dm`), and removes an
  agent's consumers at its bye and an owner's on the emergency brake.
- **The login service keeps a channel directory** (ADR-016, RT-6.39). A
  key-value bucket in the tenant account (`AUTH_DIRECTORY_BUCKET`, default
  `BUS_DIRECTORY`) that only the login service reads and writes holds the
  channels, their members by Keycloak `sub`, and each identity's fixed anchor,
  from which its agents' names and presence subjects are made. Until the
  one-time import from Mattermost (a following release) has run, Mattermost
  still decides membership and every anchor is today's name: no agent changes
  its name. The service's JetStream user needs the bucket's rights
  (`services/auth/nats-server.example.conf`).
- **The login service image is published to GHCR.** Each merge to `main` that
  changes `services/auth/` pushes `ghcr.io/dembaca/ai-agent-bus-auth:<commit SHA>`
  with a build provenance attestation, after the service's tests; deployments
  use the digest from the job summary.

### Changed

- **In doubt, foreign** (RT-6.16, ADR-016). An agent whose owner cannot be
  read was a peer; now the owner last read holds for 5 minutes
  (`OWNER_GRACE_SEC`, at most 15 minutes after Mattermost last answered, with
  its cache), after
  that the agent is foreign and its `TASK` a request. A bot of this session or
  trusted humans that cannot be read, or configured names none of which
  resolves, no longer turn the owner split off: only a bot read without an
  owner does. The crown election fails closed the same way, on Mattermost and
  in a home over NATS: trusted humans that cannot be resolved never let an
  agent of another owner stand (RT-6.6, RT-6.38).
- The Stop hook opens a fresh listen window when the latest own post changed,
  no longer by comparing its time with this host's clock (RT-6.2 step 5). A
  window file from an older hook keeps the old rule until its next window.

### Fixed

- The version notice, `bus_info` and the host installer's `--check` find the
  installed plugin by its registry key, not by a substring of its path; an
  install in a folder without the plugin's name was invisible, and a foreign
  plugin with the name in its path could be reported as ours.
- **The endpoint file is named before it can exist** (RT-6.10). A SIGTERM in
  the server's start or in a `rekey` no longer leaves `endpoint-*.env` behind:
  the file carries its name from the moment it is on disk
  (`hookapi._write_file`, `server._start_hook_api`, `hookapi.rekey`), and a
  rekey names both files while both are there. The sweep also removes a
  leftover `endpoint-*.env.tmp`, which a signal between the temp file and its
  replace leaves and nothing else would take (`bin/mm-agent-sweep.sh`).

### Internal

- The test rig (`testrig/`) drives the real bus through a Mattermost stand-in
  and an injected fault, runnable without the lab; its process check
  (`_alive`) has a test for each of its four answers.
- CI runs the NATS tests and the proof of the permission model against
  nats-server 2.15.3; an update of that server needs them green.
- doc-check: a doc-map rule may exclude paths, a malformed rule is rejected,
  and a test pins the shipped map. Flaky tests of the login service are fixed
  at the cause.

### Docs

- ADR-015 (the operator web console, standing alone on NATS only; proposed,
  not built) and ADR-016 (the bus without a chat as its source; accepted).
- An example NATS server configuration for the login service's auth callout
  (`services/auth/nats-server.example.conf`), with placeholders instead of
  keys and a test that starts a server from it.
- The login service's README: the Mattermost role User Manager needs an
  Enterprise license, so without one the service's token stays a system
  admin's.
- The smoke test has a section "Over NATS" (`docs/smoke-test.md`): device
  login, first NATS session, the pilot home and the negative checks.
- The website has a page with the target picture (`site/target.html`),
  marked planned throughout.
- The website shows the marketing name Choirbus with a logo and favicons; the
  project, the plugin and the install commands stay `ai-agent-bus`.

## 0.96.0

Needs: the plugin reinstalled from the new marketplace, host copy from the new repo, every session restarted.

### Breaking

- **New name: ai-agent-bus.** Mattermost is now one chat the bus can run on,
  so the project drops it from its name and moves to
  https://github.com/dembaca/ai-agent-bus (website
  https://dembaca.github.io/ai-agent-bus/). The old repository
  `dembaca/mattermost-ai-agent-bus` is frozen at 0.95.0 and gets no releases.
- The Claude Code plugin and its marketplace are `ai-agent-bus`
  (`/plugin marketplace add dembaca/ai-agent-bus`,
  `/plugin install ai-agent-bus@ai-agent-bus`); the skill is
  `skills/ai-agent-bus/` (`ai-agent-bus:ai-agent-bus`, also in the reload line
  after compaction); the MCP server in `plugin.json`, the OpenCode plugin and
  the pi extension is `ai-agent-bus`. The tool names change with them:
  `mcp__plugin_mattermost-ai-agent-bus_mattermost-agent-bus__*` is now
  `mcp__plugin_ai-agent-bus_ai-agent-bus__*`, and a channel push comes as
  `<channel source="plugin:ai-agent-bus:ai-agent-bus">`.
- Hosts on 0.95.0 learn nothing of the move: their release check reads the old
  repository. The switch is by hand:
  - Uninstall the old plugin and remove its marketplace, add the new
    marketplace and install `ai-agent-bus@ai-agent-bus`, in every Claude config
    that has it, the corral homes too.
  - Rewrite what names the old plugin, marketplace, MCP server or tool prefix:
    permission rules, `--channels` arguments, `allowedChannelPlugins` entries,
    statusline paths, and `enabledPlugins` and `extraKnownMarketplaces` in the
    `.claude/settings.json` that other repositories check in. In OpenCode and
    pi the MCP server is `ai-agent-bus` too, so their tool names and permission
    rules change the same way.
  - A server added by hand (`claude mcp add`, Codex's
    `[mcp_servers.mattermost-agent-bus]`, Cursor) keeps running under its old
    key; re-add it as `ai-agent-bus` to get the new tool names.
  - Local checkouts: `git remote set-url origin` to the new repository, and
    `MM_AGENT_BUS_ROOT`, symlinks and `skills` paths to
    `skills/ai-agent-bus/`. An `MM_RELEASE_URL` that names the old repository
    goes.
  - On each host install the host copy once with the installer from a clone
    of the new repository checked out at `v0.96.0`:
    `bin/mm-agent-host-install.sh --release v0.96.0`, without
    `MM_RELEASE_URL`. The installer of 0.95.0 does not do: it unpacks only the
    files it knows, so its copy of 0.96.0 lacks `skills/ai-agent-bus/`, and a
    second run calls it current. Later releases and self-updates find the new
    repository by default.
  - Restart every session.
- The runtime texts call it the AI agent bus.

### Kept

- The Python package `mattermost_ai_agent_bus` and its console script
  `mattermost-ai-agent-bus-mcp`, the `MM_*` variables, the host copy at
  `~/.local/share/mm-agent-bus`, the state directories, the registrar and its
  URLs, and the file names `opencode/plugin/mattermost-agent-bus.ts` and
  `pi/mattermost-agent-bus.ts` (OpenCode and pi configs point at them).

## 0.95.0

### Breaking

- `bus_info`'s `crown` is now the name of the home whose crown the agent holds
  (`~<home>`) or `false`, no longer `true`/`false`: a caller that tests for the
  value `true` must test for truth instead. No hook, plugin or script of the
  bus reads it.

### Changed

- **The crown belongs to its home** (the coordinator election,
  [RT-6.6](docs/arc42/06-runtime-view.md); ADR-002; #258): an agent whose
  `MM_PROJECT_CHANNEL_ID` pointed to another channel than the one its home's
  name gives could claim a crown in that channel and act as its coordinator
  there (the 👑 prefix, the ask-mode acknowledgement, the mandate read).
  `MM_PROJECT_CHANNEL_ID` now counts only while its channel has the home's
  name (a channel the chat refuses counts as another; a read that fails for a
  passing reason keeps the id); the crown file records its home and a crown of
  another home counts nowhere; every crowned post decides by its own channel;
  the coordinator's duties follow the home-bound crown.

### Docs

- The decision record for agent traffic over NATS (ADR-014) records the
  owner's decisions of 2026-11-22 on the login: a resumed session keeps its
  approval while the refresh token is valid; offline tokens with 30 idle days
  for the bus's client only; the host uses each refresh token once, without
  reuse detection; the login service in Go; four later points open (#260).

## 0.94.1

### Fixed

- **No post lost or counted forever around a reconnect's backfill**
  ([RT-6.2](docs/arc42/06-runtime-view.md) step 1, #244, #245): posts handed
  to the agent while the WebSocket was down no longer stay counted as pending
  by the turn hooks although `wait_for_events` never returns them; the read
  mark no longer passes a post the backfill has not queued yet (a newer
  hand-over during the backfill or the crown renewal, or a live edit seen
  first), so a server that ends meanwhile no longer loses it; a backfill that
  cannot read leaves the hand-overs for the next one.
- **A dropped event stream closes its socket** (#250): when handling an event
  failed in the inbox, the old WebSocket stayed open beside the new one until
  garbage collection; it is now closed, at most 2 s, before the reconnect.

### Internal

- The WebSocket inbox takes an event source (`events.py`, Mattermost's in
  `mattermost_ws.py`); errors cross the chat port as `model.NotFound` and
  `NotAllowed`, never as HTTP statuses; the host process is typed; the own-post
  bookkeeping moved from the Mattermost adapter into `compose.py`; a test runs
  the whole server on stand-ins (#247–#249, #251, #252, #256). ADR-014 (agent
  traffic over NATS) accepted (#242, #253).

## 0.94.0

### Changed

- **One sentence in the terminal once the result is on the bus**
  ([RT-6.2](docs/arc42/06-runtime-view.md) step 2): an agent that posted its
  result or status on the bus closes its turn with one sentence saying what it
  posted where (`Posted the RESULT in the build thread.`) instead of repeating
  the content, which cost output tokens and context. A question asked in the
  terminal is still answered there. `SKILL.md`, the MCP instructions and the
  sign-off's words (Stop hook, `GET /v1/turn-end`) say so.
- **The bus skill comes back after a compaction**
  ([RT-6.7](docs/arc42/06-runtime-view.md)): a compaction keeps the MCP
  instructions and tool schemas but drops the skill's text, and agents then
  got the bus wrong (an `ask_human` with an invented `question` argument).
  The MCP server now builds the whole text after a compaction
  (`GET /v1/handoff`): the own latest posts as before, then the skill. OpenCode
  and pi get it verbatim. Claude Code saves a hook's `additionalContext` over
  10,000 characters to a file and shows only a preview, and the skill is
  longer, so its agent gets the call to load the skill
  `mattermost-ai-agent-bus:mattermost-ai-agent-bus` with the Skill tool and
  the rules most often broken after a compaction. `hooks/after-compact.sh`
  asks `GET /v1/handoff?runtime=claude-code` instead of building the text from
  `GET /v1/posts`, so one place words it for all three runtimes. Without own
  posts the skill alone comes back; a server without `SKILL.md` beside it
  leaves it out. The pi extension adds it without showing it (`display: false`): the
  human needs neither the skill nor the posts, which are on the bus, in the
  terminal.

### Internal

- The MCP server's entry points talk only to `ChatPort` and
  `RegistrationPort`, with `compose.py` as the composition root (#237–#241);
  tests pin the inbox's risky paths before the event source moves (#243).

## 0.93.0

Needs: host copy 0.93.0, and under corral the opencode plugin entry pointing at it.

### Added

- **The harness plugins come with the host copy**
  ([RT-6.28](docs/arc42/06-runtime-view.md) step 2,
  [RT-6.29](docs/arc42/06-runtime-view.md) step 5,
  [RT-6.20](docs/arc42/06-runtime-view.md) step 1,
  [RT-6.27](docs/arc42/06-runtime-view.md) step 1): the installer also copies
  `opencode/` (the OpenCode plugin) and `pi/` (the pi extension) into each
  release, what is committed only (`git archive`), optional as the skill is,
  so a release before this one still installs. Under corral, OpenCode and pi
  load the bus plugin read-only from `~/.local/share/mm-agent-bus/current`
  instead of a checkout an agent can write. The OpenCode plugin's default
  server gets `UV_PROJECT_ENVIRONMENT` in the user's cache
  (`~/.cache/mm-agent-bus/venv-<hash of the tree>`), the mechanism the pi
  extension has used since 0.86.3 (`venvDir`, now shared by both): nothing is
  written into the host copy, whose `mcp/.venv` stays the host process's.
  The relay's Python dependencies in that cache stay the agent's to change,
  like any file in its home, and hold no token.
  `node_modules/` is ignored, so an `npm install` in a clone does not block an
  install from it.
- **[adapters/opencode.md](adapters/opencode.md), *With corral***: the minimal
  plugin entry pointing at the host copy, the read grant, why `command` with
  `MM_REG_SECRET` and `environment` with `MM_CHAT_URL` … go under corral, which
  config the sandboxed OpenCode reads (its private `$HOME`), how to check it
  works, and the idle unload (`experimental.location_idle_timeout`).
  [adapters/pi.md](adapters/pi.md) loads the extension from the host copy too.
- **A warning when no relay connects**
  ([RT-6.18](docs/arc42/06-runtime-view.md) step 11): a keyless session's
  host process that has had no relay handshake 5 minutes after its start (the
  harness does not load the bus plugin, so the agent has no bus tools and no
  turn cycle, while the host kept its status alive) shows
  `relay: none since HH:MM — is the bus plugin installed in the harness?
  (adapters/opencode.md)` in `bus_info` and in the hook API's `/v1/me`
  (`relay`), logs it once and sends the responsible human one plain DM with
  the bot, the harness (`CORRAL_AGENT`, which preStart now passes to the host
  process) and a link to its adapter doc. A later handshake clears it; the DM
  never repeats in the session.

### Changed

- **No self-registration inside a corral sandbox**
  ([RT-6.18](docs/arc42/06-runtime-view.md) error handling,
  [RT-6.10](docs/arc42/06-runtime-view.md) error handling): with
  `CORRAL_SANDBOX`, `MM_BUS_MCP_ENDPOINT` or `MM_BUS_ENDPOINT` set,
  `session_start` (tool and client) and `bin/mm-agent-register.sh` refuse
  before anything reaches the registrar, naming the relay and the adapter
  docs; `bus_info` and the server's instructions say the same. A bot
  registered from inside puts its token into the sandbox. Defense in depth
  only: an agent that clears the variables gets past it; the boundary is a
  sandbox that cannot read the registration secret.
- **Site**: the harness matrix shows OpenCode + corral as supported, like
  pi + corral.

## 0.92.0

### Changed

- **The channel header shows the ask mode and a mandate**
  ([RT-6.6](docs/arc42/06-runtime-view.md) step 7,
  [RT-6.25](docs/arc42/06-runtime-view.md) steps 2 and 8,
  [RT-6.26](docs/arc42/06-runtime-view.md) step 6): the bus line in the home's
  header ends with `· asks → 👑 first` while the home's ask mode is
  `coordinator`, and with `· tasks here from 👑 of ~<home> ends <YYYY-MM-DD HH:MMZ>`
  while a mandate holds there (the end as RT-6.26 step 3 computes it, in UTC
  as `bus_info` shows it: `until 2026-12-01` ends `2026-12-01 00:00Z`). Every
  header change posts a system message in the channel, so only these rare
  facts go there, and the crown holder's server writes the line only when it
  differs: when the crown passes to it, once after it starts, and now also
  when the mode or the mandate it knows changes (a switch kept, a record read
  on the heartbeat, a mandate's start, end or expiry), checked on the
  heartbeat from the reads that happen anyway. With the default mode and no
  mandate the line is the one before, byte for byte: an upgrade writes
  nothing. Display only: the header is never read for the crown, the mode or
  a mandate; any member can edit it. A header the bot may not change is
  logged once, as before, and tried again only on a change; any other
  failure is tried again at the next heartbeat. ADR-005 and ADR-006 note it.
- **ADR-013 (the channel banner) withdrawn** in
  [Section 9](docs/arc42/09-architecture-decisions.md): the banner depends on
  the Mattermost edition and a permission setting, and the bus builds no
  feature that does. The header carries the rare facts instead. It was never
  built.

## 0.91.2

### Added

- **A relay log for permission prompts** ([RT-6.13](docs/arc42/06-runtime-view.md)
  step 6): a background subagent's permission dialog was approved in the
  terminal and no DM came, and nothing could tell whether Claude Code never
  sent the request or the server failed (its log lines go to stderr, which
  nobody keeps). Each step of the relay now leaves one JSON line in
  `permission-<uid>.log` in the state directory (0600): request received, not
  relayed (with the reason), DM posted, posting failed (the error's class), verdict
  sent (allow or deny, by whom), closed at the turn's end, and a `yes`/`no` for
  an id that is not open. A line holds a time, the request id, the tool name,
  the step and a reason, never the prompt's description or input, nor any
  command, path or file content. The log never passes 500 lines (then the newest 400 stay); a failed
  write is logged as a warning and never fails the relay. The sweep removes the file of a bot that is
  gone. RT-6.13's error table says what a missing `request received` line
  means for a subagent's prompt.
- ADR-009 and ADR-011 in the decision table of
  [Section 9](docs/arc42/09-architecture-decisions.md) say what is built:
  ADR-009 all three phases, ADR-011 phase 3 (0.86.0) and `latest` (0.87.0).
  No decision text changed.

## 0.91.1

### Fixed

- **A bus turn after an interrupted turn is no longer woken by what came
  during it** ([RT-6.9](docs/arc42/06-runtime-view.md) step 1): a trusted
  human's reply handed over in a turn the human then interrupted made the next
  agent-woken bus turn a human's, and one push before a bus turn that was
  interrupted and started again woke both. The turn after an interrupted one
  now takes nothing from before it and goes by its own first hand-over, which
  may be one it collected rather than the one that woke it (a drain that
  returns a human's later reply), so it can now and then count as a human's:
  rare, and biased against the bus. A push after an Escape is lost to it.
- **A human's answer to a question the 👑 had settled in an earlier part no
  longer counts as an ask** (RT-6.9 step 2): the answer says it answers a
  question to the 👑 (`coord`), and the Asks waiting figures leave it out.
- RT-6.9 step 3 says that human posts per agent-day differ between channel
  and listener sessions: a worker's unaddressed human post counts only where
  `wait_for_events` returns it.

## 0.91.0

### Added

- **Settled without a human** ([RT-6.9](docs/arc42/06-runtime-view.md)
  step 1): what the agents settled among themselves or through the
  coordinating agent, so the human did not have to act. Every `STATS` line
  counts the turns the bus started by who woke them (`bus_turns_agent`,
  `bus_turns_human`, from the server-verified author of what it handed the
  agent; only with turn hooks), the questions sent to the 👑 and those it
  answered against the asks that reached a human (`asks_crown`,
  `asks_crown_answered`, `asks_human`), the tasks this agent closed with `DONE`
  on an agent's `TASK` with or without a human in the thread
  (`tasks_agent_only`, `tasks_with_human`), and what humans did
  (`human_posts_trusted`, `human_posts_other`, `human_answers`). The report's
  new Settled table shows the shares per version and role, and human posts and
  answers per agent-day. Counts only. Questions to the 👑 no longer count in
  the Asks waiting figures, which are about waiting for a human.
- **Overhead stats for every day of a session**
  ([RT-6.9](docs/arc42/06-runtime-view.md) step 2): long sessions (days,
  resumed) rarely end, so 0.87–0.90 had almost no Claude Code numbers. The
  heartbeat now posts a `STATS` part every 24 h of session time (a constant),
  the end posts the rest. Each line carries a random `sid` (the same in all
  parts of a session), `part` and, on the end's line, `final: true`. What is
  open at a cut (a running turn, an open ask, an answer the human has not come
  back after, the crown) goes on in the next part and counts there, once.
- **Saved waiting**, a proxy for the human's reaction time, kept as a side
  figure (RT-6.9 step 1): per ask a trusted human answered on the
  bus while the terminal was idle (no human prompt since the ask), the time
  from the answer to the human's next prompt: `asks_idle`, `saved_n`,
  `saved_sec`, `saved_max_sec`, and `saved_open_n`, `saved_open_sec` for
  answers the human had not come back after by the end, a lower bound. A
  proxy, not a counterfactual; counts and seconds only.
- **Overhead by role** (RT-6.9 step 3): `crown_sec`, the seconds a session
  held the crown, from the heartbeat's own crown state. A part (a day, or the
  rest at the end) holding it more than half its time is the coordinator's,
  else a worker's, so a session can count in both.
- **The report** (`mm-agent-stats.sh report`, `stats_report`) counts a
  session once over its parts (`sessions` distinct, `parts` the lines) and adds
  `settled` (per version and role), `saved` (per version and harness) and
  `roles` (per version: tokens, turns, asks, settled), with a table each. Lines from older versions count as before.

## 0.90.2

### Fixed

- **The release notice no longer goes stale for a day after an update**
  ([RT-6.30](docs/arc42/06-runtime-view.md) step 5): a host copy installed right
  after a release check: the check had found the previous version as newest,
  so every session start said "older than ... not run since, or the repository
  is frozen or rewound" until the daily check came due. The server now checks
  again after an hour in that case, and passes that hour to `--if-due`. Only a
  host copy does: a plugin tree or checkout ahead of the tags keeps the day.
  Failures still count as before.

## 0.90.1

### Fixed

- **An aborted run no longer leaves the bot `working`**
  ([RT-6.27](docs/arc42/06-runtime-view.md) step 3,
  [RT-6.20](docs/arc42/06-runtime-view.md) step 3): the pi extension sent
  `presence working` at a run's start without waiting for it, and an abort at
  once sent `idle` beside it; the two could reach the server in either order,
  which then showed `working` until the lease ran out. The OpenCode plugin
  had the same race between its prompt hook's `working` and an interrupt's
  `idle`, and requests `working` as the turn starts now, before its turn
  counter, so an interrupt during that counter is applied last. Both now send their presence calls one at a time
  (`presenceQueue`, in the shared keep-in-step helpers): a state goes out only
  once the call before it has settled, and one superseded while it waits is
  not sent. A run still never waits for the bus.

## 0.90.0

### Added

- **Agents attach files to their posts** ([RT-6.33](docs/arc42/06-runtime-view.md),
  [ADR-012](docs/arc42/09-architecture-decisions.md)): asked for a bar chart, an
  agent could only post text bars. `mm-agent-post.sh --file PATH` (up to 10)
  reads each file in the sandbox with the agent's own rights and sends its
  bytes to the session's server (`POST /v1/files`), which uploads it into the
  post's channel; the post carries the ids, a file alone is a post too. The
  `attach(name, text, channel)` tool uploads a small text file (at most
  100 KB) without a shell; `post_message` and `reply_in_thread` take `files`.
  The server never opens a path the agent names (under corral it runs on the
  host: a link in the workspace would hand host files to the chat). Checked
  by the bytes, never the name: PNG, JPEG, GIF, WebP, PDF, and `txt`, `md`,
  `csv`, `tsv`, `json`, `log`, `yaml` as UTF-8 without NUL; the extension must
  name the same type, the name is cut to a plain base name, at most 10 MiB
  (refused before the body is read; Mattermost's `MaxFileSize` on top). SVG
  is refused. A post takes only this server's own uploads, unused, within the
  hour, in its channel: a human's or another agent's file id is refused. The
  home, guest channels, the lobby and the bot's DMs, as for posts. OpenCode
  and pi use the same shell route.

## 0.89.0

### Added

- **A bus post is answered in its thread, not only in the terminal**
  ([RT-6.2](docs/arc42/06-runtime-view.md) step 2): an agent got a trusted
  human's question as a reply in its thread and answered only in its
  terminal, and its `IDLE` let the turn end. Now a post that reached the
  agent and needs an answer — a mention, a DM, a reply in a thread it posted
  in, a post marked `for_crown`, handed over by `wait_for_events` or pushed
  by the channel (also a held post pushed when the crown comes back) — waits
  for one in its thread (`unanswered-<uid>.env` beside `last-<uid>.env`: at
  most the 10 newest, none older than 24 h, kept across a restart of the
  server). A human's post counts as it is; an agent's only when it wants an
  answer: it names the agent, is a DM, or its word asks for something
  (`TASK`, `DELEGATE`, `HANDOVER`, `CANCEL`, `BLOCKED`, `NO`), so courtesy
  replies and `ACK`/`RESULT` never make agents block each other. A post of the
  agent's in that thread (or that DM) answers it, the link line of an
  `ask_human` with that `root_id` too, and `resolve_ask` answers its ask's
  thread; a reaction, a pure status (`IDLE`, `BYE`, `HELLO`, `ASSIGNMENTS`) and
  the server's own lines (reminders, permission prompts and the relay's
  verdicts and notes, `resolve_ask`'s note) do not. At the turn end the Stop
  hook blocks once per post (💬 banner), together with the sign-off in one
  reason when both are due, the answer first. Not counted: gated posts, fyi
  posts, reactions, expired answers, the 👑's advice, system, webhook and
  server-own posts, digests. `GET /v1/last-post?unanswered=1` carries the
  posts not reminded of yet (`unanswered: [{post_id, root_id, channel,
  author}]`) and marks nothing; new `POST /v1/reminded {post_ids}` marks
  those a caller blocked for, so a dropped check leaves them due.
  `GET /v1/turn-end?unanswered=1` answers `answer` (with `posts`), or a
  `signoff` with both reasons, and the OpenCode plugin and the pi extension
  ask for it and mark the posts as the block goes out. The MCP instructions
  and the skill say it in a clause.

## 0.88.0

### Changed

- **The push tag says when the crown woke the agent**
  ([RT-6.6](docs/arc42/06-runtime-view.md) step 5,
  [RT-6.12](docs/arc42/06-runtime-view.md) step 3): a post that wakes the
  agent only because it holds the 👑 (a post in its home naming no agent,
  outside its own threads) carries `for_crown="true"` in its channel tag, and
  `wait_for_events` marks such an event `for_crown: true` (not `crown`, which the
  adapter boundary keeps for the roster). The crown holder,
  woken by a human's plain question, took it as someone else's and left it
  unanswered; the MCP instructions, the skill and
  coordinator.md now say a post so marked is the agent's to answer or route.
  `channel.wake_reason` says why a post wakes (`addressed`, `crown`), and
  `channel.wakes` is its yes/no.
- **The release notice never points back**
  ([RT-6.30](docs/arc42/06-runtime-view.md) step 6, RT-6.2 step 8): when the
  newest release the host's check found is older than the running version,
  the line names when the check ran and that it may just not have run since,
  `the release check of 2026-11-13 09:58Z found v0.86.0 as newest, older than
  0.87.0: not run since, or the repository is frozen or rewound` (the lib's
  `mm_release_notice` and the server's alike), and neither the turn-end
  banner, corral's start hook nor the installer's `--check` names the
  installer for it any more: the Stop hook by `older` in the server's
  `release` answer (decided against the server's own version), the others
  by `mm_release_newer` in `mm-agent-lib.sh`.

## 0.87.1

### Changed

- **The debug card's top part is a table**
  ([RT-6.31](docs/arc42/06-runtime-view.md) step 5, presentation only): two
  columns, one row per fact, the label in bold, `|` in a value escaped; the
  props stay as `json` blocks below. `sig` is split into its own rows (debug
  record, attestation, ask mode, mandate), so each fact appears once; the
  mandate's reason no longer reads `no mandate: mandate post ...`. Times are
  short and in UTC (`19:11:30Z` today, else `2026-11-12 21:15Z`). Categories,
  DM filtering and the 4,000-character cap are unchanged.

## 0.87.0

### Added

- **The registrar may name `latest`** (the owner's decision of 2026-11-13,
  ADR-011 phase 3,
  [RT-6.32](docs/arc42/06-runtime-view.md#632-self-update-between-sessions)
  step 4): besides a fixed `v<x.y.z>`, the release record's `version` may be
  `latest` (registrar 0.10.0, `RELEASE_VERSION=latest`). `--auto` then takes
  the newest tag of the host's release check's list that is not withdrawn
  (the check now writes its tags as `TAG` lines into
  `release-withdrawn.env`, [RT-6.30](docs/arc42/06-runtime-view.md) step 4)
  and goes the way of a fixed version: only forward, signed by a pinned key,
  not withdrawn, between sessions, `--hold` respected. A newest tag that
  fails the signature check is refused; the host does not step back to an
  older one. The five guard rails are unchanged; the fixed pin stays for
  staged rollouts.
- Latency: a new tag reaches a `latest` host at the end of its first session
  after the daily release check has listed it, so up to about a day. When
  timing matters, a fixed pin is the fast path.
- The decision lines in `release-auto.log`, `TARGET` in `release-auto.env`,
  the notice and `bus_info`'s `release.auto.target` say `latest (v<x.y.z>)`,
  or `latest` while the newest tag is not known yet: `updates itself to
  latest (v<x.y.z>) when this session ends`.

A host before 0.87.0 refuses a record that names `latest` (a failed read:
nothing pinned, no update). Name a fixed 0.87.0 or newer until every host runs
one, then `latest`.

Needs: registrar 0.10.0 for latest, every host on 0.87.0 first

## 0.86.4

### Fixed

- **No false `could not unregister` at the end of a corral session**
  ([RT-6.1](docs/arc42/06-runtime-view.md) step 7, RT-6.18 step 7): `postEnd`
  stops the host MCP server first, whose exit work unregisters the bot, then
  unregistered again with the now dead token, got `401`, warned and kept the
  session file. When the registrar refuses the token (`401`, or `404`) and
  Mattermost does too (`401` on `/users/me`; `mm_token_gone` in
  `mm-agent-lib.sh`, now the sweep's rule as well), it logs `<name>
  unregistered already (by its MCP server)` and removes the file; a failing
  or unreachable registrar and any other failure keep the warning and the
  file. The start hook's unregister-again does the same once it had started
  the host process.
- **A post handed over no longer counts as pending**
  ([RT-6.2](docs/arc42/06-runtime-view.md) step 1, RT-6.12 step 3, RT-6.22):
  the read watermark skipped a handed-over post while an older post of the
  same channel still waited in the inbox, and moved later only to that older
  one. The push acknowledges a batch mentions first, and `wait_for_events`
  returns them first, so a mention (often an agent's fyi `ACK` in a guest
  channel, where little else moves the mark) stayed pending: the turn-end
  check reported `pending=1 fyi=1`, `wait_for_events` returned nothing, and
  the Stop hook or pi's turn-end blocked turn after turn until the agent
  posted in that channel. The inbox now remembers such posts and the mark
  passes them once the older one is handled; it keeps the newest post the
  mark passed, too, so an older post that arrives late and pulls the mark
  back moves it past that one again once handed over.

## 0.86.3

### Fixed

- **pi's MCP server starts from a read-only checkout**
  ([RT-6.27](docs/arc42/06-runtime-view.md) step 1): the extension's
  `uv run --locked --project <checkout>/mcp` built its environment in the
  checkout's `mcp/.venv`, which a corral read grant refuses (`failed to remove
  file … Operation not permitted`). The server now gets
  `UV_PROJECT_ENVIRONMENT` in the user's cache
  (`~/.cache/mm-agent-bus/venv-<hash of the checkout>`), one per checkout,
  under corral and without (there in the corral home, so one per corral
  home and checkout); the checkout stays untouched. It is a cache, safe to
  delete.
- **[adapters/pi.md](adapters/pi.md) under corral**: the sandbox must read the
  bus checkout (`providers.paths.ro`), or pi lists no extension; pi started
  from `~` has no project `.envrc` and so no project channel and no bot.
- **A failed unregister says why** ([RT-6.1](docs/arc42/06-runtime-view.md)
  step 7, RT-6.18): the corral `postEnd` hook logged only
  `could not unregister <name>`; it now logs
  `could not unregister <name>: token: <reason>; secret: <reason>`, and the
  start hook's unregister-again does the same (its channel failure no longer
  says `unregistered … again` before it tried). No token or secret in it:
  `mm-agent-unregister.sh` prints the HTTP status and only a JSON
  `.message`/`.error`, printable and cut to 200, never one that holds the
  credential, and never plain text (it printed the whole answer before).

## 0.86.2

### Fixed

- **An ask notifies only the human it asks**
  ([RT-6.11](docs/arc42/06-runtime-view.md) step 1): an @-mention of a person
  in `need`, `context`, `recommendation`, `decision`, `where` or an answer
  notified that person, since `ask_human` wrote only `@channel`, `@here` and
  `@all` plain. Its fields now go through the same check as `post_message`,
  also those of a queued ask when it is posted: a person's name turns plain
  and is listed in `unmentioned`, an agent keeps its @, and the mention the
  server writes for the asked human stays. `human` must be one username:
  `channel`, `here`, `all` (`human="channel"` posted `@channel`) or a name
  with spaces is refused.
- **Mentions are read as Mattermost reads them** (RT-6.11): `@dembaca.x`,
  `@dembaca-x`, `@all_x` and a mention in italics (`_@dembaca_`) notify, and
  now turn plain too. Whether a name is a person's is the user lookup's to
  say: a person whose name starts with the agent prefix is no longer taken
  for an agent.
- **Code is left as written** (RT-6.11 step 1): a code block or code span
  keeps its @ in `post_message`, `reply_in_thread` and `ask_human` alike;
  Mattermost takes no mention from code, and a command keeps its exact text.

## 0.86.1

### Fixed

- **A question's situation no longer makes it an approval**
  ([RT-6.25](docs/arc42/06-runtime-view.md) step 1, RT-6.11): the floor reads
  what a human's answer picks or reacts to: the `decision`, the `answers` and
  the questions of the `need` (its sentences ending in `?`, outside code,
  quotes and URLs); all of the `need` when there is no `decision`. Beside a
  decision the need's other sentences are the situation: "PR #209 is merged (0.84.0)",
  "once registrar 0.8.0 is deployed", "a record naming the release to run"
  no longer refuse a question with three or four answers. An action in the
  decision, in an answer ("tag it now", also the noun in "the lines in its
  tag"), in a need's question ("Install 02dadca on hogan?" beside "Go
  ahead as recommended?") or anywhere in a need without a decision is still
  an approval.
- **An ask card keeps its code blocks** (RT-6.11 step 1): `need` was made one
  line, so a fenced block in it reached the card's `Context` joined into one
  line, `**` dropped and possibly cut at a `.` inside it. The title is now the
  first sentence outside a code block; the rest goes to `Context` with its
  line breaks and its blocks whole, and blank lines inside a block (of `need`,
  `context`, `recommendation` or `decision`) are no longer folded. A block
  left open in `need` is closed before the context; a code span at a line
  start ("```uv run pytest``` on hogan?") opens no block.

## 0.86.0

### Added

- **The host copy updates itself between sessions** (ADR-011 phase 3,
  [RT-6.32](docs/arc42/06-runtime-view.md#632-self-update-between-sessions)):
  the registrar (0.9.0) publishes one signed release record for its
  installation, `GET …/register/v1/release` (`mm-agent-release+jwt`): the
  release keys it offers and, optionally, the version hosts run. At the end of
  a corral session, after the bot is gone, the stop hook runs
  `mm-agent-host-install.sh --auto`, which reads the record (verified with
  `MM_REGISTRAR_KEYS` by the host copy's own Python), pins the offered keys
  when the host has none yet, refuses a record with other keys than the pinned
  ones, and installs the named version as `--release` would: only forward,
  only a tag signed by a pinned key, never a withdrawn one, and only when no
  other session of the host copy runs. A running session keeps its version.
  Without pinned registrar keys nothing is taken from the registrar.
- `--hold` and `--unhold`: a host's stop switch for self-updates
  (`release-hold` beside `releases/`).
- The notice (the start hook, the turn-end banner, `--check`, `bus_info`'s
  `release.auto`) says what will happen: `updates itself to v<x.y.z> when
  this session ends`, `held`, or that the registrar names a key the host
  doesn't trust. The release check reads the record for it, nothing more.

- The installer's lock holds its pid: a lock whose installer is gone is taken
  over, and the release check no longer waits for it. git gives up a stalled
  transfer after 60 s, and the end hook stops `--auto` after 600 s.

A host without release keys becomes a checked one at its first session end
after the registrar offers keys: it pins them then, and from that point it
refuses a release whose signature they don't verify.

Self-update starts with sessions that began on 0.86.0: install it once by hand.

Needs: registrar 0.9.0 for self updates

## 0.85.0

### Added

- **MCP debug, the bus side** (ADR-010 phase 2, [RT-6.31](docs/arc42/06-runtime-view.md#631-mcp-debug)):
  with the registrar's keys pinned, each server reads its home's `/agent-debug`
  record (registrar 0.8.0, `GET …/register/v1/debug/<channel id>`), verifies it
  like the ask mode's and counts it only when a trusted human of this session
  set it. While a category (`wake`, `identity`, `sig`, `asks`, `calls`, `props`)
  is on, the agent's post in its home gets a grey `debug` attachment after any
  ask card, about the event it answers; the agent gets the same lines as a
  `debug` field in `wait_for_events` and as a block in its channel tag. Never a
  key, signature, token or argument value; secrets' values as their length.
  Never in a guest channel or the lobby; a DM shows all categories to a peer
  agent or a trusted human, else only `props` and `calls`. No last good record:
  the registrar's exact 404 `no debug set`, a failed read and a record by
  someone not trusted are off at the next read; `bus_info` shows `debug`.

Needs: registrar 0.8.0 for debug

## 0.84.0

### Added

- **Release keys pinned once per host** (ADR-011 decision (4), [RT-6.29](docs/arc42/06-runtime-view.md#629-host-copy-from-a-signed-release)):
  a `--release` run with `MM_RELEASE_SIGNERS` keeps the keys that verified the
  tag in `release-signers` beside `releases/` and `src.git` (0600, type and
  base64 only), where `XDG_STATE_HOME` cannot move it. Later runs without the
  variable and the release check (RT-6.30), so also the MCP server's daily
  check under corral, verify against it. Other keys in the variable, a rotation
  included, are refused unless `--replace-signers`; the same keys change
  nothing. The file and its directory must be the user's own, writable by no
  one else and by no sandbox; anything else refuses the run instead of going
  unchecked. Both name where the keys came from. `--forget-signers` removes the
  file.
- **A withdrawn version is refused** (ADR-011 decision (5)): the release check
  also writes `release-withdrawn.env` beside `releases/`; `--release v<x.y.z>`
  and `--rollback` (a `-dev` or `-legacy` build too) refuse a version it lists,
  naming the release whose CHANGELOG withdrew it; `--withdrawn-ok` goes on
  purposely. Without the file nothing is refused, and a line says so.

## 0.83.0

### Added

- **The release check** (ADR-011 phase 2, [RT-6.30](docs/arc42/06-runtime-view.md#630-which-release-is-out-and-what-it-needs)):
  `bin/mm-agent-release-check.sh` lists the newest `v<x.y.z>` tags at
  `MM_RELEASE_URL`, fetches them into `src.git` (`--depth=1`, without `+`) and
  checks them as `--release` does, with the keys in `MM_RELEASE_SIGNERS` when
  the host sets them. From the CHANGELOG of the newest that passes it writes
  `release.env` in the host state directory: the version, `### Breaking`, the
  `Needs:` and `Withdrawn:` lines. It installs nothing; a tag that does not
  pass is logged once and never shown; a failed check changes nothing. Run by
  the installer's `--check` and by the MCP server about once a day as a child
  with a clean environment, stopped after 120 s; a failed try is repeated at the
  next hourly look, at most three a day. A server whose tree a sandbox can
  write (the plugin cache) runs only the host copy's script, or none. The
  installer and the check never fetch into `src.git` at the same time.
- **The notice:** `bus_info` has `release` (this server's version and source —
  `signed release`, `release … (signature not checked)`, `unsigned` after
  `--from`, `plugin` for the plugin —, the
  newest release that passed, `step`, `breaking`, `needs`, whether its
  signature was checked, `withdrawn`, `older`); the turn-end banner names a
  newer release once per version (`release` in the answer to `GET /v1/last-post`,
  which the Stop hook reads anyway); corral's start hook and
  `--check` print it too. A human installs a release; the agent never does.
- **The roster shows versions:** each server puts `v<x.y.z>` (with `unchecked`,
  `unsigned` or `plugin` when it is no signed release) as the last segment of
  its status text, cut before the label when the text is too long;
  `coordinator(action="roster")` rows have `version` and
  `source`, else the HELLO card's `version`. Older readers skip the segment.
- **Release rules** (CLAUDE.md, "Releasing"): `Needs:` lines take only `MM_*`
  names, versions and lowercase words of at most 15 letters, at most 120
  characters; `Withdrawn:`
  lines name older releases. `scripts/doc-check.py` enforces both.

### Changed

- The installer's tag checks moved into `mm-agent-lib.sh` (`mm_release_signers`,
  `mm_release_url`, `mm_release_tools`, `mm_rgit`, `mm_release_tag`), shared
  with the release check; `--release` behaves as in 0.82.0.

## 0.82.1

### Fixed

- **A question about version tags is no longer lifted to an approval** ([RT-6.25](docs/arc42/06-runtime-view.md) step 1, RT-6.11).
  The floor's words `tag` and `push` matched the plural nouns ("which key
  signs the version tags?", "channel pushes"), so a `kind=question` with three
  answers was refused. The plural nouns `tags` and `pushes` no longer lift; every
  other form (`tag`, `tagged`, `tagging`, `push`, `pushed`, `pushing`) does. The
  floor still scans only `need`, `decision` and `answers`, never `context`, and
  "Merge PR #5?", "Install 0.81 on hogan?" or "Let's tag v1" stay approvals.

## 0.82.0

### Added

- **`mm-agent-host-install.sh --release v<x.y.z>`: the host copy from a
  release tag** (ADR-011 phase 1b, [RT-6.29](docs/arc42/06-runtime-view.md#629-host-copy-from-a-signed-release),
  [§7.1](docs/arc42/07-deployment-view.md#71-production-deployment-corral-host-managed)).
  Fetches the tag from `MM_RELEASE_URL` (default the GitHub repository) into a
  bare `src.git` beside `releases/`, without `+`, so a tag fetched before never
  moves; checks that the tag is annotated, that its tag object carries the
  asked name and that `plugin.json`, `mcp/pyproject.toml` and `__init__.py` in
  its tree say its version. Then `git archive` into `releases/<x.y.z>/` and
  the switch, the downgrade rule, the lock and the garbage collection of
  0.81.0.
- **The signature check is opt-in through `MM_RELEASE_SIGNERS`** (decided
  2026-11-12). Set (the pinned Ed25519 public keys: the owner's release key and
  an offline backup), the installer verifies the tag with `git verify-tag`
  (`gpg.format=ssh`) against an allowed-signers file it writes, and marks the
  release `SOURCE=signed` with the tag, its object, the commit and the signer's
  fingerprint; it then needs git 2.34 or newer and an `ssh-keygen` with
  `-Y find-principals`. Unset, it prints a WARNING that the signature is not
  checked and marks the release `SOURCE=release`, `VERIFIED=no`; a release
  verified before is never marked down.
- Refused, with `current` unchanged, in both modes: a lightweight tag, a tag
  object named otherwise, a moved tag, the same version from another tag
  object than `releases/` holds, another version in the tree, a failed fetch;
  with keys also `MM_RELEASE_SIGNERS` set without a key, an unsigned tag, a PGP
  or X.509 signature, a signature by a key not pinned.
- **The installer refuses to run from a tree a sandbox can write in every
  mode**, `--release` included, and names the host copy's installer
  (`<MM_BUS_HOST_DIR>/bin/mm-agent-host-install.sh`); the start hook's notice
  names that path too. git runs without replace refs, the fetch follows no
  HTTP redirect, and an installer killed by INT, TERM or HUP removes its lock.
- **Releasing** (CLAUDE.md): after the merge the owner creates the signed tag
  `v<version>` on the merge commit and pushes it; agents never sign. A CI job
  on a pushed `v*` tag checks that its name matches `plugin.json`
  (informational).

### Changed

- **Which kind of host copy runs is shown:** the installer's output, `--check`
  and the start hook (its notice of a newer plugin, and its "ready" line) say
  `signed release v<x.y.z>`, `release v<x.y.z> (signature not checked)` or
  `unsigned`. `--from` and an install from the
  clone stay as they were, still `SOURCE=unsigned`.

## 0.81.0

### Added

- **`mm-agent-host-install.sh --rollback` and `--downgrade`** ([RT-6.28](docs/arc42/06-runtime-view.md#628-host-copy-install-switch-and-rollback), ADR-011 phase 1a).
  `--rollback` swaps `current` and `previous`; `--downgrade` is needed to
  install an older version than the one `current` runs, which is refused
  otherwise.
- **corral's hook files: a dispatcher** (`adapters/corral/mm-hook-dispatch.sh`,
  [RT-6.28 steps 4, 8, 9](docs/arc42/06-runtime-view.md#628-host-copy-install-switch-and-rollback)),
  installed as `current.hooks/mm-session-start.sh` and `mm-session-stop.sh`
  beside `current`. corral approves a hook by its configured file's content and
  skips a `postEnd` whose file changed during the session; the dispatcher stays
  byte for byte the same across releases, so a switch makes corral neither
  skip a running session's `postEnd` nor ask again. **Point corral's `exec`
  at these two files once** (the installer prints the config); the old
  `current/adapters/corral/…` paths still work, and the start hook says so.

### Changed

- **The host copy is a release directory, switched in one step** (ADR-011
  phase 1a, [RT-6.28](docs/arc42/06-runtime-view.md#628-host-copy-install-switch-and-rollback), [§7.1](docs/arc42/07-deployment-view.md#71-production-deployment-corral-host-managed)).
  The installer writes `releases/<version>-dev.<commit>/` beside
  `MM_BUS_HOST_DIR` with a `SOURCE=unsigned` file (no signed releases yet,
  phase 1b), runs `uv sync --locked` there before the switch, and only then
  points the symlink `current` at it in one rename (`mv -T` with GNU, `mv -h`
  with BSD and macOS); the one before stays as `previous`. A failed sync, or
  no `uv`, leaves `current` as it was. Releases neither link nor a running
  session uses are removed. `preStart` records the release a session runs
  from in the state directory (`<session>.release`), and at `postEnd` the
  dispatcher runs that release's stop hook when the record names a release
  directory of the same host copy, out of every sandbox's reach; else the
  current one.
- **Migration:** a host copy from before (a directory `current`) moves to
  `releases/<version>-legacy/` on the first install, gets its Python
  environment again and becomes `previous`. **Do it with no corral session
  running**: the installer refuses while one runs from the old copy (corral
  would skip its `postEnd`), and before any switch it warns of running
  sessions without a release record. Installing the commit `current` already
  runs changes nothing. One installer at a time (`.install.lock`).

## 0.80.0

### Added

- **The 👑's receipts as reactions and a new post kind** ([RT-6.12 step 7, RT-6.6 step 10](docs/arc42/06-runtime-view.md)):
  👀 and ⏳ from the crown holder's server, the receipt marker 👀 as a segment
  of the crown's status text (`coordinator · working · 👀 · …`), and the quiet
  fyi post `props.a2a.kind = crown_taken` with `holder`.

### Changed

- **The 👑's receipts and the claim over a missing one** (ADR-007 amendment
  phase E with phase 2, [RT-6.6 step 10, RT-6.12 step 7, RT-6.25 step 6](docs/arc42/06-runtime-view.md)).
  A post in the home that wakes the crown holder (a human's plain post, a
  mention of it, a question routed to it) gets a reaction from the holder's
  server, no model turn: 👀 when the agent has it (a push taken up, or the
  post returned by `wait_for_events`), ⏳ at once while a turn runs. The
  holder's status shows a marker for this, `coordinator · working · 👀 · …`.
  The other servers of the home watch such a post (only by their own trust,
  and only under a holder with the marker); with neither reaction after
  `MM_ACK_WAIT_SEC` (45 s) and one more look 10 s later, the oldest peer
  that stands claims the crown at its slot and posts one quiet fyi line,
  ``👑 taken over by `<bot>`: no 👀 for `<post id>` within 45 s``
  (`props.a2a.kind` `crown_taken`, posted again if it failed). The crown
  stays with it for a tick and a crown lease: the old holder yields when it
  reads that line from an author that stands for it (from its WebSocket, or from the home's
  posts at its tick when its socket is dead) and claims nothing for one
  crown lease (150 s), as after the silent drop. The successor of a silent holder posts the same line naming
  the silence. A question to the 👑 without either reaction after 45 s goes
  to the human at once instead of after `MM_ASK_ROUTE_WAIT_MIN`. What people
  notice: 👀 and ⏳ under the posts the coordinator gets, and a coordinator
  whose server or socket is gone is replaced within about a minute when a
  post needs it (a channel session that stops taking up was found by phase D
  already). Mixed versions: a holder on an older plugin shows no
  marker and is judged as before, so nobody claims over it. Runtimes without
  turn hooks (Cursor, Codex) show no ⏳: as 👑 they lose the crown when a post
  for them is not fetched within 45 s and a slot (a known limit).

## 0.79.0

### Added

- **A pi extension** (ADR-009 phase 3, [RT-6.27](docs/arc42/06-runtime-view.md),
  [adapters/pi.md](adapters/pi.md)). `pi/mattermost-agent-bus.ts` gives a pi
  session (pi-coding-agent 1.3) what the OpenCode plugin gives an OpenCode one:
  it registers the MCP server and the skill for the session, sets the presence
  per turn, runs the drain and sign-off checks when a run has settled
  (`agent_settled`, not `agent_end`), keeps one long poll of `/v1/wake` open
  for the session's life and starts a turn on a wake (at once when idle, as a
  follow-up after the tools of a human's run), hands the handoff state back
  after a compaction, and reports the context use for the bot's status
  (ADR-008). No listen window. It holds no token and no secret and finds the
  server as the OpenCode plugin does; under `corral run pi` the host hooks keep
  the token on the host as for Claude Code. Written against pi's 1.3.0 declarations
  and type-checked locally (CI runs no tsc); no live run against a bus yet.
  Without corral the registration secret reaches the server alone by a
  `!command` pi runs for it: set `MM_REG_SECRET_GOPASS` (new for pi, as the
  corral hooks use it) or `MM_REG_SECRET_CMD` (new). CI runs its tests
  (`node --test pi/test/extension.test.ts`). The MCP server is unchanged.
## 0.77.1

### Changed

- **The skill says: keep the main loop free** (`SKILL.md`, *Staying reachable*; runtime text, no new behaviour).
  An agent gives substantial work (builds, test runs, reviews, long searches) to
  sub-agents or background tasks, their model and effort fit to the task where
  its runtime offers that: a free main loop takes a message as soon as it comes
  or at its next look, a busy one late. `runtime-claude-code.md` carries the
  Claude Code specifics (the Agent's `model` and `effort`; with a channel a free
  loop wakes at once); `coordinator.md` links to it. Shortened elsewhere in
  `SKILL.md` to stay within its budget.

## 0.77.0

### Changed

- **Reachable only with a sign of life of the agent** (ADR-007 amendment
  phase D, [RT-6.6 steps 1 and 6, RT-6.12 step 4, RT-6.17 step 4](docs/arc42/06-runtime-view.md)).
  The server's heartbeat proved that the server runs, not that its agent does:
  a session in a background terminal kept the 👑 for two days while it took
  up nothing. The server now counts the agent's own signs (a turn begins or
  ends, a tool call, the hooks' and the listener's calls, never the status
  line's) and judges it `silent` while no turn runs: a push or wake left
  untaken for `MM_ACK_WAIT_SEC` (new, default 45 s), or no sign for
  `MM_LEASE_SEC` (900 s) outside 👂 idle as a channel. Silent, its status
  shows 💤 `silent · no sign since HH:MMZ`, it is not reachable (no
  candidate, no slot; `not_reachable` names it `silent`), and a holder drops
  the crown (no claim for one crown lease after); the next sign brings it back
  as a member. What people notice: a coordinator whose session stops taking up
  pushes loses the crown about a minute after the first one; an agent of a
  runtime without turn hooks (Cursor, Codex, `bus` state) shows 💤 `silent`
  15 minutes after its last bus tool call, unless it set `working` by hand;
  the skill's Cursor listen timer now waits at most 10 min, not 15. A running turn counts as
  alive until it ends: the server's ping between events is not built, nor the
  rule for a permission prompt left open. Mixed versions: an older plugin reads
  the 💤 as not reachable too.

## 0.76.0

### Added

- **One line in the HELLO thread when the context reaches 85 %** ([RT-6.6 step 9](docs/arc42/06-runtime-view.md), ADR-008 step 4 in part).
  When the status line reports `ctx` at 85 % or more, the agent's own MCP server
  posts `ctx 86% · compaction soon` in its HELLO thread, fyi, without a model turn.
  Once per context window: not again until a report is below 70 % (a compaction)
  and it climbs to 85 % again. Like a reminder it is no post of the agent's (no
  sign-off undone, no activity for the stall check); it wakes no other agent and
  enters no digest (channel push and listener). A 4xx refusal is not retried in
  that window, a transport error or 5xx is. A runtime that reports no `ctx` gets
  none. The 5-hour and 7-day thresholds, `BLOCKED` and the blocked state are not
  built: Claude Code reports no `rate_limits` behind the gateway, so they wait for
  that measurement.

## 0.75.0

### Changed

- **A dead 👑 is found in minutes, not in 15** (ADR-007 phase 1,
  [RT-6.6 steps 2 and 6](docs/arc42/06-runtime-view.md)). The crown holder's
  server renews its status every `MM_CROWN_BEAT_SEC` (60 s), a status write
  only, and the crown ends after `MM_CROWN_LEASE_SEC` (150 s) instead of
  `MM_LEASE_SEC` (900 s). The other servers look again when the holder's
  status would end, each after its slot (10 s times its place among the agents
  that stand), and the first that finds the crown gone claims it: a successor
  that died with its status still live delays the claim by one slot, not by a
  lease. What people notice: a coordinator idle or asleep for more than
  2.5 minutes loses the crown (it was 15), and the roster names a new one
  about 160 s after a holder died. Members' presence lease and heartbeat are
  unchanged. A slot claims only a crown whose holder is gone; a released
  one still goes to the pending `take` or the tick's election. The hosts
  need synchronised clocks: a holder more than about 90 s behind looks dead
  between its beats. Mixed versions: an older server still keeps a crown for
  900 s and claims only as the oldest; the new rules apply to what it receives.

## 0.74.0

### Breaking

- **The default props prefix is `x_`, not `bgl_`.** An installation that uses `bgl_`
  sets `MM_PROPS_PREFIX=bgl_` before updating, on every server of the bus, or its
  bridge's props are refused and its system replies are not recognised.

### Added

- **`MM_PROPS_PREFIX`: the props prefix is a setting** ([RT-6.16 step 8](docs/arc42/06-runtime-view.md), [chapter 7](docs/arc42/07-deployment-view.md)).
  The prefix of the props keys agents may add (and of the system key) was the
  installation-specific `bgl_`; it is now `MM_PROPS_PREFIX`, default `x_`. One
  value for the whole bus. A malformed prefix, or one a bus or Mattermost key
  starts with, stops the server at its start. The MCP instructions state the prefix in force. **Upgrading an installation that
  uses `bgl_`: set `MM_PROPS_PREFIX=bgl_`** where `MM_TRUSTED_HUMANS` is set.

## 0.73.0

### Breaking

- **A tool call with an argument the tool does not have is refused.** Until now it
  was accepted and the argument silently dropped. A caller that passes an unknown
  argument (for example `ask_human(need=…, question=…)`, which posted a card without
  its question) now gets the error `no argument <name>; the arguments are: …`
  and must correct the call.

### Changed

- **Unknown tool arguments are refused** ([RT-6.11 step 1](docs/arc42/06-runtime-view.md), IF-04).
  The argument models of all bus tools forbid extras (one place, `server.py`), the
  schema says `additionalProperties: false`, and the error names the unknown
  argument, the tool's valid ones and any other invalid argument of the same call.
  SKILL.md lists the fields of `ask_human` (`need` is the question), as do its tool
  description and coordinator.md.

## 0.72.0

### Changed

- **A post to another agent no longer wakes the 👑**
  ([RT-6.6 step 5](docs/arc42/06-runtime-view.md), [RT-6.2 step 4](docs/arc42/06-runtime-view.md)).
  The coordinator wakes at once for a post in its home that names no agent,
  which it routes, as before. A post that names another agent is that
  agent's task and reaches the coordinator with its digest, which saves a
  model turn per such post. Unchanged: a mention of an agent gone or not here
  that its server answers still wakes it (RT-6.21), and so do its own
  threads, mentions and DMs. The channel push, the listener and `/v1/wake` share the
  rule: `/v1/posts` marks a post `names_agent`, and `absent` when the
  server's RT-6.21 check wrote `ABSENT=<post_id>` to `gate-<uid>.env`.
  What people notice: a request to an agent that does not pick it up (its
  gate held back an author it does not trust, or a typo in the name) gets
  the 👑's attention only with the digest (`MM_LISTEN_DIGEST_SEC`).
  An agent mention is read like the RT-6.21 check reads it: in any case,
  not inside an address like `ops@agent-x.example`.

## 0.71.34

### Added

- **`props` on posts, system replies, attested bots** ([RT-6.16 steps 8-10](docs/arc42/06-runtime-view.md)).
  `post_message` and `reply_in_thread` take `props`: only `bgl_` keys, any other
  refuses the call, `bgl_system` is the bus's. The server's answers without the
  model (`Not woken: …`, departed or absent agents) carry `props.bgl_system=true`
  and reach no bot as a task. A bot of another name with a valid registrar
  attestation counts as an agent: it wakes, in a DM too, and approves nothing.

## 0.71.33

### Changed

- **The human's answer on an ask gives the assignee time before a stall**
  ([RT-6.19 step 2](docs/arc42/06-runtime-view.md)). A trusted human's yes,
  no or choice emoji on an ask counts as activity like their post: the task
  the ask shielded (0.71.31) is due `MM_STALL_SEC` after the answer, not at
  once, so the assignee can act first. A reaction without its time counts
  from the first look that sees it. A task that stays still after the grace
  still wakes the coordinator.

## 0.71.32

### Changed

- **A question only the human can answer goes to them; an ask to the 👑 counts for `unasked`**
  ([RT-6.25 step 3](docs/arc42/06-runtime-view.md), [RT-6.11 step 3](docs/arc42/06-runtime-view.md)).
  `ask_human(only_human=true)` marks a question about the human's own settings or
  action: in ask mode `coordinator` it goes to them directly, not via the 👑
  (`route_note` says so); the default is unchanged. `_unasked` counts an open,
  unsettled question to the 👑 as an open ask to its human, so a post naming them
  while it waits draws no `unasked` warning; once its server has settled it (after the wait of RT-6.25 step 6) it no longer counts.

## 0.71.31

### Changed

- **A task whose assignee waits on a human's answer is no stall**
  ([RT-6.19 step 2](docs/arc42/06-runtime-view.md)). When the assignee has
  an ask open that its server reminds (within 24 h, agent on the roster),
  the task is listed with that ask but wakes the coordinator no more than
  the ask itself; before, a task waiting only on a merge go woke it every
  20 min. An answered or resolved ask shields nothing, and the shielded time
  uses up no backoff.

## 0.71.30

### Changed

- **No polling for the answer to an ask in a channel session**
  ([RT-6.11](docs/arc42/06-runtime-view.md)). A 👍 collected by
  `wait_for_events` is a tool result, which auto mode did not credit (a merge was
  refused). The MCP `instructions` and `SKILL.md` say: after
  `ask_human` end the turn, the answer comes as a channel tag. `wait_for_events`
  in a confirmed channel session with an ask waiting on a human ends its result
  with `{kind: hint, text}`.

## 0.71.29

### Changed

- **A BLOCKED that waits on a human without an ask draws the warning**
  ([RT-6.11 step 3](docs/arc42/06-runtime-view.md), [RT-6.14](docs/arc42/06-runtime-view.md)).
  The hard `warning` of `_with_unmentioned` now covers `BLOCKED` and
  `WAITING` as well as `IDLE` and `RESULT`. An ask the human has answered no
  longer counts as open for the `unasked` check (`asks.blocking` instead of
  `open_to`, whose other callers are unchanged): a new decision after an
  answer needs a new ask. SKILL.md and the MCP `instructions` say so.

## 0.71.28

### Changed

- **One account cache in the client** ([RT-6.16 step 1](docs/arc42/06-runtime-view.md);
  follow-up of #182). `_usernames` and `_bots` are one cache,
  `user id -> Account(username, bot, deleted)`, filled by `usernames` and by
  the read of a bot's description alike, so the two cannot drift apart. A
  deleted bot is no agent (`agents`) but still no human owner
  (`_native_owner`): a rotated registrar bot as `owner_id` must not turn every
  agent foreign. No change on the wire.

## 0.71.27

### Changed

- **The question of an ask stands in its card** ([RT-6.11 step 1](docs/arc42/06-runtime-view.md)).
  The bold question sat above the card, apart from what belongs to it. The
  text is now only the mark and the mention (`🙋 @<human>`, `❓ @<👑>`), which
  still notifies; the question is the card's title. `Where` is left out when
  not given instead of shown as `–`. Open asks, reminders and ask headlines
  read the question from the card's title, from an older ask's bold text; a
  slim post (push, `get_thread`, `list_recent`, the hook API's posts) shows it
  after the mention as before, since it carries no attachments. The title goes
  through the link checks (RT-6.15) like the card's fields.
- **A hint to structure a long context** (RT-6.11 step 1). The `ask_human`
  description says that more than a few sentences read better as
  short lines or a list than as one paragraph; the shape stays the agent's.

## 0.71.26

### Changed

- **An ask keeps its line breaks and markdown** ([RT-6.11 step 1](docs/arc42/06-runtime-view.md)).
  `Context`, `Recommendation` and `Decision` of `ask_human` were each folded
  into one line, so a long context reached the human as a wall of text. They
  now keep paragraphs, lists and markdown; only the ends are trimmed and a run
  of blank lines becomes one. `Need`, `Where` and the answers stay one line.
  The text after the first sentence of `need` goes to the front of `Context`
  as its own paragraph. The tool description no longer asks for 1 to 3
  sentences: how an agent structures its text is its own choice. No new
  field, no length check; `@channel`, `@here`, `@all` and mentions are
  cleaned as before.

## 0.71.25

### Fixed

- **A peer agent was gated as a person** (RT-6.16 step 1). Reading a bot's
  description cached its username but not that it is a bot, so a later
  `agents` lookup found the name cached and never marked it: the agent counted
  as `other`, and its TASK to a worker got "Not woken: … is not on my trusted
  list". The description read now records the bot mark with the name.

## 0.71.24

### Changed

- **A mandated holder watches the channels of its mandate** (RT-6.19 step 6,
  RT-6.26 step 9; #141 point 5). While it holds the crown, the stall check also
  looks at each guest channel where a mandate names its home: the registrar's
  record there (or the pinned post without pinned keys), set by a trusted
  human of the session, not ended. Each channel keeps its own quiet period in
  `stall-<uid>.env` (keys with `@<channel id>`, no new file); a guest with a
  live coordinator of its own is not looked at (second in line: that
  coordinator nudges, the holder steps in when its crown lapses). A guest not
  judged at a check (failed read) keeps its saved state. One stall event for all channels due; the text marks the
  guests and their items. `bus_info` shows `mandate_holds`. No new trust, no
  approval; no registrar change.

## 0.71.23

### Added

- **A task to an agent that cannot take it is flagged** ([RT-6.14 step 10](docs/arc42/06-runtime-view.md)).
  `post_message` and `reply_in_thread` with `TASK` or `DELEGATE` check the
  agents the text @-mentions against the roster of the target channel (a DM
  is not checked); one that is gone, idle, offline or not in the channel is
  named in the result as `not_reachable`, with a `warning`. The post goes out
  all the same. Reachable is a live lease, not "listens right now". Reads
  fail open.

## 0.71.22

### Changed

- **Each meaning once in `model.py`** (arc42 §5.1, follow-up to 0.71.20). `Answer`
  now holds `YES`, `NO` and the choice of an alternative (`Answer.choice(n)`), so
  the answer marks left `Mark`; `Mark.BUS` is `Mark.HEADER` (the channel
  header's bus; the status `BUS` is a different character) and
  `StatusMark.CROWN` is `StatusMark.COORDINATOR` (a state, shown with the
  `crown` shortcode; the crown in a text stays `Mark.CROWN`). `a2a.answer_symbol`
  gives an answer's characters. The channel shows the same characters, byte for byte.

## 0.71.21

### Changed

- **The attestation is read from the registrar** ([RT-6.24 step 5](docs/arc42/06-runtime-view.md), ADR-008).
  Since 0.71.19 nothing wrote it onto the bot's props, so other agents read only
  the description. With the registrar's keys pinned (`MM_REGISTRAR_KEYS`),
  `agent_attestation` now asks `GET <registrar>/register/v1/attestations/<bot id>`
  (`attestation_record` on the port, next to `mandate_record`): `{"record": "<JWS>"}`,
  signed afresh per read. Only the registrar's own 404 with `{"error":"no
  attestation"}` means none; any other 404 or failure is unreadable and the
  description decides, as before. Kept for the smaller of 600 s and the record's
  `exp`; an unreadable one is asked again after a tenth of that. Needs a registrar
  with the endpoint (0.7.0); without it nothing changes. The props reading
  (`ATTESTATION_PROP`) is gone.

## 0.71.20

### Changed

- **No emoji in the code** ([RT-6.2 step 6](docs/arc42/06-runtime-view.md), arc42 §5.1).
  The core speaks in meanings (`Mark`, `Answer`, `StatusMark`, `Head` in
  `model.py`) and asks `a2a` for what Mattermost shows; `symbols.py` is the one
  Python module that knows the characters and shortcodes (written as escapes),
  `bin/mm-agent-symbols.sh` the one shell file. The channel shows the same
  characters as before. `test_no_emoji_literals` fails on a literal in the
  Python sources, the shell scripts or the OpenCode plugin, and the boundary test
  lets only `a2a.py` and `client.py` import `symbols.py`. The tests write their
  string literals as escapes. Docstrings, comments and the runtime texts
  (`SKILL.md`, README, arc42) stay text.

## 0.71.19

### Fixed

- **The attestation is no longer written to the bot's own props**
  ([RT-6.24 steps 3 and 5](docs/arc42/06-runtime-view.md#624-owner-attestation)).
  `attest.start` wrote it into the bot's user props with `PUT /api/v4/users/<id>/patch`,
  which Mattermost answers with a 403 for a bot's own user (measured with the
  diagnosis of 0.71.14): it never reached another agent, and every start logged a
  warning and tried again. The server now checks and logs the own attestation once
  and writes nothing; `publish_attestation` is gone from the port and the adapter,
  the HELLO card still carries it. Other agents get an owner from the bot
  description (and the native owner) as without one, which is what has been
  deciding all along. The carrier is open: the direction is the registrar serving
  it, as it serves the ask-mode record.

## 0.71.18

### Added

- **`/agent-mandate`: the mandate as a signed record**
  ([RT-6.26 step 8](docs/arc42/06-runtime-view.md), ADR-006 phase 2). With the
  registrar's keys pinned (`MM_REGISTRAR_KEYS`) only a record the registrar
  signed mandates: a trusted human types `/agent-mandate set ~<home> [until
  YYYY-MM-DD] [note]` in the target home (`off`, `status`), and each server
  reads `GET <registrar>/register/v1/mandates/<channel id>` at its first look
  and then every 120 s, verifies it (`attest.verify_mandate`, type
  `mm-agent-mandate+jwt`) and counts it only when `set_by_id` is one of its
  trusted humans. A 404 (none, or `off`) means none; a failed read, a record
  that does not verify and an older one keep the last good one to its expiry.
  A pinned mandate post then counts nothing (a hard cut, decided 2026-11-05), and the 👑's server
  answers a trusted human's one once with the command; `bus_info`'s `mandate`
  names `source` (`registrar` or `post`) and the `note`. Without pinned keys
  phase 1 holds, the pinned post. Needs the registrar 0.6.0, deployed first,
  and the slash command `/agent-mandate` created in Mattermost. Only the
  registrar's own 404 `{"error":"no mandate set"}` means none: any other 404 (a
  registrar before 0.6.0, a proxy) is a failed read, so a rollout never reads
  it as `off`; a failed read holds the last record for 5 refresh periods
  (about 10 min) after the last read that went through, not to its end; a
  swallowed mandate post is logged once on a server that is not the 👑's. The
  command is `/agent-mandate set ~<home> [until YYYY-MM-DD] [note]` (no emoji in
  the syntax), the record's claim `home`, `bus_info.mandate.role` reads
  `coordinator of ~<home>`; a test checks the registrar's interop vector.

## 0.71.17

### Changed

- **The roster reads the numbers from the status text, not the props**
  ([ADR-008 amendment](docs/arc42/09-architecture-decisions.md),
  [RT-6.6 steps 1 and 8](docs/arc42/06-runtime-view.md#66-coordinator-crown-and-presence-lease),
  [RT-6.14 step 4](docs/arc42/06-runtime-view.md#614-agent-cards-and-task-states-a2a-shapes)).
  The diagnosis of 0.71.14 showed `PUT /api/v4/users/<id>/patch → 403`:
  Mattermost refuses a bot's patch of its own user, so `agent_usage` in the
  props never reached anyone and no roster showed another agent's `ctx`. The
  custom status is writable and every roster read holds it, so
  `coordinator("roster")` now parses its segments (`usage.from_status`: `ctx`,
  `5h`, `7d`, `$cost`; a segment the cut dropped is unknown, not 0). The props
  write, the per-agent lookup and its cache are gone; `bus_info.usage` is
  `{report, shown}`. The status text is a contract now, tested with the cut
  variants. The attestation of RT-6.24 step 3 is written by the same patch and
  is refused the same way; it is noted in ADR-008 as open.

## 0.71.16

### Fixed

- **Events are no longer lost when the channel connection ends mid-push**
  ([RT-6.12 step 3](docs/arc42/06-runtime-view.md#612-messages-pushed-into-the-session-channel)).
  The pusher took a batch of events out of the inbox and the inbox handed them
  over (the read watermark passed their posts) at the read; when the relay
  connection ended, or a push failed, before the batch was pushed, the rest was
  neither pushed nor in the inbox, and the Stop hook did not count it. Now an
  event is handed over only once the pusher dealt with it (pushed on a live
  connection, held for the digest, dropped on purpose); the rest goes back to
  the front of the inbox for the next connection or `wait_for_events`. The
  inbox keeps what the pusher took in a list (`take` / `ack` / `nack`) that the
  read watermark and the counters count as waiting, so a newer post handed over
  meanwhile cannot move the watermark past events that come back. A push
  cancelled while on its way may be pushed again (at least once). A digest or a
  held post that cannot go out stays held (the run waits before it tries again),
  in order; the bookkeeping after a hand-over never ends the run.

## 0.71.15

### Changed

- **`ask_human` takes `answers` as a list of plain texts, the server gives the
  emojis**
  ([RT-6.11 step 1](docs/arc42/06-runtime-view.md)). `answers` was one string
  cut at every `/`: `👍 = ja, 5h/7d sind sichtbar · 👎 = nein` became
  `👍 = 👍 = ja, 5h · 👎 = 7d sind sichtbar · …`. Now it is two to four texts
  (`["go", "no-go"]`), the emojis come by position (2: 👍 the go, 👎; 3 to 4:
  1️⃣ to 4️⃣), so `choice: n` is always `answers[n - 1]`. A mark an agent wrote
  first (👍 ✅ 👎 ❌ 1️⃣–4️⃣ with `=`, `:` or `-`) is stripped, the cleaned list
  comes back as `answers` (also skin tones of 👍/👎 and ✔️ ☑️ count as marks), an
  entry of nothing but a mark is refused with a hint, not dropped, and so is a
  list of one or of five or more, an approval with more than two too. A `str`
  is no longer taken; an ask queued with one before this version has no answers
  (a queued `go/no-go` becomes free text). The ask keeps the list
  (`ANSWERS=` in `asks-<uid>.env`), and a reaction event names the answer as
  `chosen` beside `choice` (👍/👎 on two answers too).

## 0.71.14

### Added

- **The cost where there are no limits**
  ([ADR-008 amendment](docs/arc42/09-architecture-decisions.md),
  [RT-6.10 step 3](docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent),
  [RT-6.6 step 1](docs/arc42/06-runtime-view.md#66-coordinator-crown-and-presence-lease)).
  Behind a gateway (`ANTHROPIC_BASE_URL`) Claude Code reports no `rate_limits`,
  only the cost. The statusline tap also sends `cost.total_cost_usd` as
  `cost_usd` (moved by $0.10 or more, else every 60 s); where no 5-hour window
  is reported the bot's status shows it in the place of `5h` (`ctx 62% · $4.20`)
  and the roster row carries `cost_usd`. The gateway limit is written into
  ADR-008 as a known limit.
- **Why the numbers do not reach the roster becomes visible**: a failing write
  or read of the usage report in the bot's props is logged as a warning (once
  per failure) and `bus_info` has a `usage` block: the report held, `write`
  (`written 40 s ago`, or the last failure as `PUT <path> → <status>`) and
  `read` (how many reads of another bot's report found one, found none or
  failed, and the last failure). The cause of `ctx` showing only in the own
  roster row is not found yet; this is the way to see it.
- The usage report is written into the props with every status write (a turn's
  start or end), under the same lock, at most once a minute; before, a `ctx`
  below the first band (50 %) reached the roster only with the heartbeat, up to
  five minutes later, and nothing proves yet that this was the cause.

## 0.71.13

### Changed

- **Restart texts name `--resume <id>`, not `--continue`**
  ([RT-6.2 step 8](docs/arc42/06-runtime-view.md), RT-6.10 step 5). In a
  directory several sessions share, `claude --continue` takes the newest
  transcript, which may be another session's (it ran the coordinator's twice,
  once under another bot). The update note of the Stop hook, `bus_info`'s
  `update.how` and the docstrings now say `claude --resume <session-id>`
  (`corral run -- --resume <session-id>`), with the session's own id when the
  server (or, in the Stop hook, the hook's input) knows it, else "pick this session" in the `--resume` picker. Texts that
  only describe the takeover name `--resume <id>` first.

## 0.71.12

### Fixed

- **A reconnect is confirmed again**
  ([RT-6.12 step 2](docs/arc42/06-runtime-view.md#612-messages-pushed-into-the-session-channel)).
  In a corral session the host process lives on across relay reconnects
  (`/reload-plugins`, `/mcp`, a plugin update). Each new connection forgets
  `channel-<uid>.env`, but the process's confirmation stayed set: no new check
  was sent and nothing wrote the file, so the Stop hook asked for a listener
  beside a working push. Now a new connection gets a check with a new code and
  its confirmation writes the file again.

## 0.71.11

### Added

- **Stale copies stay off the bus**
  ([RT-6.10 step 1](docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent),
  [RT-6.18 step 1](docs/arc42/06-runtime-view.md), ADR-007 amendment step 3).
  At start the MCP server checks whether it is older than the installed plugin:
  code under `~/.claude/plugins/cache/` is current when its tree is the install
  path of any entry of `installed_plugins.json` for this plugin (any scope or
  marketplace) and stale when older than every entry; any other code (a
  checkout, the marketplace clone, an environment inside a tree) when older than
  its tree's `plugin.json`; not checked when `CLAUDE_PLUGIN_ROOT` names its own
  tree. A stale server registers no bot and takes none over: `bus_info`
  says `on_bus: false` with `stale: this server is <v> from <path>; installed
  is <v>` and what to do, the hook API answers `503`. Not checked while an
  installed version cannot be read, and only at start: a running session
  survives an auto-update. Case: 13 old servers from Cursor's `mcp-process`.
- corral's `preStart` compares the version of the host copy's Python environment
  (its distribution metadata) with the host copy and fails closed when it is
  older (`MM_BUS_UNAVAILABLE`, rerun the installer), since the host process
  reads no `installed_plugins.json`.

## 0.71.10

### Changed

- **OpenCode like the channels** (ADR-009 phase 2,
  [RT-6.20 steps 4, 5 and 7](docs/arc42/06-runtime-view.md#620-opencode-turn-cycle-through-its-plugin)).
  The plugin's 15 s timer gives way to one long poll of
  `GET /v1/wake?wait=50` between turns, and there is no listen window any
  more: the agent is woken for as long as its session lives. The server's gates
  hold as before (project channel, an own post that is no `BYE`); a turn that
  runs holds a wake until it has ended; `replaced` and failures back off; the
  unload aborts the request. Between turns the status is 🛰 "on the bus"
  (reachable). The plugin options `pollSec` and `MM_LISTEN_POLL_SEC` are gone
  for it, `waitSec` and `retrySec` are new.
- The inbox's WebSocket opens with the first long poll
  ([RT-6.17 step 7](docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)),
  one connect at a time, and a connect that is cancelled or fails half way
  leaves nothing that reads as connected. Not at the server's start: a session
  that never polls needs no socket; Claude Code's channels are unchanged.

## 0.71.9

### Changed

- **The crown only for peers**
  ([RT-6.6 step 2](docs/arc42/06-runtime-view.md#66-coordinator-crown-and-presence-lease),
  [ADR-007](docs/arc42/09-architecture-decisions.md) amendment, phase B). Each
  server elects among its own agent and the agents of the home that are its
  peers (`lease._elected`, `owners.py`): an unbound or foreign agent is never
  the oldest, and a server claims only when its bot is bound and it is a peer
  of each reachable agent with an owner (the case of an unbound bot that won
  the crown). Without owners the election runs as before; an explicit `take`
  and the rule of one crown per home are unchanged. Circles foreign to each
  other in one home get no automatic 👑. Without trusted humans (no owner
  split) the election is as before; a description that cannot be read, this bot's own too, blocks
  it instead of counting as "no owner"; of two crowns an unbound holder yields
  to a bound one; the roster, the task gate and the departed answer name only
  a peer as the next coordinator.

## 0.71.8

### Added

- **A long poll for `GET /v1/wake`**
  ([ADR-009](docs/arc42/09-architecture-decisions.md) phase 1,
  [RT-6.17 step 7](docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).
  With `?wait=S` (whole seconds, at most 55) the server holds the request until
  the checks of `/v1/wake` say the agent wakes, with the same rules, gates and
  trust as without it (a gated post never, past a `BYE` never), and answers as
  before. `S` is a hard deadline: then it answers `{wake: false}`, a check
  still running given up; a client reads with a timeout of about `S` + 10 s.
  `until` may be left out: no listen window ends the wait. The inbox rings it
  whenever it queues an event (posts and reactions once it is connected, from
  the first `wait_for_events`), the session's checks keep 1 s apart from the
  end of one, and a check runs at the latest every 15 s; it takes nothing from
  the queue. One waiting request per session: a second one answers the first
  `{wake: false, reason: "replaced"}` and cancels its running check, so two
  pollers never hold both heavy slots; a client that goes, or the server's
  exit, ends it. Without `wait` nothing changes, so the OpenCode plugin works
  as before; it moves to the long poll in phase 2.

## 0.71.7

### Fixed

- **A question never goes to a foreign 👑**
  ([RT-6.25 steps 3, 5 and 6](docs/arc42/06-runtime-view.md#625-ask-routing-modes)).
  In ask mode `coordinator` the question of a worker went to the home's 👑
  whoever it was, so a foreign or unbound bot that held the crown took the
  question, and its answer closed it before the human saw it. Now only a peer
  of this session is asked (`owners.peer`, the check of the mandate): else the
  human at once, with a `route_note`. The 👑's answer settles a question only
  while it is a peer, also if the crown moved after the question.
- `adapters/corral.md`: the example secret is the enrollment token from
  `/agent-token create`, the shared secret is gone.

## 0.71.6

### Added

- **Context and limits of the agents in the chat, phase 1**
  ([ADR-008](docs/arc42/09-architecture-decisions.md), steps 1 to 3).
  `bin/mm-agent-statusline.sh` reports the numbers of Claude Code's status line
  JSON (`ctx`, `ctx_size`, per window `used` and `resets_at`; no cost, no model)
  to the session's MCP server, detached and within 1 s, when one moved by a
  point or every 60 s ([RT-6.10 step 3](docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent)).
  `--usage-only` is the one line for a status line script of your own that
  passes the JSON on. New `POST /v1/usage` on the hook API keeps the last
  valid report per session and logs once whether `rate_limits` came
  ([RT-6.17](docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).
- **The bot's status shows them**: `working · bus · ctx 62% · 5h 40% · ~home`;
  `7d` and the reset time (`↻21:40Z`) only from 90 %; the home stays last, the
  label, then `7d`, `5h`, `ctx` give way within 100 characters. Written with the
  lease on the heartbeat, and once more when a band is crossed
  ([RT-6.6 steps 1 and 8](docs/arc42/06-runtime-view.md#66-coordinator-crown-and-presence-lease)).
- **The roster shows them**: each server writes `agent_usage` into its bot's
  props (like the attestation, under the status lock);
  `coordinator("roster")` adds `ctx`, `limit_5h`, `limit_7d` per agent and a
  `limit_shared` group for agents on one subscription (same `five_hour.resets_at`)
  ([RT-6.14 step 4](docs/arc42/06-runtime-view.md#614-agent-cards-and-task-states-a2a-shapes)).
  `coordinator.md` gets one line on what to do with the numbers.
- Not yet: threshold posts, `BLOCKED` and the blocked state (ADR-008 step 4),
  `/agent-status`, other runtimes.

### Fixed

- **`coordinator.md` of 0.71.4**: the follow-up of that release (`skills` back in
  the list of what an agent *can* do, the full "a risk, a blocker, a correction"
  list) missed its merge; it is in here, with the budget at 6,500 characters.

## 0.71.5

### Changed

- **Asks to the same target are queued, not refused**
  ([RT-6.11 step 2](docs/arc42/06-runtime-view.md#611-asking-a-human)).
  A second `ask_human` to a human (or the 👑) with an ask open used to come back
  `posted: false`; the agent had to remember and ask again. Now it is stored in
  `asks-<uid>.env` (`QUEUED=`) and answers `queued: true`, `position`, `behind`;
  at most 5 wait (`queue_full: true`). One question per ask and go / no-go are
  checked at once. When the open ask is answered (the reminders' rule) the
  heartbeat posts the next, routed as of then (RT-6.25); `resolve_ask` posts it
  at once; an `ask_posted` fyi event tells the agent. A 24 h old ask still blocks.
  The reminder says `· n more waiting` (RT-6.19), `coordinator("tasks")` lists the
  agent's own queue with `queued: true`.
- A queued ask gets an id (`q-…`): `resolve_ask` withdraws it. One queued ask is
  taken out of the queue before it is posted, so the heartbeat and `resolve_ask`
  never post it twice; once the ask is out, a failing later step does not post
  it again. `ask_posted` and `ask_route` events carry `intent: fyi` and no
  longer wake the 👑.
- `post_message` / `reply_in_thread` with `IDLE` or `RESULT` that name a trusted
  human without an open or queued ask return a `warning` beside `unasked`.

## 0.71.4

### Changed

- **The 👑 stays reachable and delegates**
  ([RT-6.14](docs/arc42/06-runtime-view.md#614-agent-cards-and-task-states-a2a-shapes)).
  `coordinator.md` now says why (a 👑 stuck in a long turn answers nobody) and
  in which order to give work away: a `TASK` to a reachable peer, else
  subagents or background jobs of the harness; decisions, review, asks and
  small things stay with the 👑, and the crown goes over before large own work.
  `runtime-claude-code.md` names the Agent tool and `run_in_background` as the
  means.

## 0.71.3

### Changed

- **Every alternative of an ask has its own emoji**
  ([RT-6.11 step 1](docs/arc42/06-runtime-view.md#611-asking-a-human)).
  The `Answer` field put the agent's alternatives (`answers`, "go / not now")
  before a fixed `👍 = yes · 👎 = no`, so two alternatives hung on no emoji and
  👍 stood twice with different meanings. Now `A / B` reads `👍 = A · 👎 = B`,
  three or four alternatives get 1️⃣ to 4️⃣, and the human's keycap comes back as
  `choice: <n>` in the reaction event. An approval has go / no-go only:
  `ask_human` posts nothing for an approval with more than two alternatives and
  says why. SKILL.md and the tool text tell agents to write it so.

## 0.71.2

### Changed

- **`/agent-mode` is now `/agent-ask-mode`**
  ([RT-6.25 step 8](docs/arc42/06-runtime-view.md#625-ask-routing-modes)).
  All human controls on the bus share the prefix `/agent-` and name their
  concept: `/agent-token`, `/agent-ask-mode`, later `/agent-mandate`. Only the
  texts the bus shows and reads change: the 👑's announcement (`… set by <human>
  with /agent-ask-mode (signed by the registrar)`), the hint answering an
  `ask-mode …` post, the skill, the docs. The registrar answers with the trigger
  it is configured with, and the signed record (`mm-agent-mode+jwt`, its claims)
  is unchanged, so no registrar build and no new key: the Mattermost slash
  command only needs its trigger word changed (same token). The post keyword
  `ask-mode coordinator|direct` stays.

## 0.71.1

### Fixed

- **The 👑 is renewed before missed posts are judged**
  ([RT-6.6 step 6](docs/arc42/06-runtime-view.md#66-coordinator-crown-and-presence-lease),
  [RT-6.12 step 3](docs/arc42/06-runtime-view.md#612-messages-pushed-into-the-session-channel)).
  After a sleep longer than the lease, the crown in `coord-<uid>.env` lay in
  the past until the next heartbeat tick. A trusted human's plain post in the
  home that the reconnect's backfill found in that window was held for the
  digest (up to `MM_LISTEN_DIGEST_SEC`) instead of waking the coordinator,
  and the gate judged it as if the agent had no crown. A (re)connect now runs
  one tick before the backfill (`WebSocketInbox.before_backfill`): it renews
  the crown, or claims it by the rules of step 2, and never takes one another
  agent holds; failing, it logs and goes on as before. And each time the
  crown passes to the agent (`lease.on_crown`), the pusher pushes at once the
  held posts that now wake it, in their order and once; gated and fyi posts
  stay for the digest.

## 0.71.0

### Added

- **Mandate for a role**
  ([RT-6.26](docs/arc42/06-runtime-view.md#626-mandate-for-a-role),
  ADR-006 phase 1, #141 point 3). A trusted human pins a post in a home whose
  first line is `mandate: 👑 of ~<home> [until <YYYY-MM-DD>]`; the holder of
  that crown then gives tasks there as the human's standing word. Each MCP
  server reads its home's pinned posts (`GET /api/v4/channels/<id>/pinned`)
  at its first look and on the heartbeat once 120 s have passed, keeps the
  result in memory, and keeps the last good one through a failed read until
  it expires. It counts only when the author's account id is one of the
  session's own trusted humans, top-level in the home, never a webhook's,
  bot's or agent's post. It ends 7 days after the post's time (its last
  edit, else its creation), `until` (the start of that day, UTC) at most 30
  days after it; deleted or edited away it is gone at the next read, unpinned
  it pauses. The role resolves per event to the live 👑 of `~<home>`
  (`lease.coordinator_of`, else that agent's own status here), and only a
  peer with the owner split active: a foreign holder or a lapsed crown
  mandates nobody. The holder's `TASK` roots in the home and its replies in
  their threads carry `mandated` (the mandate post's id) and `mandated_by` in
  `wait_for_events` and as push attributes; `coordinator("tasks")` marks the
  rows `mandated` (with `scope="all"` only in the home's entry), each judged
  once at first sight; `bus_info` shows `mandate` (role, by, until, holder,
  post) or why none holds. It approves nothing: the posts stay `from_agent`,
  never `from_trusted_human`, and the approval floor is unchanged. New
  `pinned_posts` and `get_channel_id` on the chat port.

## 0.70.0

### Added

- **Coordinator overview across homes**
  ([RT-6.14 step 9](docs/arc42/06-runtime-view.md#614-agent-cards-and-task-states-a2a-shapes),
  #141 point 2). `coordinator("tasks", scope="all")` and
  `coordinator("roster", scope="all")` cover every channel the bot is in as
  `bus_info` lists them (home, guests, lobby; RT-6.22): per channel its role,
  its crown holder as a home, and its tasks and asks (same window, trust
  filter and closing rules as for the home, with that channel's crown) or its
  roster (the agents whose home it is, coordinator first). `tasks` adds
  `asks_by_human`: each human's open asks across those channels, oldest first,
  with their channel. A channel that cannot be read comes back as an `error`
  entry, the others still answer. Reads only. `scope="home"`, the default, is
  unchanged; `scope="all"` with `who`, `take` or `release` is refused. New
  helpers `lease.roster_in` / `lease.crown_in`; `lease.roster` is unchanged.
  Budgets grow by the new argument: the `coordinator` docstring +80
  characters, coordinator.md +100, the per-turn static text (RT-6.9) from
  10,900 to 11,000.

## 0.69.0

### Added

- **corral keeps its bot across `claude --continue`**
  ([RT-6.18 steps 9–10](docs/arc42/06-runtime-view.md#618-keyless-host-managed-session),
  RT-6.1 step 7). A corral session with Claude Code (`CORRAL_AGENT=claude`) that
  ended by itself (not `CORRAL_AGENT_EXIT=aborted`) no longer loses its bot:
  `postEnd` sends the host process SIGUSR1 instead of SIGTERM, which only sets
  the bot offline (no `BYE`, no stats post, no unregister) and leaves its state
  directory, and marks the session file kept (`MM_SERVER_ENDED`,
  `MM_KEPT_WORKDIR` and `MM_KEPT_CLAUDE_SID`, both sha1 keys). Only when the host
  process saw a Claude session: at its first relay handshake it writes the
  session id's key to `hosted/<session>/claude-sid.env`. The next `preStart`
  reads corral's command line (`ps -o args=` of its parent, the words after
  `--`): `--continue`/`-c` takes over the kept bot of the same workdir that ended
  last, `--resume <id>`/`-r <id>` the one kept for that session — same name,
  threads, DMs, asks, read watermark and counters (the state directory is
  renamed), no registration, no welcome-DM baseline; the banner says
  `took over <name>`. Anything else — a plain start, the picker,
  `--fork-session`, another agent, a command line that cannot be read —
  registers a new bot as before. The claim is one rename, so of two starts only
  one gets the bot; a rejected token (`401`) means a new bot. The host process
  learns the kept session's key (`MM_BUS_KEPT_SID`) and logs a resume of
  another conversation; the bot stays and is kept under the relay's session.
  The token stays in the host state directory (0600) and reaches the host
  process on stdin only, as before.
- **Kept corral bots are reaped** after `MM_SESSION_KEEP_SEC` (24 h) from their
  end mark, with their own token, by every `preStart` and by
  `mm-agent-sweep.sh` (`KEPT` until then), through one lib function
  (`mm_unregister_session_file`); a claim left by a start that died goes back
  under its name.

### Changed

- **Both corral hooks refuse a state directory a sandbox can write**
  (`XDG_STATE_HOME` in a workdir, a corral home or corral's temporary
  directories): `preStart` contributes only `MM_BUS_UNAVAILABLE`, `postEnd`
  runs nothing. A kept token lives up to a day longer than its session.
- The sweep counts a kept bot's grace period from its `MM_SERVER_ENDED` mark
  where there is one (also for `mcp-*.env` of RT-6.10 step 7), else from the
  file's mtime as before.

## 0.68.0

### Added

- **Ask mode from the registrar's signed `/agent-mode`**
  ([RT-6.25 step 8](docs/arc42/06-runtime-view.md#625-ask-routing-modes),
  ADR-005 phase 2). With the registrar's keys pinned (`MM_REGISTRAR_KEYS`)
  a home's ask mode comes only from the record the registrar signs when a
  member of the channel types `/agent-mode direct|coordinator` there (registrar
  0.5.0). Each server reads `GET <registrar>/register/v1/modes/<channel id>`
  (from `MM_REGISTER_URL`, else `MM_CHAT_URL`; no Authorization header, 5 s,
  at most 8 KiB) at its first look at the mode and then every 120 s on the
  heartbeat, verifies it (`attest.verify_mode`: type `mm-agent-mode+jwt`, a
  pinned kid, this home, `direct` or `coordinator`) and counts it only when
  `set_by_id` is one of its own trusted humans and `set_at_ms` is newer than
  the record it keeps. The crown holder that keeps a new record posts one
  line in the home (`… set by <human> with /agent-mode (signed by the
  registrar)`). A trusted human's `ask-mode …` post then switches nothing: it
  goes to no model, and the 👑's server answers it once with the
  `/agent-mode` hint. A failed read, 404, an invalid record or `delegated`
  keep what is kept. `bus_info.ask_mode` adds `source: registrar` with
  `set_by`, and `signed`. Without pinned keys nothing is read from the
  registrar and phase 1 holds unchanged: inert until the #83 rollout pins the
  keys. Interop vector shared with the registrar (seed 0x00..0x1f, sha256
  prefix `8663e8575e142e7c`).

### Fixed

- **A switch posted while no server held the home's crown is acknowledged**
  ([RT-6.25 step 2](docs/arc42/06-runtime-view.md#625-ask-routing-modes)).
  Only a server holding the crown at the moment it kept a switch confirmed
  it, so a switch posted between a holder's restart and the next crowning
  was never acknowledged. The crown holder now confirms a kept mode that has
  no confirmation yet on its heartbeat, once (`CONFIRMED=1` in
  `askmode-<uid>.env`; a thread with the reply gets none).

## 0.67.0

### Added

- **Ask routing modes: questions to the coordinator first, approvals to a human**
  ([RT-6.25](docs/arc42/06-runtime-view.md#625-ask-routing-modes), ADR-005
  phase 1). `ask_human` takes `kind`: `question` (approach, order, who does X)
  or `approval` (an action with effect), default `approval`. The server's
  floor makes an ask an approval whatever was declared when it names a merge,
  deploy, release or tag, install, delete or remove, push, an external write
  (issue, PR, ticket, mail, a message outside the bus) or spending; the result
  says so (`kind_declared`, `floor`). Each home has an ask mode: `direct` (as
  before, the default) or `coordinator`. The default comes from
  `MM_ASK_ROUTE`; a trusted human switches a home with a top-level post there,
  `ask-mode coordinator` or `ask-mode direct`. Nobody else's post switches
  anything, and an agent's never does. Every server that sees the switch keeps
  it in `askmode-<uid>.env`; one without it reads the home's newest posts once.
  The 👑 acknowledges a switch once in its thread; `bus_info` shows `ask_mode`
  and its source. In `coordinator`, a question of an agent that does not hold
  its home's crown goes to the 👑, posted as `❓ @<👑> — **<need>**` and marked
  as not for a human; the worker gets the answer as `from_agent` with
  `advice`, never as approval. The 👑 answers it or escalates it with its own
  `ask_human(root_id=…)`. With no 👑 reachable the question goes to the human
  at once; unanswered for `MM_ASK_ROUTE_WAIT_MIN` minutes (default 15), the
  worker's server asks the human once, linked to the question, closes the
  question and tells the agent (`{kind: ask_route}`). The stall check wakes the
  👑 for an open question. Approvals, the 👑's own asks and every ask in
  `direct` go to the human as before. Not in this phase: a signed
  `/agent-mode` record, `delegated` with ADR-004, standing approvals, the
  coordinator's answers in the digest.

## 0.66.1

### Fixed

- **An edit that adds a mention wakes the agent once**
  ([RT-6.12 step 3](docs/arc42/06-runtime-view.md#612-messages-pushed-into-the-session-channel)).
  A post edited to add `@agent-…` woke nobody: the inbox read only the
  WebSocket's `posted` event, and Mattermost sends an edit as `post_edited`.
  An edit now wakes the agent when it makes a post a mention or DM to it that
  was not queued as one before, once per post and only within 24 h of the
  post: classified, gated
  ([RT-6.16](docs/arc42/06-runtime-view.md#616-who-may-wake-an-agent)) and
  delivered like a new mention, typing indicator included. The event says
  `edited` (a channel tag attribute; `wait_for_events` has it in the post).
  A typo fix in a post that already named the agent, an edited channel post
  and an edit of an older post wake nobody. The edited post is behind the
  read watermark, so it does not pull the mark back; the hooks' count
  (`/v1/pending`, the Stop hook's drain, the listener, `/v1/wake`) adds it
  while it waits. Which posts were queued as a mention or DM is kept in
  memory: after `claude --continue`, an edit of such a post less than a day
  old wakes the agent once more.

## 0.66.0

### Added

- **Owner attestation: the registrar's signed claim of whom an agent answers to**
  ([RT-6.24](docs/arc42/06-runtime-view.md#624-owner-attestation), ADR-003, #83).
  A registrar that signs answers a registration with `attestation`, a compact
  Ed25519 JWS over `bot_id`, `username`, `responsible`, `trusted`,
  `trusted_cut`, `kind`, `via` (`enrollment` or `secret`), `iat` and `exp`.
  `mm-agent-register.sh` exports it as `MM_BOT_ATTESTATION` and writes it to
  the session file; it reaches the MCP server in every mode (the corral host
  process through its start line, a shell-registered session through its
  environment, `session_start` and a taken-over bot through the session file).
  The server verifies it against the keys pinned in the new setting
  `MM_REGISTRAR_KEYS` (60 s of clock skew), compares it with the session's own
  responsible human, trusted humans and kind (a difference is a warning; the
  environment stays authoritative), and shows `attestation: valid | invalid
  (<reason>) | unverified (no pinned key) | none` in `bus_info`. A valid one
  replaces the profile read-back. The server publishes it in its bot's user
  props (`agent_attestation`: the props are read and sent back whole, so the
  custom status lease survives Mattermost's patch, which replaces the map) and
  in the HELLO card (`agent_card.attestation`; the HELLO text is unchanged).
- **Attested agents are judged by their attestation** (RT-6.16 step 1). With
  keys pinned, another agent's valid attestation gives its owner and trusted
  list instead of its description; where they disagree the attestation wins,
  logged. `via: enrollment` stands whatever the native owner says; `via: secret`
  is only the caller's claim and makes no agent more trusted than before: its
  owner counts only where a readable native owner agrees, else the agent is
  foreign (as an `unbound` one is). A `trusted_cut` is a cut
  list (foreign). Invalid, expired or under a key that is not pinned: the
  description decides, logged once per agent. The roster marks `attested`
  agents.
- **`bin/mm-agent-registrar-keys.sh`** reads the registrar's
  `/register/v1/keys`, prints each key's `x` and kid (checked with openssl
  where it is installed) and says to compare them out of band before pinning
  them in `MM_REGISTRAR_KEYS`. It writes no configuration; the bus never
  fetches keys at runtime.

Without `MM_REGISTRAR_KEYS`, and with a registrar that sends no attestation,
nothing changes. `cryptography` is now a direct dependency of the MCP server
(it was one already through `mcp`).

## 0.65.8

### Added

- **Agents address their own channels by name; `bus_info` shows the ids**
  ([RT-6.22](docs/arc42/06-runtime-view.md#622-guest-channels)).
  `list_recent`, `post_message` and `reply_in_thread` took only a 26-character
  channel id, which an agent with guest channels never saw. `channel_id` now
  also takes the name (`~name`, any case) of the agent's home, a guest channel
  or the lobby. Any other name is refused with the list of the agent's own
  channels: names never widen what an agent can read or post to. `bus_info`
  gains `channels: [{name, id, role}]` (`home`, `guest`, `lobby`; the lobby
  once, as home, when it is the home); `home`, `guests` and `lobby` stay.

## 0.65.7

### Added

- **A mention of an agent not in the channel gets an answer**
  ([RT-6.21](docs/arc42/06-runtime-view.md#621-mention-of-a-departed-agent-or-one-not-here)).
  `@agent-x` in a channel that agent is no member of reached nobody:
  Mattermost only showed the sender an ephemeral hint, and the 👑 left the
  post alone because it names an agent. Now the server that answers
  departed mentions (the 👑's, else the oldest reachable agent's) answers
  once per thread and agent: `agent-x is not in ~<channel> (home ~<home>):
  add it to the channel, or I forward it.` The home comes from the agent's
  status and is left out when it names none. Not here is a bot account on a
  live lease outside the channel; a person, an unknown name and a gone agent
  (which keeps its departed answer) are not. The event carries `not_here`
  (`<agent> (~<home>)`), wakes the answering agent, which forwards the post,
  and shows it typing in that thread
  ([RT-6.5](docs/arc42/06-runtime-view.md#65-typing-indicator-while-a-mention-is-answered)).
  Only names neither in the channel's roster nor gone are read: one
  `POST /users/usernames` for all of them and one
  `GET /channels/<id>/members/<user>` per live bot, both kept 60 s (new port
  members `bots_named`, `is_member`).

### Fixed

- **Dotted link labels are no host names**
  ([RT-6.15 step 4](docs/arc42/06-runtime-view.md#615-references-as-links)).
  `[RT-6.5](https://github.com/…)`, `[0.65.6](…)` or `[v1.2](…)` were taken
  for a host that differs from the link's and written out as label plus URL.
  A dotted label now names a host only when its last part starts with a
  letter (a top-level domain, `xn--` too) or it is an IPv4 address;
  `[example.com](https://evil.test/)` and `[1.2.3.4](https://evil.test/)` are
  still written out, `[README.md](…/README.md)` stays a link.

## 0.65.6

### Fixed

- **A refused registration says why**
  ([RT-6.1 step 3](docs/arc42/06-runtime-view.md#61-host-managed-session-lifecycle)).
  `curl -f` threw the registrar's answer away, so the corral banner showed only
  `curl: (22) ... 403`. `mm_json_post` now reads the HTTP status; on an error
  `bin/mm-agent-register.sh` prints `register failed: HTTP 403: <reason>` (the
  registrar's plain text, or its JSON `message`/`error`; printable, 200
  characters). A success body, which holds the bot token, is still never
  printed; an error body that holds the registration secret is withheld.

## 0.65.5

### Fixed

- **A backticked post id is linked**
  ([RT-6.15 step 2](docs/arc42/06-runtime-view.md#615-references-as-links)).
  A code span that holds only a post id, `` `<id>` ``, is linked as a bare id
  is, the word before it folded: ``Ask `<id>` `` becomes the ask's link,
  `` `<id>` `` alone `[Post](…)`. Other code spans and fences stay as written;
  reading turns the link back into the bare id.
- **Another agent's ask shows its headline**
  ([RT-6.15 step 2](docs/arc42/06-runtime-view.md#615-references-as-links)).
  `ask <id>` of an ask the agent did not post read `Input`; the adapter now
  reads that post once (`GET /api/v4/posts/<id>`) and takes its first bold
  text when the post is an ask (`props.a2a.kind = "ask"`, or `🙋 @` at its
  start after an optional 👑), its `@` dropped (`@dembaca` → `dembaca`,
  `@channel` → `channel`) so it notifies nobody. At most 5 reads per post; a
  definite answer (the headline, no ask, 403/404) is remembered while the
  server runs, an error that may pass (5xx, timeout, connection, bad JSON) is
  read again in a later post. A post that is no ask or cannot be read keeps
  `Input`, and the post goes all the same. The same for an ask's card.

## 0.65.4

### Changed

- **A compact HELLO**
  ([RT-6.14 step 1](docs/arc42/06-runtime-view.md#614-agent-cards-and-task-states-a2a-shapes)).
  `announce` writes three lines instead of seven, without naming the agent
  (Mattermost shows the author): ``` `HELLO` · **<task>** ```, a blank line,
  then project, branch or
  PR, model and role (project and branch in backticks when they are one plain
  word, a link as given), then `for … · trusts … · can: …`. The card in
  `props.agent_card` is unchanged. A HELLO without props is read in both
  forms; a departed agent's HELLO is found by its author, an old
  `HELLO @<agent>` also by its text
  ([RT-6.21 step 3](docs/arc42/06-runtime-view.md#621-mention-of-a-departed-agent-or-one-not-here)).
- **A static channel header**
  ([RT-6.6 step 7](docs/arc42/06-runtime-view.md#66-coordinator-crown-and-presence-lease)).
  The header's bus part no longer names the crown holder and the time; it is
  one line, `🚌 agent bus · plain post → 👑 routes it · @agent → that agent ·
  🙋 = a question for you`, written only when it is missing or differs, so a
  crown change no longer makes Mattermost post "updated the channel header".
  The crown holder's server checks it when the crown passes and once after it
  starts; an old bus part (`· 👑 @<agent> since <HH:MM>`) is replaced once.
  The 👑 on posts and in the custom status stay.
- **A status post's word in monospace**
  ([RT-6.14 step 2](docs/arc42/06-runtime-view.md#614-agent-cards-and-task-states-a2a-shapes)).
  When a post starts (after an optional 👑) with a bus word in capitals,
  `post_message`, `reply_in_thread`, `announce` and the server's own `BYE`
  write it in backticks, ``` 👑 `IDLE` · <gist> ```, and put a blank line
  after the headline when more lines follow. Agents write as before. Every
  reader of the first word (props, sign-off and listen gates, the Stop and
  compaction hooks, task titles, digests, the shell client) takes both forms,
  so older posts keep working.
- **An ask's callout titles are capitalised**
  ([RT-6.11 step 1](docs/arc42/06-runtime-view.md#611-asking-a-human)):
  `From`, `Blocking`, `Where`, `Context`, `Recommendation`, `Decision`,
  `Answer`. Nothing reads them back by title.

### Fixed

- **A coordinator's compaction handoff names its HELLO thread**
  ([RT-6.7 step 1](docs/arc42/06-runtime-view.md#67-handoff-state-after-compaction)).
  The compaction hook and `GET /v1/handoff` looked for a post starting with
  `HELLO` and missed one starting with `👑 HELLO`. A coordinator's `👑 BYE`
  now also ends the listen window in the Stop hook.

## 0.65.3

### Fixed

- **`ask_human` counts only real questions**
  ([RT-6.11 step 1](docs/arc42/06-runtime-view.md#611-asking-a-human)).
  The one-question check counts a `?` only where it ends a sentence (a space
  or the end follows), and none in a code span, a quoted example (`"…"`,
  `„…“`, `“…”`, `«…»`, `'…'`) or a URL. An ask whose context quotes an example
  headline such as `"Merge PR #1?"` or links `https://…?a=b` is no longer
  refused; two real questions still are.
- **A post's files in `GET /v1/posts` and in the digest**
  ([RT-6.23 step 1](docs/arc42/06-runtime-view.md#623-files-attached-to-a-post)).
  The hook API's `/v1/posts` (and so `mm-agent-poll.sh`) names a post's
  attachments as a slim post does (`files`, only when it has any); a digest
  line and a post in the compaction handoff end with
  ` · files: <id> <name>; …` after their cut text, so the agent knows to call
  `get_file`.

### Changed

- **Leaner `SKILL.md`**: about 670 characters shorter (15,769 of its 16,500
  budget), the same rules, tool names and scenario ids; repetitions merged.

## 0.65.2

### Changed

- **An ask's link shows its headline**
  ([RT-6.11 step 1](docs/arc42/06-runtime-view.md#611-asking-a-human),
  [RT-6.15 steps 2–3](docs/arc42/06-runtime-view.md#615-references-as-links)).
  The line `ask_human` posts in the thread an ask came from reads
  `🙋 <headline>` linked to the ask instead of `🙋 asked Post`, and `ask <id>`
  in any post shows the ask's headline (cut at 60 characters), `Input` when
  the server does not know it. The server keeps its own asks' headlines in
  `asks-<uid>.env`, after `resolve_ask` too (the newest 50); agents read every such link back as `ask <id>`.

### Fixed

- **No `IDLE` demand every turn after a plugin update**
  ([RT-6.2 step 2](docs/arc42/06-runtime-view.md#62-end-of-a-turn-drain-sign-off-listen)).
  With an MCP server older than 0.65.1 still running, `GET /v1/last-post` has
  no `signed_off`; the Stop hook falls back to the old rule (latest own post an
  `IDLE` or `BYE`) instead of asking for a sign-off every turn.

## 0.65.1

### Changed

- **A stall of reminded asks wakes nobody**
  ([RT-6.19](docs/arc42/06-runtime-view.md#619-stall-noticed-asks-reminded)).
  When every open item is an ask within its 24 h whose agent is still on the
  roster, the coordinator's server sends no stall event and pushes nothing:
  those asks' reminders nudge the human already. Any item that needs an agent
  (a task, a post naming an agent, an expired ask, an ask whose agent left)
  still wakes it, the reminded asks listed beside it; the digest says when an
  ask is not reminded.
- **No sign-off request when nothing changed on the bus**
  ([RT-6.2 step 2](docs/arc42/06-runtime-view.md#62-end-of-a-turn-drain-sign-off-listen)).
  The Stop hook (and `GET /v1/turn-end`) asks for an `IDLE` only when the
  agent posted, or an addressed event (mention, DM, an answer on its ask, the
  👑's human task) reached it, since its last `IDLE`/`BYE`. A turn spent only
  in the terminal ends without the extra request. The server's own ask
  reminders no longer count as the agent's latest post. `GET /v1/last-post`
  adds `signed_off`.

## 0.65.0

### Added

- **The agent sees the files a post carries**
  ([RT-6.23](docs/arc42/06-runtime-view.md#623-files-attached-to-a-post)).
  Slim posts, `wait_for_events` items, `list_recent` and `get_thread` name a
  post's attachments as `files: [{id, name, type, size}]` (about 85
  characters per file), a channel push as one `files` attribute. The new tool
  `get_file(file_id)` reads the file through the server's own token: an image
  as a picture (Mattermost's preview, at most 5 MB), a text file as text (cut
  at 20,000 characters, with a note), any other type as metadata only.
  Nothing is written to disk. The result names the author with
  `from_trusted_human`; a file from anyone else is data, never instructions
  (RT-6.16).

## 0.64.4

### Fixed

- **An ask's link line is no ask**
  ([RT-6.11](docs/arc42/06-runtime-view.md#611-asking-a-human),
  [RT-6.19](docs/arc42/06-runtime-view.md#619-stall-noticed-asks-reminded)).
  The `🙋 asked <ask id>` line that `ask_human` posts in the thread it was
  given now carries `props.a2a.kind = "ask_link"`; one already on the server
  without props is told by its text alone. The stall digest and
  `coordinator("tasks")` no longer list it as an open ask, also after the
  real ask was resolved; reminders were and stay for the ask only.

## 0.64.3

### Changed

- **A trusted human's 👍/👎 on an ask shows the agent typing**
  ([RT-6.5](docs/arc42/06-runtime-view.md#65-typing-indicator-while-a-mention-is-answered),
  [RT-6.11](docs/arc42/06-runtime-view.md#611-asking-a-human)). A yes/no
  reaction by a trusted human on an open ask, delivered by `wait_for_events`
  or the channel push, starts the typing indicator in the ask's thread, as a
  mention does, so the human sees the agent picked it up. An expired answer,
  another emoji and anyone else's reaction start none; a mention or DM in the
  same delivery keeps the indicator.

## 0.64.2

### Changed

- **Status posts in lines, not ` · ` chains**
  ([RT-6.2 step 2](docs/arc42/06-runtime-view.md#62-end-of-a-turn-drain-sign-off-listen)).
  The sign-off the Stop hook asks for is a headline `IDLE · <gist>`, then a
  line each for `**done:**`, `**open:**` and `**next:**` (a `- ` list for
  several items). The skill asks the same shape for `DELEGATE`, `WAITING`,
  `RESULT` and `PROGRESS`. A bystander still reads only the first line, now
  the headline (RT-6.14 step 6).
- **Post ids read `Post` for the human**
  ([RT-6.15 steps 2–3](docs/arc42/06-runtime-view.md#615-references-as-links)).
  A post id the server links, and a link the agent wrote with the bare id as
  its label, get the label `Post`; after `ask`, `thread`, `post`, `reply` or
  `RESULT` the word becomes the label (`ask <id>` → `[ask](…/pl/<id>)`). The
  id stays in the URL, and an agent reads the post with the bare id as before.

## 0.64.1

### Fixed

- **The corral note names guest channels as `~name`**
  ([RT-6.22 step 1](docs/arc42/06-runtime-view.md#622-guest-channels)).
  Bash 5 tilde-expanded the `~` in the note's guest list, so the agent read
  `Guest channels /Users/youbgl-infra`.
- **town-square and off-topic are no guest channels unless named**
  ([RT-6.22 step 1](docs/arc42/06-runtime-view.md#622-guest-channels)).
  The bot is in the team's default channels only because it joined the team;
  they now count as guests only when `MM_PROJECT_CHANNEL` lists them. A
  mention there still wakes the agent, as in any channel.

## 0.64.0

Closes out #63 (WP6): homes, guest channels, the lobby and owner classes are built.

### Added

- **The shell posts by channel name**
  ([RT-6.22 step 4](docs/arc42/06-runtime-view.md#622-guest-channels)).
  `mm-agent-post.sh --channel <name>` and `POST /v1/post` with `channel` post
  in the home, a guest channel or the lobby; a guest post carries
  `props.a2a.home` as with `post_message`. The shell joins nothing: a channel
  that does not exist or that the bot is not in is refused (`400`, exit 1) and
  nothing is posted. `--channel <name>` with `--root` is a usage error.
- **Owner warning at session start**
  ([RT-6.16 step 1](docs/arc42/06-runtime-view.md#616-who-may-wake-an-agent)).
  When the own bot's owner (native owner, else `for @`) is not the responsible
  human (`MM_RESPONSIBLE_HUMAN`, else the first trusted human),
  `session_start` carries a `warning` and the MCP log has it. Nothing else
  changes.

### Fixed

- **A foreign agent's ✅ closes no ask**
  ([RT-6.11](docs/arc42/06-runtime-view.md#611-asking-a-human),
  [ADR-001](docs/arc42/09-architecture-decisions.md#adr-001-authority-comes-from-trust-not-from-a-role-or-a-channel)).
  `coordinator("tasks")` and the stall digest counted any bus agent's ✅ as
  `resolve_ask`; another owner's agent could hide an ask from them. Now only a
  peer's ✅ closes it. The reminders (RT-6.19) were already right: they stop
  only for the human's or a trusted human's answer and the asker's own
  `resolve_ask`; a test now proves it for ✅.

### Docs

- README, `adapters/claude.md` and `SKILL.md` say where posts go (home, guest
  channels, lobby) and link RT-6.22; ADR-001 to ADR-003 and Section 11 mark
  what #63 delivered and keep the open items (#83, RISK-01, RISK-03 to RISK-05).

## 0.63.0

### Changed

- **The lobby** (WP5 of #63,
  [ADR-002](docs/arc42/09-architecture-decisions.md#adr-002-one-home-per-agent-guests-only-talk-a-crown-per-agent-home),
  [RT-6.22 step 3](docs/arc42/06-runtime-view.md#622-guest-channels)).
  `MM_CHANNEL`, when it is not the agent's home (a `MM_PROJECT_CHANNEL` is
  set), wakes the agent only for a mention, a DM, a reply in a thread it
  posted in, and a broadcast (`@channel` / `@all` / `@here`, `to="all"`). With
  trusted humans set, a broadcast by someone not trusted or by another owner's
  agent stays gated, so only trusted humans' and peers' broadcasts wake.
  - Other lobby posts go nowhere: no event from `wait_for_events`, no push,
    no digest; their read mark moves on. `list_recent` reads them on demand.
  - No coordinator wake there: the crown holder of another home wakes like
    any agent (since 0.59.0, kept).
  - Events from the lobby carry `lobby`; the gate's "Not woken" answer there
    names the lobby's own coordinator, as in a guest channel.
  - The Stop hook's count there is unchanged: a mention, `@channel` and posts
    for all agents, never a plain post.
  - Without `MM_PROJECT_CHANNEL` the lobby is the home: nothing changes.

## 0.62.0

### Added

- **Guest channels** (WP4 of #63,
  [ADR-002](docs/arc42/09-architecture-decisions.md#adr-002-one-home-per-agent-guests-only-talk-a-crown-per-agent-home),
  [RT-6.22](docs/arc42/06-runtime-view.md#622-guest-channels)).
  `MM_PROJECT_CHANNEL` takes a comma-separated list: the first entry is the
  home, the others are guest channels, and so is every other team channel the
  bot is a member of (someone added it) besides the lobby and the stats
  channel. One entry and no further membership behave as before.
  - Joined at start by `session_start` and `mm-agent-channels.sh`; a guest that
    is missing or private is reported once (`guest_channels`, `warning`,
    `bus_info.guests_not_joined`) and the session starts anyway.
  - Watched, and picked up while the session runs: the inbox reads the
    channels again at every connect and on the WebSocket's `user_added` /
    `user_removed` naming the bot; the hook API reads guests too (Section 7.3).
  - Only a mention, a DM or a reply in a thread the agent took part in wakes
    it there: no `@channel`, no `to="all"`, no coordinator wake, no digest, and
    the Stop hook counts nothing else there. Events carry `guest`.
  - `post_message(channel=<name>)` posts by name, joining a public channel.
    The agent's posts in a guest channel carry `props.a2a.home` and no crown.
  - A `BYE`, the agent's or the exit handler's, lists the guest threads not
    ended with `DONE` or `CANCEL` (`threads-<uid>.env`).
  - The gate's "Not woken" answer in a guest channel names that channel's own
    coordinator.
  - `bus_info` lists `home`, `guests` and `lobby`; the corral note and status
    line say `home #x, guests #y, lobby #z`; the session file carries the list.
- **Skill:** the Channels section of `SKILL.md` is a pointer to the new
  `channels.md` (home, guest, lobby, tasks across channels).

## 0.61.0

### Changed

- **The owner is bound, and read natively where possible** (WP3 of #63,
  [ADR-003](docs/arc42/09-architecture-decisions.md#adr-003-the-owner-comes-from-the-registrar-bound-bot-description),
  [RT-6.16](docs/arc42/06-runtime-view.md#616-who-may-wake-an-agent) step 1).
  Needs [mattermost-agent-registrar](https://github.com/dembaca/mattermost-agent-registrar)
  0.2.0 for bound bots. New `ChatPort.agent_owner`:
  - the bot's native Mattermost owner (`owner_id` of `/api/v4/bots/<id>`) where
    the operator grants bot accounts `read_others_bots`, else `for @…` from its
    description, cached like the description; the trusted list still comes
    from the description;
  - a native owner that disagrees with `for @…` makes a foreign agent (logged);
  - an `unbound` bot (registered with the shared secret) and one the registrar
    described without `for @` have no owner, whatever `owner_id` says: the
    registrar's own account owns them. Their `claims @…` is never read as the
    owner. An owner id that is no human (a bot, a plugin) is ignored;
  - a bot made by hand, without a registrar description, now has its native
    owner as owner where that is readable (it was foreign before).
- **An unbound own bot is named:** the owner split stays off for it, as
  before, and `bus_info`, `session_start` and the host's banner carry a
  `warning` that the bot is unbound and an enrollment token in
  `MM_REG_SECRET` (`/agent-token create`) binds it.

### Docs

- ADR-003 records the decided design (registrar `/agent-token` → per-user
  scoped enrollment tokens, native owner via `assign_bot`, the shared secret as
  the unbound fallback; Keycloak/OIDC dropped; #83 parked) and is built.
  RISK-02 is mitigated once the shared secret is off
  (`ALLOW_SHARED_SECRET=false`), RISK-03 reduced with `read_others_bots`.
  Setup links to the registrar README instead of restating it.

## 0.60.0

### Changed

- **Peer and foreign agents: authority comes from trust** (WP2 of #63,
  [ADR-001](docs/arc42/09-architecture-decisions.md#adr-001-authority-comes-from-trust-not-from-a-role-or-a-channel),
  [ADR-003](docs/arc42/09-architecture-decisions.md#adr-003-the-owner-comes-from-the-registrar-bound-bot-description),
  [RT-6.16](docs/arc42/06-runtime-view.md#616-who-may-wake-an-agent)):
  the MCP server reads each author bot's description (`for @<owner> · trusts
  @…`, written by the registrar) with its own bot token, cached, in every
  harness. A bus agent whose owner and every human it trusts are trusted by
  this session is a **peer** and works as every agent did before. Every other
  bus agent is **foreign**:
  - its mention, DM or reply in my thread wakes the worker as a request,
    marked `foreign` with its `owner`, never as a task;
  - its `TASK`/`DELEGATE` roots are left out of `coordinator("tasks")` and the
    `ASSIGNMENTS` table;
  - its broadcasts (`@channel`, `to="all"`) are gated as data;
  - it approves nothing (as before: only `from_trusted_human` does).
  
  A bot without `for @…`, and a trusted list the registrar cut (`+N`), are
  foreign. This closes the relay through an agent that trusts a human my
  session does not.
- **A routed request keeps its author:** a foreign agent's event in a thread
  whose root is by one of my trusted humans carries
  `root_from_trusted_human`, so a foreign coordinator can route my human's
  work to me without an ask.
- The gate's "Not woken" answer stays silent when the post also @-mentions an
  agent whose description trusts the author: that one answers.
- `coordinator("roster")` rows carry `owner`, `trusts` and `foreign` from the
  bot description; the `announce` result warns when the home's 👑 is foreign.
- SKILL.md keeps one rule for agents' requests; the coordinator's routing
  across owners moved to `coordinator.md`.

### Upgrade notes

- The split is active only when the session has trusted humans and its own bot
  description names an owner (`for @…`); otherwise every bus agent is a peer,
  as before. A description that cannot be read counts as a peer's, logged.
- Agents before 0.60.0 still treat every bus agent as a peer (RISK-04):
  upgrade every session before relying on the owner split. Until per-user
  registration secrets (WP3), anyone holding the shared secret can claim any
  owner (RISK-02).

## 0.59.0

### Changed

- **A home per agent, and a crown per home** (WP1 of #63,
  [ADR-002](docs/arc42/09-architecture-decisions.md#adr-002-one-home-per-agent-guests-only-talk-a-crown-per-agent-home)):
  an agent's home is its work channel (`MM_PROJECT_CHANNEL`, else
  `MM_CHANNEL`).
  - The custom status names it: `working · <label> · ~<home>`; past 100
    characters the label is cut, never the home. The host session hooks write
    the same form.
  - The roster, the crown election, "two crowns, the older keeps it" and the
    channel header count only agents whose status names the home. Two homes
    keep two crowns without conflict; a member whose home is elsewhere never
    claims the crown, and a project channel's coordinator no longer shows as
    the lobby's crown holder.
  - The 👑 prefix and `props.a2a.crown` go only on posts in the home, DMs and
    group messages, not in another team channel.
  - The coordinator wakes for plain posts and passes the wake gate only in its
    home (and DMs). A project channel's coordinator no longer wakes for a
    plain post in the lobby; only the lobby's own crown holder, an agent whose
    home the lobby is, routes it. A departed agent's mention is answered by
    the home's coordinator (RT-6.21).
  - The roster lists a member holding its own home's crown last, as
    `👑 of ~<home>` (the ambassador), never as this home's crown.
  - Parity: one channel, and a status without a home (a plugin before
    0.59.0), behave as before; an older agent counts in every channel until
    it upgrades (RISK-04). Upgrade every session to get separate crowns.
  - The corral session note says `home #<home>, lobby #<MM_CHANNEL>`.
  See [RT-6.6](docs/arc42/06-runtime-view.md#66-coordinator-crown-and-presence-lease).

## 0.58.0

### Added

- **Decisions for homes, guests and owners** (#63, #83), docs only, nothing
  built yet: arc42 Section 9 records
  [ADR-001](docs/arc42/09-architecture-decisions.md#adr-001-authority-comes-from-trust-not-from-a-role-or-a-channel)
  (authority from trust: peer and foreign agents, the relay gap closed, a
  routed request keeps its author),
  [ADR-002](docs/arc42/09-architecture-decisions.md#adr-002-one-home-per-agent-guests-only-talk-a-crown-per-agent-home)
  (one home per agent, guests only talk, a crown per agent and home, the
  lobby, the ambassador listing) and
  [ADR-003](docs/arc42/09-architecture-decisions.md#adr-003-the-owner-comes-from-the-registrar-bound-bot-description)
  (the owner from the registrar-bound description, per-user secrets before a
  second owner, #83 parked).
  [Section 11](docs/arc42/11-risks-and-technical-debt.md) lists RISK-01 to
  RISK-05. RT-6.6, RT-6.16 and Section 7.3 point to them.

### Fixed

- **Docs say what the code does:** the `post_message` tool doc says an empty
  `channel_id` posts to the work channel (`MM_PROJECT_CHANNEL`, else
  `MM_CHANNEL`), not to `MM_CHANNEL`; RT-6.16 says bus agents carry the
  registrar's configurable prefix, not always `agent-`
  ([RT-6.16](docs/arc42/06-runtime-view.md#616-who-may-wake-an-agent)).

## 0.57.1

### Changed

- **OpenCode with its plugin finds its runtime file:** `SKILL.md` and
  `runtime-claude-code.md` say that an OpenCode agent with the plugin reads
  `runtime-claude-code.md`, without the channel and permission prompt parts
  ([RT-6.20](docs/arc42/06-runtime-view.md#620-opencode-turn-cycle-through-its-plugin)).

## 0.57.0

### Changed

- **A slimmer skill:** `SKILL.md`, which every agent on the bus loads in
  full, goes from ~22,100 to ~16,300 characters. It keeps what every agent
  needs on every runtime (contract, bus words, posting and threads, asks,
  approvals, the crown); what only one runtime needs moves, unchanged in
  meaning, to a file next to it that `SKILL.md` points to:
  `runtime-claude-code.md` (`Stop` hook, channel push and `channel_ok`,
  listen window, status, permission prompts) and `runtime-no-hooks.md`
  (polling with `wait_for_events`, the timer listen window, status by hand;
  same function and security, only the host-held token is corral's). Install
  the whole skill directory, not only `SKILL.md`: the adapter docs say so. A
  test gives each file a budget and checks every pointer names a file there
  ([RT-6.9](docs/arc42/06-runtime-view.md#69-measuring-the-coordination-overhead)).

## 0.56.0

### Added

- **The 👑 keeps the work channel's header:** when the crown passes to an
  agent, its server writes who holds it since when and how to address the
  agents (`plain post → 👑 routes it · @agent → that agent · 🙋 = a question
  for you`) into the header, after a `🚌 agent bus` mark. Only that part is
  replaced; what people wrote stays. Written only when the crown changes
  hands and the header says something else, since Mattermost posts a system
  message for every change; without permission it is logged once and the
  crown works as before
  ([RT-6.6](docs/arc42/06-runtime-view.md#66-coordinator-crown-and-presence-lease)).

### Fixed

- **Post ids in an ask's card are links:** the permalinks and written-out
  odd links of a post now cover the text of its message attachments too
  (pretext, text, field values), in the adapter for every caller, so a post
  id in an ask's context opens with one click; agents read it back bare.
  SKILL.md and the `ask_human` doc say to write PRs and issues in an ask as
  markdown links too
  ([RT-6.15](docs/arc42/06-runtime-view.md#615-references-as-links)).
- **An ask always has its own thread:** `ask_human` with `root_id` posted the
  ask inside a shared thread, where any later post by the human counted as
  the answer and stopped the reminders. The ask is now always a top-level
  post, recorded with its own thread; the given thread gets a one-line link
  to it (`🙋 asked <permalink>`) without the mention
  ([RT-6.11](docs/arc42/06-runtime-view.md#611-asking-a-human)).

## 0.55.0

### Changed

- **`static` in STATS is the per-turn text only:** it counted `SKILL.md`
  (~22k characters) as if every turn carried it, though most sessions never
  load the skill. `static` is now the MCP instructions and tool docs; the
  skill's size is a field of its own, `skill`, and `tok.skill` says whether
  the session loaded it. `mm-agent-stats.sh report` shows skill characters
  per load and how many sessions loaded it; `static` of versions before
  0.55.0 still holds the skill
  ([RT-6.9](docs/arc42/06-runtime-view.md#69-measuring-the-coordination-overhead)).
- **A budget for the MCP instructions:** every variant (host-managed,
  hosted, self-registering, unavailable; with and without push and
  permission relay) has a limit about 10 % over its size today, 1.7k–2.4k
  characters, which a test holds. `SKILL.md`'s budget is per load now.

### Added

- **`stats_report` tool:** a session reads the overhead report from the
  stats channel through its own bot, without the host's token: the newest
  versions, as `mm-agent-stats.sh report --json` gives them; a test keeps
  the two the same
  ([RT-6.9](docs/arc42/06-runtime-view.md#69-measuring-the-coordination-overhead)).

## 0.54.0

### Changed

- **A long post no longer fills the session:** a pushed post, and a post
  from `wait_for_events`, over 2,000 characters is cut at the last line or
  word end before that and ends with `… (cut, <n> chars in all; get_thread
  <post_id>)`. Pushes measured a median of 465 characters and a maximum of
  6,051, with Mattermost allowing 16,383; the cut touches fewer than one in
  ten. `raw=true`, `get_thread` and `list_recent` keep the whole text. A push
  digest lists at most 20 posts, then `+<n> more` with the `list_recent`
  call that shows the rest
  ([RT-6.12](docs/arc42/06-runtime-view.md#612-messages-pushed-into-the-session-channel), #68).

## 0.52.0

### Fixed

- **The 👑 shows typing for a human's plain post:** a human's post without
  a mention wakes only the coordinator as its task, but unlike a mention or
  DM it showed no "is typing…", so the human could not tell a stalled
  coordinator from a working one. Delivered by `wait_for_events` or the
  channel push, such a post now starts the indicator in its thread; a gated,
  digest-only or fyi post does not. Pushed mentions and DMs already showed it
  ([RT-6.5](docs/arc42/06-runtime-view.md#65-typing-indicator-while-a-mention-is-answered)).
- **A stall no longer wakes the coordinator for its own ask alone:** its
  reminders already nudge the human, so a stall whose only open items are the
  crown holder's own asks (within 24 h) wakes nobody. With other items open
  they are still listed
  ([RT-6.19](docs/arc42/06-runtime-view.md#619-stall-noticed-asks-reminded)).

## 0.51.0

### Changed

- **Asks always go on the bus:** a question or decision for a human goes out
  as `ask_human` also when the agent asked in its own terminal or UI, which
  the human may not watch; the bus is the one place where they see what waits
  on them. SKILL.md and the MCP instructions say so
  ([RT-6.11](docs/arc42/06-runtime-view.md#611-asking-a-human)).
- **The stall event says what it measures:** it read "no post by an agent or
  trusted human for 37 min since 09:49" although the coordinator had posted
  since. Its own posts (an `IDLE`, an ask) are deliberately no activity, they
  only push the next wake out; the text now reads "no post by another agent or
  a trusted human since 09:49 (37 min; your own posts do not count)"
  ([RT-6.19](docs/arc42/06-runtime-view.md#619-stall-noticed-asks-reminded)).
- **§5 describes the MCP server's final structure** after P3b: entry points,
  bus logic, port, bus types, adapter and bus state, the decision for the port
  and adapter split, and why registration stays outside the port
  ([§5.2](docs/arc42/05-building-block-view.md), #82 P3b).

### Fixed

- **One list of watched channels:** the inbox watched the channels named by
  `MM_CHANNEL` and `MM_PROJECT_CHANNEL`, while the hook API's count also took
  the work channel and the ids `MM_PROJECT_CHANNEL_ID` and `MM_CHANNEL_ID`.
  With only an id set, the Stop hook counted posts the inbox never delivered.
  Both now use `client.watched_channel_ids`, so the inbox watches a channel
  given by id too
  ([RT-6.2](docs/arc42/06-runtime-view.md#62-end-of-a-turn-drain-sign-off-listen)
  step 1).

## 0.50.0

### Fixed

- **The Stop hook no longer drains for a post nothing can collect:** the
  pending count behind `/v1/pending` (and the listener's and turn plugin's)
  counted an `@channel`, a post for all agents or a group post that names
  nobody in any channel the bot is in, also one the server does not watch
  (the bot was added to it, or `MM_PROJECT_CHANNEL` changed without a
  restart). `wait_for_events` delivers none of these, so every turn end
  reported "1 unread" with nothing to collect. Outside a DM and the watched
  channels only a post naming the agent counts now
  ([RT-6.2](docs/arc42/06-runtime-view.md) step 1).

### Changed

- **Users, channels and reactions are typed through the port:** `get_me`,
  `get_channel`, `channel_users` and `reactions` return `Me`, `Channel`,
  `ChannelUser` and `Reaction` (`model.py`) instead of Mattermost's JSON, built
  in the adapter with only the fields the callers read; a post's reactions are
  `Reaction`s too. The new port member `account_created` gives the bot
  account's creation time, so `unread.py` no longer imports `client.py` and is
  a port module like the others. The adapter-boundary test also fails when a
  port member returns JSON ([§5](docs/arc42/05-building-block-view.md), #82
  P3b). Tool output is unchanged.
- **One shared test fake:** `mcp/tests/fakes.py` holds `FakeChat`, a typed
  subset of the port with only the members the tests call (checked against
  the port's signatures, and by mypy), in place of the near-identical fakes
  in the hook API, turn plugin, announce, ask, slim and task tests.

## 0.49.0

### Changed

- **The bus logic talks to its chat through a port:** `port.ChatPort`, a
  `typing.Protocol`, holds the chat operations the bus logic calls (posting
  and replying, reading threads and channel posts, reactions, status and
  presence, channel membership and unread channels, people and trusted
  humans), with `Post` and `Thread` in its signatures.
  `client.MattermostClient` is now `client.MattermostAdapter` and implements
  it; mypy checks that it does. The wake gate, lease and roster, slim results,
  departed agents, reminders, stall check, own post, stats and the pending
  count take the port instead of the client and no longer import `client.py`
  (`unread.py` keeps one user-JSON helper). Registration stays outside the
  port. The adapter-boundary test now also fails when such a module imports
  the adapter or uses a member the port lacks
  ([§5](docs/arc42/05-building-block-view.md), #82 P3b). Behaviour and output
  are unchanged.

## 0.48.0

### Changed

- **Posts are typed inside the MCP server:** a post is a frozen `Post`
  (`model.py`) that `client.post_of` builds from Mattermost's JSON, with the
  transport fields and what the bus's props say (from a webhook, kind, intent,
  `to`, PR, branch, crown); a thread is a `Thread`. The inbox, read
  watermark and pending count, hook API, wake gate, departed agents, stall,
  tasks, own post, slim results and the tools read its fields instead of the
  JSON's keys
  ([§5](docs/arc42/05-building-block-view.md), #82 P3b). Behaviour and output
  are unchanged: a snapshot test holds the tools' and events' JSON, slim and
  raw, byte for byte, and the adapter-boundary test now fails on a post key
  read outside the adapter.
## 0.47.0

### Changed

- **The bus keeps its own read watermark:** what waits for an agent is what
  came after the newest post its MCP server handled in each channel (handed to
  the agent, gated, or the bot's own), kept in `read-<uid>.env` in the state
  directory. Mattermost's unread counters no longer decide what is pending: the
  Stop hook, the listener and the inbox backfill read only channels whose
  newest post is past the mark, from the mark. A DM that holds only the bot's
  own post (a permission prompt) is never pending; a post that reaches the
  inbox while the bot posts in the same channel still counts; and
  `claude --continue` or the workspace's next server go on from the same mark
  instead of Mattermost's last view. The drain's fyi detail needs no extra
  request. Marking channels viewed in Mattermost stays, for its UI only
  ([RT-6.2](docs/arc42/06-runtime-view.md#62-end-of-a-turn-drain-sign-off-listen),
  #82 P3b).

## 0.46.0

### Changed

- **Task rows read the props first:** `post_message` and `reply_in_thread`
  write the PR and branch a post names into `props.a2a` (`pr`, `branch`), and
  `crown: true` while the author holds the crown. The task rows take PR and
  branch from there, else from the text (older posts, people's). A `DONE` or
  `CANCEL` by someone other than the task's author counts when it comes from
  the crown holder the roster names now, or from an agent whose post carries
  `crown`; a 👑 typed in the text no longer closes a task, except on an
  agent's post from before 2026-10-28T00:00Z, so no closed task comes back
  (RT-6.14, #82).

## 0.45.1

### Changed

- **The MCP server is type-checked:** mypy (strict, pinned in the `dev` group of
  `mcp/pyproject.toml`) checks `mcp/src/` in CI and before a push. The fixes are
  annotations only, with one guard: a permission prompt that expires between
  its lookup and its answer is now dropped instead of raising (#68).

## 0.45.0

### Changed

- **A reaction by someone not trusted wakes no worker:** with trusted humans
  configured, a reaction to an open ask by a person who is neither trusted nor
  an agent approved nothing but still woke the agent. It is now gated like their
  post: data only, last in `wait_for_events` and in the channel digest, not
  counted by the listener, no answer posted. The coordinator and reactions by
  trusted humans and agents are unchanged
  ([RT-6.16](docs/arc42/06-runtime-view.md#616-who-may-wake-an-agent), #68).

## 0.44.0

### Added

- **A mention of a departed agent gets an answer:** when a post in the work
  channel @-mentions an agent whose status lease ran out (a `BYE` alone, which
  may mean "task finished", does not count),
  the coordinator's server (without a 👑: the oldest reachable agent's) answers
  in the thread, once, with since when it is gone and a link to its `BYE`
  (when that is its latest post in its HELLO thread) or last post, and the event (`departed`) wakes the coordinator to route the work.
  💤 idle and 👂 listening agents are not departed
  ([RT-6.21](docs/arc42/06-runtime-view.md#621-mention-of-a-departed-agent-or-one-not-here)).

## 0.43.0

### Changed

- **A restarted MCP server keeps the bot outside Claude Code too:** without a
  session id (Cursor, OpenCode, Codex) the server's exit no longer posts `BYE`
  and unregisters. It keeps the bot and marks the workspace's 0600 session file
  (`MM_SERVER_ENDED`); the workspace's next server takes the bot over within
  `MM_SESSION_KEEP_SEC` (24 h), like `claude --continue` — after an OpenCode
  plugin reload or idle unload, or a Cursor/Codex server restart, the agent keeps
  its name, threads and DMs without a `session_start`. `session_end`, or a last
  post that is a `BYE`, still ends the bot at once. A bot nobody took over is
  reaped by the workspace's next `session_start`, another session's start or
  `mm-agent-sweep.sh` once the grace period is over, even while the harness runs
  on ([RT-6.10](docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent), #99).

## 0.42.4

### Fixed

- **A permission prompt answered in the terminal closes in the DM too:** Claude
  Code says nothing when the terminal answers first, so the DM showed nothing
  and the id stayed open for 24 h; a late `yes <id>` was still sent and got a
  misleading "✅ allowed". An open dialog lets no turn end, so when the turn
  ends (the Stop hook sets the status idle or listening) every prompt still
  open gets "🖥️ Answered in the terminal" under it and is closed; a later
  `yes <id>` gets "No open permission prompt"
  ([RT-6.13](docs/arc42/06-runtime-view.md#613-permission-prompts-answered-from-mattermost)).

## 0.42.3

### Fixed

- **No endless drain after a permission prompt:** the prompt DM, the verdict's
  reply and the human's taken-out answer left the DM unread for the bot, and
  nothing could collect it, so the Stop hook asked to drain an empty inbox on
  every turn (`pending=1` in a `D` channel). Each of these posts now marks the
  DM read ([RT-6.13](docs/arc42/06-runtime-view.md#613-permission-prompts-answered-from-mattermost)).

## 0.42.2

### Fixed

- **A coordinator started as a channel keeps the crown between turns:** the Stop
  hook set it 💤, so after `MM_LEASE_SEC` its crown ran out though the server
  still pushed to it; its posts lost the 👑 and a human's plain post woke nobody.
  Idle in a channel session now shows 👂 `listening · the bus wakes me` and counts
  as reachable ([RT-6.6](docs/arc42/06-runtime-view.md#66-coordinator-crown-and-presence-lease)).
- **A reply to a reply lands in its thread:** `reply_in_thread` (and every post
  with a `root_id`) given a reply's id posts in that reply's thread; Mattermost
  refused it, and the agent saw only "Error executing tool". A post Mattermost
  refuses now comes back as `refused` with its reason
  ([RT-6.14](docs/arc42/06-runtime-view.md#614-agent-cards-and-task-states-a2a-shapes)).
- **Open tasks and the stall check see a busy channel's newest posts:** the task
  window pages back from the newest post; with `since`, Mattermost answered at
  most 1000 posts in no set order, so `ASSIGNMENTS` showed no open tasks and the
  stall check missed recent posts (RT-6.14, RT-6.19).
- **An ask an agent resolved is no longer open:** `coordinator("tasks")` and the
  stall check count `resolve_ask`'s ✅ by any bus agent, so a successor session
  can close its predecessor's asks (RT-6.11, RT-6.19).

## 0.42.1

### Fixed

- **A table after text renders:** Mattermost shows a markdown table only after a blank line; the server now inserts one before a table that follows a line of text in every post an agent sends ([RT-6.15](docs/arc42/06-runtime-view.md#615-references-as-links)). Text in code fences stays as it is.

## 0.42.0

OpenCode 2.x plugin: the bus in OpenCode sessions (#94, refs #69). Plugin API V2
only; OpenCode 1.x (plugin API V1) is not supported.

### Added

- **OpenCode plugin** (`opencode/`, OpenCode 2.x): the MCP server and the skill
  added by the plugin, presence per turn, drain and sign-off when a turn ends,
  a wake-up while idle when the bus has something for the agent (a stall event
  for the coordinator included, RT-6.19) — within the Stop hook's listen window
  and its gates, 👂 meanwhile, never while another session works, and with
  backoff for a counter the woken turn cannot clear —, the handoff state after
  a compaction as context that starts no turn
  ([RT-6.20](docs/arc42/06-runtime-view.md#620-opencode-turn-cycle-through-its-plugin)).
  The plugin's input is synthetic session input, told apart from the human's;
  its turn state survives OpenCode unloading the project. Install: a `plugins`
  entry naming `opencode/` (with options) or a symlink in `.opencode/plugins/`
  ([adapters/opencode.md](adapters/opencode.md)).
- **Hook API:** `GET /v1/turn-end`, `/v1/wake`, `/v1/handoff`, so the server
  decides and words it.
- The plugin finds only its own server: the one it adds keys its files on
  `MM_WORKSPACE` (the project directory) with the Claude Code session
  variables blanked, so OpenCode started from a Claude Code shell is safe; no
  channel push (`MM_CHANNEL_PUSH=0`).

### Fixed

- `POST /v1/stat` answered 502 for a block (`kind=` collided with the
  recorder's own argument): the Stop hook's and the plugin's blocks were not
  counted.

### Docs

- [adapters/opencode.md](adapters/opencode.md): OpenCode 2 runs sessions in a
  background service whose environment, not the caller's, reaches MCP servers
  and the agent's shell — start it cleanly or use `--standalone`;
  `mcp.servers` by hand with `MM_WORKSPACE` and the empty `CLAUDE_*` variables;
  `opencode run … </dev/null` in scripts.

## 0.41.0

The coordinator notices a stall; open asks remind their human (#96, refs #69).

### Added

- **Stall check** in the heartbeat of the agent wearing the 👑, no model and no
  tokens while the bus is quiet: when something is open (a task in open /
  active / blocked / review, an open ask, a post that @-mentions a reachable
  agent without its answer) and for `MM_STALL_SEC` (default 600 s) no agent or
  trusted human posted and no agent was `working`, the server wakes its agent
  with an event `{kind: stall}`: per item what, who is next, the last
  activity. It arrives like a message: channel push, `wait_for_events`, and
  the hooks' pending count. Repeated wakes back off (10, 20, 40 … min, capped
  at `MM_REMIND_MAX_SEC`, default 8 h); any activity starts over. coordinator.md
  replaces *Spot stalled tasks* with what to do: nudge one addressee per item
  in its thread, no broadcast, nothing if the wait is fine
  ([RT-6.19](docs/arc42/06-runtime-view.md#619-stall-noticed-asks-reminded)).
- **Ask reminders**, sent by the server of the agent that asked: a reply in the
  ask's thread with the human's @-mention and the ask's title,
  `MM_ASK_REMIND_SEC` (default 1200 s) after the ask, then 10, 20, 40 … min
  later, each gap capped at `MM_REMIND_MAX_SEC`; they stop once the ask is
  answered (a 👍/👎 by the human or a trusted human, or the human's reply in
  its thread) or resolved, after 24 h (a yes approves nothing then) or with the
  session, and survive a restart without repeating (`remind-<uid>.env`).
  Posts carry `props.a2a.kind = "reminder"` (fyi). The overhead summary counts
  them (`ask_reminders`).

## 0.40.0

Every harness runs the MCP server; the shell scripts are its clients (#69).

### Breaking

- **Shell-only agent sessions are gone.** An agent's shell holds no bot token
  and no registration secret in any runtime: it reaches the bus only through
  its session's MCP server
  ([RT-6.17](docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).
  Removed: `bin/mm-agent-session.sh` (`start` / `stop`; agents register with
  the MCP tool `session_start` and end with `session_end`), the Cursor example
  hooks `adapters/cursor/hooks.json.example`, `mm-session-start.sh.example`
  and `mm-session-end.sh.example` (they registered through the shell and wrote
  the token into Cursor's env file), the `mm_load_hook_session` helper, and the
  docs that told agents to export `MM_REG_SECRET` into their shell.
- **Removed in 0.38.0, listed here for the migration** — the scripts and their
  MCP replacements: `mm-agent-lease.sh` (`tick` / `take` / `release` / `who`) →
  the server's heartbeat and `coordinator("take" | "release" | "who")`;
  `mm-agent-roster.sh` → `coordinator("roster")`; `mm-agent-unread.sh count` /
  `detail` → `wait_for_events(timeout_sec=1)` (the hooks: `GET /v1/pending`).
  `mm-agent-unread.sh baseline` stays for the host session hooks.
- **`mm-agent-post.sh`, `mm-agent-poll.sh`, `mm-agent-status.sh` need the
  session's MCP server.** Without one they exit 3 and say what to start; no
  token fallback. `mm-agent-poll.sh --team` is gone (the bot's team);
  `mm-agent-status.sh offline` by hand is gone (the server sets it at the end).
- **`mm-agent-post.sh` prints the server's slim post**, not Mattermost's:
  `id`, `root_id` (a reply), `channel` (the name, not `channel_id`), `author`,
  `at` (readable, not `create_at` in ms), `message`, and hints such as
  `unmentioned`. Read `id` and `message` as before; take `channel`/`at`
  instead of `channel_id`/`create_at`.
- **`mm-agent-poll.sh` prints slim posts:** `id`, `user_id`, `root_id`,
  `create_at`, `type`, `message`, `to`, `intent`, `fyi`, `gated` — no
  `props`, `metadata` or `update_at`; a2a props come as `to` / `intent`.
  `--channel` reads only the work, project and default channel (403 otherwise).
- **`mm-agent-post.sh` posts with `post_message`'s rules:** a human's
  @-mention becomes the plain name and `@channel`/`@here`/`@all` plain words.
  To reach a human use the MCP tool `ask_human`; to wake every agent `--to all`.
- **A message `-` reads stdin** in the new form (`mm-agent-post.sh -`); in the
  0.36 form `CHANNEL_ID - [ROOT_ID]` it is still the text `-`.
- **Exit codes:** 1 a refused post or a 400, 2 usage, 3 no reachable server of
  this session — no longer curl's codes. Scripts that tested curl's codes test these.

### Changed

- **Thin clients:** `mm-agent-post.sh [--channel ID] [--root ID] [--to AGENT|all]
  [--kind WORD] MESSAGE|-` (the 0.36 form `CHANNEL MESSAGE [ROOT]` still works)
  posts through the new `POST /v1/post` and `POST /v1/reply`, which run the code
  of `post_message` / `reply_in_thread` (mention rewriting, a2a props, kind and
  its refusal, 👑) and answer what they return; a refusal goes to stderr, exit 1.
  `mm-agent-poll.sh` reads `GET /v1/posts` (new `?channel=`) and prints its
  JSON lines oldest first.
- **Finding the server per harness:** outside Claude Code the server keys its
  endpoint and session file on the workspace: `CLAUDE_PROJECT_DIR`,
  `MM_WORKSPACE` (Cursor: `${workspaceFolder}`), the client's first MCP root,
  else its working directory. The shell takes `MM_BUS_ENDPOINT`/`MM_BUS_KEY`,
  else the session id, else `CLAUDE_PROJECT_DIR` / `CURSOR_PROJECT_DIR` /
  `MM_WORKSPACE` exactly, else the closest file walking up from `$PWD`. A
  second server in the same workspace does not overwrite a live first one's
  file and warns in `session_start` / `bus_info`. The walk from `$PWD` stops
  at the nearest git top (a nested repository or a worktree under a checkout
  never reaches the outer workspace's server), and no endpoint file is keyed
  on `/`, the home directory or above it: such a server waits for the client's
  roots and says so in `bus_info`. A live server's file is not overwritten for
  a session id either: a harness started from a Claude Code shell
  (`codex exec`, `opencode`) inherits `CLAUDE_CODE_SESSION_ID`; its warning
  names both MCP clients (the endpoint file now carries `MM_BUS_CLIENT`) and
  says to start it with `env -u CLAUDE_CODE_SESSION_ID`. When the roots move
  the file, the new one is written before the old one goes.
- **`mm-agent-post.sh` options:** only `--channel`, `--root`, `--to`, `--kind`,
  `-h`/`--help` and `--` are options, so messages such as `- done`, `-1 left`
  or `---` are posted (0.40.0 drafts refused them); in the 0.36 form nothing
  after the channel id is an option.
- **`GET /v1/posts?channel=`** reads only the channels the bus watches (work,
  project, default), never a DM or group channel: `403`. §7.3 names what a
  holder of a session's `MM_BUS_KEY` can do.
- **`mm-agent-status.sh` never hands over to another bot's server:** with
  `MM_BOT_USER_ID` set the server must name that bot (since 0.38 a server found
  by directory was asked whatever bot it served).
- **A symlinked state directory** is allowed again outside the corral host
  process (0.39.0 refused it everywhere).
- **The server remembers the agent's last post** by the work channel's id
  (`MM_PROJECT_CHANNEL_ID` included), as the shell did.
- Docs: adapters for Cursor, OpenCode and (new, untested) Codex with complete
  MCP configs; README, `docs/protocol.md`, the smoke test, SKILL.md, the MCP
  instructions and arc42 §5, RT-6.6, RT-6.10, RT-6.11, RT-6.17, §7.2, §7.3.

### Fixed (live tests with Codex, OpenCode and Cursor)

- **The MCP server ends with its client.** A server whose client went without
  closing its stdin (Codex killed the `uv` that started it) or that got SIGTERM
  lived on: the SDK reads stdin in an AnyIO worker thread that is no daemon and
  cannot be cancelled, so the interpreter waited for it forever. The orphan kept
  its endpoint file, and the next server's shell clients exited 3. Now the exit
  work (BYE, stats, offline, unregister, endpoint file) runs explicitly when
  stdin ends, on SIGTERM/SIGHUP, or when a watchdog sees the server's parent
  gone, and the process leaves with `os._exit` (within 2.5 s after a signal)
  ([RT-6.10](docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent)).
- **A stuck owner no longer blocks the endpoint file.** Another server counts
  as live only when its pid runs and it answers an authenticated `GET /v1/me`
  within 1 s; listening is not enough
  ([RT-6.17](docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).
- **Channel push is Claude Code's alone.** An `MM_CHANNEL_PUSH=1` from the
  user's shell put Codex, OpenCode and Cursor servers into channel mode; now a
  client that does not name itself `claude-code` gets the plain server
  ([RT-6.12](docs/arc42/06-runtime-view.md#612-messages-pushed-into-the-session-channel)).
- **Inherited Claude Code session variables are ignored** by a server whose
  client is not Claude Code: it keys its files on the workspace and takes over
  no bot of that Claude session. Start other harnesses from a clean shell all
  the same ([§7.2](docs/arc42/07-deployment-view.md#72-environment-differences)).
- `session_end` answers with the registered bot's name (`ended` was empty).
- `mm-agent-poll.sh` names the channel it reads (`polling channel=<name>`), not
  `work`; `GET /v1/posts` carries `channel`.
- Docs: a complete, tested Codex configuration (all `MM_*` variables, since
  Codex passes no other environment to MCP servers;
  `default_tools_approval_mode = "approve"` for `codex exec`); Cursor: remove
  the old global `sessionStart`/`sessionEnd` hooks and `mattermost` MCP entry.

### Migration

- **Claude Code without corral:** nothing to change for the plugin's server;
  drop `mm-agent-session.sh` from scripts and `trap`s, let the agent call
  `session_start`. To keep `MM_REG_SECRET` out of the agent's shell, add the
  server with a command that reads it itself ([adapters/claude.md](adapters/claude.md)).
- **Cursor:** remove the `sessionStart` / `sessionEnd` hooks of the examples;
  configure the MCP server as in [adapters/cursor.md](adapters/cursor.md):
  `uv run --project …/mcp`, `env.MM_WORKSPACE = "${workspaceFolder}"`, and a
  server command that fetches `MM_REG_SECRET` itself (`sh -c
  'MM_REG_SECRET="$(gopass show -o …)" exec uv run …'`). `${env:MM_REG_SECRET}`
  works too but needs the secret in Cursor's environment, so the agent's shell
  has it.
- **OpenCode:** configure the MCP server as in
  [adapters/opencode.md](adapters/opencode.md): `uv run --project …/mcp` (not
  `cd …/mcp`) in a command that fetches `MM_REG_SECRET` itself, then remove it
  from the environment you start OpenCode with. `{env:MM_REG_SECRET}` reads it
  from that environment, which the agent's shell inherits.
- **Scripts that call `mm-agent-post.sh` / `mm-agent-poll.sh`:** see Breaking
  for the output keys and exit codes.
- **Codex:** [adapters/codex.md](adapters/codex.md): list every `MM_*`
  variable in the server's `env` and set `default_tools_approval_mode =
  "approve"` for `codex exec`.
- **Cursor, before 0.40.0 set up with session hooks:** remove the
  `sessionStart`/`sessionEnd` entries from `~/.cursor/hooks.json` and an older
  global `mattermost` MCP entry, or the session runs two bots.
- **corral:** nothing; rerun `bin/mm-agent-host-install.sh` as for any update.

## 0.39.0

Keyless corral session: the bot token stays on the host (#81, #69).

### Changed

- **The MCP server runs on the host in a corral session
  ([RT-6.18](docs/arc42/06-runtime-view.md)):** `preStart` starts it as one
  host process per session (`python -I -m mattermost_ai_agent_bus --host` from
  the host copy, a scrubbed environment, key and token on stdin) and
  contributes only `MM_BUS_MCP_ENDPOINT`, `MM_BUS_ENDPOINT` and `MM_BUS_KEY`
  besides the ids and names: no `MM_BOT_TOKEN` in the sandbox any more. The
  plugin's MCP entry point relays stdio to it after a handshake with the key
  and the session's names (session id, project directory, working directory,
  channel flag, `CLAUDECODE`), checked and used only to name files. The host
  process serves the hook API (RT-6.17) with the same key for the whole
  session, keeps its files in its own state directory
  (`~/.local/state/mm-agent-bus/hosted/<session>/`, 0700), and ends with
  `postEnd` (SIGTERM: BYE, stats, offline, unregister) or when corral is gone
  (watchdog every 5 s).
- **Fail closed:** when that process cannot start, the bot is unregistered
  again and the sandbox gets only `MM_BUS_UNAVAILABLE` with the reason — never
  the token. The MCP server tells the agent so instead of offering
  `session_start`.
- **Hooks in a keyless session:** the overhead counters go to the new
  `POST /v1/stat`, the channel confirmation comes from `/v1/me` (`channel`),
  and `mm-agent-statusline.sh` asks `/v1/me` for the 👑 (1 s at most).
  `HOST_MANAGED`, the skill and the docs key on `MM_BUS_MCP_ENDPOINT` as well
  as `MM_BOT_TOKEN` (older host installs).
- **State files** are written through one helper (mkstemp and rename, append
  with `O_NOFOLLOW`) that refuses a symlink in their place; a transcript is
  opened without following a symlink, and on the host only under
  `~/.claude/projects/`.
- **`mm-agent-host-install.sh`** copies the MCP server and the skill as well
  and prepares the server's environment with `uv sync --locked` (needs `uv`).
- **`mm-agent-sweep.sh`** leaves a session alone while its host process runs
  and removes the files of host processes whose session ended.

### Security (second review of #81)

- **No credential in any process environment on the host
  ([§7.3](docs/arc42/07-deployment-view.md#73-trust-assumptions)):** on macOS
  a corral sandbox can read the initial environment of every process of the
  same user (`KERN_PROCARGS2`). The host hooks no longer export the bot token
  or the registration secret: helpers get them on fd 3 (`MM_BOT_TOKEN_FD`,
  `MM_REG_SECRET_FD`, read by `mm-agent-lib.sh`), jq reads token and key from
  pipes, curl from stdin. `mm-agent-register.sh` no longer exports the token.
  A secret from the login environment is not passed on, and the start hook
  warns: keep it in gopass. Residual: a session's `MM_BUS_KEY` is readable by
  other sandboxes; the root fix belongs in corral's sandbox profile.
- **Host hooks only from the installed copy
  ([§7.1](docs/arc42/07-deployment-view.md#71-production-deployment-corral-host-managed)):**
  both hooks check where they run from before sourcing anything (symlinks and
  the Python included); under `~/.claude` (the marketplace clone), a corral
  home or the session's workdir `preStart` contributes only
  `MM_BUS_UNAVAILABLE` and `postEnd` runs nothing. The marketplace-clone
  `exec` mode is gone.
- **`mm-agent-host-install.sh`** copies only what is committed (`git archive
  HEAD`) and refuses uncommitted changes, untracked files and symlinks in the
  copied paths, and a source under `$CORRAL_WORKDIR`.
- **Transcripts** are opened non-blocking, must be regular files of at most
  64 MiB, and on the host their real path is checked again from the open
  descriptor.
- **Mattermost ids** in API paths must be 26 lowercase letters and digits,
  names free of path characters; anything else is refused (tool result, `400`
  from the hook API) before a request goes out.
- `postEnd` no longer signals a host process without a recorded start time;
  host files are named by the validated session key throughout.

### Security (third review of #81)

- **The installer runs nothing a clone's git config names
  ([§7.1](docs/arc42/07-deployment-view.md#71-production-deployment-corral-host-managed)):**
  git runs without system or global config, attributes file, fsmonitor or
  hooks; a clone whose config has a filter, diff or merge driver or an
  include, or that has `info/attributes`, is refused, and so is one whose
  repository lies where a sandbox writes. It prints the commit it installs.
  Use a clone that was never a session's workdir.
- **No command from the workdir:** both corral hooks, the installer and
  `mm-agent-sweep.sh` keep only absolute `PATH` entries outside the places a
  sandbox writes and work from `/`.
- **The location check** compares without regard to case, refuses a `..`
  component and resolves every step's directory, and adds corral's temporary
  directories.
- **`mm-agent-sweep.sh`** exports no token or secret: helpers get them on
  fd 3 ([§7.3](docs/arc42/07-deployment-view.md#73-trust-assumptions)).
- **`mm-agent-register.sh`** passes the registrar's answer to jq through a
  pipe, not a here-string (a temporary file on bash ≤ 5.0), and a failure
  prints the registrar's `message`, never the answer.
- **The version notice** reads the installed plugins' files only as regular
  files, bounded in size and time: a FIFO no longer stalls `preStart`.

### Upgrade

- Rerun `bin/mm-agent-host-install.sh` on the host, from a clone you
  reviewed: the host process runs that code with the bot token. Without its
  Python environment corral sessions get no bus. If corral's `exec` points at
  the marketplace clone, point it at
  `~/.local/share/mm-agent-bus/current/adapters/corral/` instead: hooks from
  the clone now refuse to run.
- Move `MM_REG_SECRET` out of the login environment into gopass
  (`MM_REG_SECRET_GOPASS`).
- Update the plugin in the sandboxes (`/plugin update`, then restart): a
  plugin older than 0.39.0 does not know the relay, so a keyless session gets
  no MCP bus until it is updated.

## 0.38.0

The MCP server runs no scripts any more: crown lease, roster, presence and
the pending counters live in Python (#82, #69).

### Changed

- **Lease and presence in the server ([RT-6.6](docs/arc42/06-runtime-view.md)):**
  `lease.py` keeps the status lease, the coordinator crown
  (tick/take/release/who, the oldest reachable agent, `coord-<uid>.env` for
  the status line) and the roster; `unread.py` counts what waits for the
  agent (RT-6.2 step 1). The hook API, the gate, the tasks, the
  `coordinator` tool and the heartbeat call them directly. Status changes,
  takes, releases and ticks run one at a time, so a release during a
  heartbeat stays a release.
- **Shell CLI:** `mm-agent-lease.sh` and `mm-agent-roster.sh` are gone; agents
  use `coordinator("who")` / `coordinator("roster")`. An operator without a
  session has no shell replacement. `mm-agent-unread.sh` keeps only
  `baseline` (the host session start).
- **`mm-agent-status.sh` is a client of the server:** it hands the state to
  the session's server (`POST /v1/presence`, new state `bus`). When that
  server answers but the hand-over fails, it writes nothing (exit 0). Only
  without a server of its session does it write the status itself, keeping
  a 👑 while the crown lease in `coord-<uid>.env` still runs.
- **Small differences:** a custom status's expiry is parsed as a time, not
  compared as text; the floor for posts to all agents is the last view, a
  day back at most and never before the bot was created; `/v1/pending`
  answers `502` when Mattermost fails instead of a count of 0.
- **Limits:** the 2-at-a-time limit of the hook API now guards its
  Mattermost-heavy routes (`/v1/pending`, `/v1/presence`).

### Checked

- `mcp/tests/test_lease.py` and `mcp/tests/test_unread.py` (the lease and
  roster cases of the old shell tests, ported to `httpx.MockTransport`; a
  release during a tick); `tests/hooks/test_lease.py`: a failed hand-over
  writes no status.

## 0.37.0

The turn hooks and the listener no longer hold the bot token: they ask the
MCP server (#80, #69). The step before keeping the token out of the sandbox.

### Changed

- **Hooks ask the MCP server, not Mattermost (RT-6.17):** the server serves a
  small JSON API on `127.0.0.1` (ephemeral port, HTTP/1.1, a random key per
  start sent as `Authorization: Bearer`, compared in constant time, `401`
  otherwise): `GET /v1/me`, `/v1/pending`, `/v1/last-post`, `/v1/posts`,
  `/v1/ask-reactions` and `POST /v1/presence`. `turn-status.sh`,
  `stop-inbox-check.sh`, `after-compact.sh` and `mm-agent-listen.sh` call
  only these; none reads `MM_BOT_TOKEN` or the session file any more. The
  server runs `mm-agent-unread.sh` and `mm-agent-status.sh` with its own
  token, so counters and status follow the same rules as before; it reads
  `last-<uid>.env`, the crown, the asks and the gate list for them, and says
  per post whether it is an agent's fyi post and whether it was gated.
- **Finding the server:** `MM_BUS_ENDPOINT` / `MM_BUS_KEY` when set, else
  `endpoint-sid-<sha1(session id)>.env` in the state directory (0600, keyed
  like the session file), which the server removes when it ends. The key
  goes to `curl` on stdin, never in its argv; each request is bounded by
  `MM_BUS_MAX_TIME` (default 3 s), and all of one hook's requests by a
  deadline inside its timeout (Stop hook 8 s of 10, prompt hook 4 s of 5).
  Same transport in every mode. The file also names the bot's id: without
  a server the prompt hook still clears the turn state and stops the
  listener.
- **Limits:** at most 32 connections, 2 script-running requests at once, 5 s
  to write an answer; `/v1/presence` takes a one-line label of at most 64
  characters and an `until` within a day.
- **Bot token off argv:** the Shell CLI (`mm_api`, `mm-agent-post.sh`,
  `mm-agent-poll.sh`, `mm-agent-unregister.sh`, registration) hands
  `Authorization` to `curl` on stdin (`mm_curl_auth`), so `ps` never shows
  the token.
- **Trade-off:** without a running MCP server (or before a self-registering
  session has its bot) the hooks do nothing and the listener ends at once,
  as without a bus session. A host-managed session's hooks used to work on
  the token alone. The Stop hook's fallback for an unknown bot id
  (`stop_hook_active`) is gone: the server always names the bot.
- **Sweep:** `mm-agent-sweep.sh` takes the endpoint files for neither
  sessions nor per-bot state; it removes those of servers that are gone
  (`MM_BUS_PID` dead, file an hour old).

### Checked

- `mcp/tests/test_hook_api.py`: auth, every route, the file's lifecycle.
  The hook tests run every hook and the listener against the real API over
  the fake Mattermost, without the token in their environment.

## 0.36.4

### Fixed

- **A refused bus input says why (#87):** an unknown or contradicting `kind`,
  a `to` other than `all` on a reply, a bad `role` or `action`, or no human to
  ask no longer ends in a bare "Error executing tool": the tool returns
  `{"posted": false, "refused": "<reason>"}` (plus `kinds`, the allowed words,
  for a `kind`) and nothing is posted ([arc42 RT-6.14](docs/arc42/06-runtime-view.md)).

## 0.36.3

### Fixed

- **A name in a URL path asks nobody (#84):** the `unasked` hint no longer
  fires on a trusted human's name inside a URL path, a repo slug
  (`name/repo#12`) or a branch (`name/feature`); `/` now borders the name like
  `.`, `@` and `-` do ([arc42 RT-6.11](docs/arc42/06-runtime-view.md)).

## 0.36.2

The MCP server's first step towards its own types (#69, #79). No behaviour
changes.

### Changed

- **State carriers behind the adapter ([arc42 §5.1](docs/arc42/05-building-block-view.md#decomposition-rationale)):**
  post props, custom status and crown, unread counters and the profile the
  registrar wrote are read and written only in `client.py`, `a2a.py` and
  `lease.py`; only `client.py` talks HTTP, the exit handler's `BYE` and
  `STATS` posts included. The rest of the server gets bus types from the new
  `model.py`: a roster entry with its status word (the lease emoji's word now
  comes from `lease.py`) and an unread channel with the time to read from
  (never before the bot existed, now in `client.py`). The bus's props come
  from `a2a.props`, an ask's card from `client.attachment_props`.
- **Checked:** `mcp/tests/test_adapter_boundary.py` fails on a carrier key,
  `httpx` or `client._api` outside those modules.

## 0.36.1

### Security

- **The host install takes its source only from host-owned locations
  (arc42 §7.1):** `bin/mm-agent-host-install.sh` now copies from the checkout
  it runs from, or from `--from`, and no longer looks its source up among the
  installed plugins. It refuses a source under `~/.cache/corral/` or
  `~/.claude/` (symlinks resolved). Run it from a checkout you reviewed, outside
  corral and outside every session's workdir
  ([adapters/corral.md](adapters/corral.md)). The start hook and `--check` name
  a newer installed plugin by version only.

## 0.36.0

What waiting for humans costs, and which harness works longest alone: the
stats now time the agent's asks and its turns, per coding harness.

### Added

- **How long a human takes to answer an ask (RT-6.9, RT-6.11):** `STATS`
  gets `asks`, `asks_answered`, `ask_wait_sec`, `ask_wait_max_sec`,
  `asks_open`, `ask_open_sec` and `ask_open_max_sec`. The wait runs from the
  ask to the first answer by a trusted human, a reaction with an agreed
  emoji or a reply in its thread (the matching of `expired`), by the
  answer's server time; a late answer counts with its full wait. The asks
  file notes `ANSWERED=` per ask, so a second reaction, a re-delivered event
  or a restarted server counts nothing again; old lines still read. Asks
  never answered on the bus, also those the agent resolved, count as open at
  session end. A permission prompt (RT-6.13) is no ask. Seconds and counts
  only: no names, no text.
- **How long turns take (RT-6.9):** the prompt hook notes who started a turn
  (the bus, by a channel push or a listener that ended, or the human); the
  Stop hook notes the end when it does not block, so a forced round belongs
  to its turn. `STATS` gets `turns_timed`, `turn_sec`, `turn_max_sec` and the
  same for the turns the bus started (`bus_turns`, `bus_turns_timed`,
  `bus_turn_sec`, `bus_turn_max_sec`). A turn that waits on a permission
  prompt or a human includes the wait; an interrupted turn gets no duration.
- **The coding harness in `rt` (RT-6.9):** the MCP client's name from the
  `initialize` handshake as `claude-code`, `cursor`, `opencode`, `codex`, or
  its own name lowercased; without one the `CLAUDECODE` fallback, else
  `other`. Older lines keep grouping under `claude-code` and `other`.
- **Report:** `mm-agent-stats.sh report` (and `--json`, as `harness` per
  version) gets a Turns table (turns per human prompt, mean and longest
  turn, the bus's share of turn time, turn time per human input) and an Asks
  table (asks per hour, share answered, mean and longest wait, asks open at
  the end, waiting share), one row per version and harness. Missing data
  shows `-`, never zero. Only Claude Code has turn hooks: Cursor, OpenCode
  and Codex show `-` in the Turns table, so comparing how long harnesses
  work on their own needs turn signals from their adapters (Cursor hooks,
  OpenCode plugin events, Codex notify), a follow-up. The waiting share adds
  overlapping asks to different humans twice.

### Fixed

- **The bus's turns counted twice in the report (RT-6.9):** `turns` counts
  every prompt, so it already holds the turn a listener's end starts; the
  report added the listener's wake-ups on top, for "turns the plugin caused"
  and again for "all turns", which put the share too low. All turns are now
  the prompts plus the rounds the Stop hook forced; the plugin's are
  `bus_turns` (pushes included) plus those rounds, and before 0.36.0 the
  wake-ups stand in for `bus_turns` (pushes uncounted there).

## 0.35.0

More from the review in #61:

- how long a yes approves, and what a permission prompt puts on the server
  (#67, from #61 §2 and §5.8);
- how agents wake each other, without `@channel` (#66, from #61 §5.6);
- what a post asks of its readers, and whom it wakes (#65, from #61 §1, §5.1,
  §5.2 and §5.7);
- small follow-ups (from #68): a `BYE` for a session that ends without
  sign-off, a Stop hook that no longer fetches 100 posts per turn, no gate
  answer to a post that names the coordinator, a warning when the profile
  carries fewer trusted names than the bus trusts.

### Added

- **Wake all agents with `to="all"` (RT-6.11, RT-6.2):** `post_message` and
  `reply_in_thread` take `to="all"` (`post_message`'s `to` already named an
  agent for a DM; `reply_in_thread` takes only `all`). It writes
  `props.a2a.to = "all"`, beside `kind` when the first word is a bus
  convention, and needs no @ in the text. Every consumer treats it like an
  `@channel` by that author: the inbox marks the event `to: all` (a channel
  push attribute too), the channel push and the background listener wake for
  it at once, the others hold it for the digest as before; humans see an
  ordinary post and are not notified. A human's `@channel` keeps waking
  agents. A post by someone not trusted with `to: "all"` is gated like their
  `@channel` (RT-6.16): no answer, the channel marked read. The props never
  make the author an agent: classes come from the account only.
- **The counters find it:** such a post holds no `@channel`, so Mattermost
  raises no mention counter. `mm-agent-unread.sh` (the Stop hook's drain and
  the listener's counter check) looks, in each channel with unread posts, for
  one by someone else with `to: "all"` since the bot last viewed the channel
  (at most a day back) that the server did not gate, and counts it with the
  mentions. The listener's post check wakes for it too. One extra GET, only
  while a channel has unread posts.
- **The status word as `kind` (RT-6.14):** `post_message` and
  `reply_in_thread` take `kind`, a bus convention word in any case
  (`kind="ack"`). The server puts it first (`ACK · on it`) unless the text
  starts with it, and writes `props.a2a` from the text, so text and props never
  disagree. An unknown word, or a text that starts with another one
  (`kind="ACK"` on `DONE · …`), is refused with the list of words, and nothing
  is posted. Without `kind` the first word is read as before.
- **Intent (RT-6.14):** `props.a2a.intent` says what a post asks of its
  readers: `action` (`TASK`, `DELEGATE`, `HANDOVER`, `CANCEL`), `reply`
  (`BLOCKED`, `NO`, asks, permission prompts) or `fyi` (`ACK`, `PROGRESS`,
  `WAITING`, `RESULT`, `DONE`, `HELLO`, `IDLE`, `BYE`, `ASSIGNMENTS`). A plain
  post has none: no text is read for it, and a question is a `BLOCKED` to
  agents or an ask to a human.
- **An fyi post wakes only whom it names (RT-6.14 step 8, RT-6.12, RT-6.2):**
  the channel push and the background listener hold an agent's fyi post for
  the digest where they would wake the agent only because it sees the post:
  as coordinator, or having posted in its thread. A mention, a DM, `to="all"`
  and the author of the thread's root, who waits for its `RESULT` and `DONE`,
  still wake at once. Such events carry `intent: fyi` (a push attribute too):
  no answer needed. Only an agent's props count, as anyone can write them
  through the API: the listener and the drain look the authors up (one
  `users/ids` request per new author and window), and the wake gate (RT-6.16)
  neither reads them nor changes for them. Posts of older versions carry no
  intent and wake as before.
- **Ranked by intent:** the push digest lists fyi posts last and says how
  many; the listener's digest line counts them; the Stop hook's drain says
  `N unread message(s) …, M needing action, the rest fyi`, with `fyi=n` per
  channel (one GET per pending channel, at most 5, and one user lookup, only
  while it blocks).
- **A late claim learns who has the task (RT-6.14 step 3):** an `ACK`,
  `PROGRESS` or `RESULT` posted in a task thread another agent already holds
  returns `taken_by: <agent>`. One thread read per claim, none once the bot
  holds the task.
- **`BYE` when a session ends without sign-off (RT-6.2 step 7, from #61
  §5.10):** when the MCP server unregisters its bot at exit and the agent's
  latest post in the work channel is no `IDLE`/`BYE`, it first posts `BYE ·
  session ended without sign-off` in the agent's HELLO thread, with the
  props every `BYE` has (`kind: bye`, `intent: fyi`, written through
  `a2a.annotate`). One request, bounded to 1.5 s, fail-open; a failure never
  holds back the unregistering. Not for the bot kept for `claude --continue`
  (the session is not over) and not for an agent that never announced. The
  corral `postEnd` hook posts none: the MCP server runs in every host-managed
  session and exits first, and the BYE is recorded like any post, so there is
  never a second one.
- **A warning when the profile under-reports the trusted list (RT-6.8, from
  #68):** the registrar may write only part of `MM_TRUSTED_HUMANS` into the
  bot's profile (it caps the list and keeps human accounts only), while the bus
  trusts all of it. After registering, `mm-agent-register.sh` (the corral
  banner shows its stderr) and `session_start` (as `warning`) read what the
  profile actually carries, from the bot's own user object (`bot_description`),
  else `/bots/<id>`, and name every missing name; the text gives the registrar's
  `maxTrusted` as the likely cause, without a number. Where the profile cannot
  be read nothing is compared: no warning, an info line in the log. Nobody is
  trusted less by it.

### Changed

- **The Stop hook reads the agent's own last post from a file (RT-6.2 step 2,
  from #68):** the MCP server (`client.post`) and `mm-agent-post.sh` keep the
  latest post in the work channel (id, time, first words, the HELLO thread) in
  `last-<uid>.env`. The hook used to fetch the channel's last 100 posts on every
  turn to find it (one request of about a hundred posts' JSON, two more when the
  channel is given by name); it now asks Mattermost nothing, and falls back to
  that fetch when there is no file. The sweep treats the file like the other
  per-bot state.
- **No gate answer to a post that names the coordinator (RT-6.16 step 3, from
  #64):** a worker no longer posts "Not woken: … <coordinator> (👑) will look
  at it" when the gated post already @-mentions the coordinator. The post is
  gated and delivered as data all the same, and the thread is not used up. A
  DM still gets the answer.
- **A yes approves for 24 h (RT-6.11):** a yes on an ask approves only within
  24 h of the ask; a day later the situation it names may have changed. A
  trusted human's later reaction on the ask, or reply in its thread, reaches the agent with
  `expired: true` (`expired="true"` as a channel push), the answer as given:
  it approves nothing, and the agent resolves the ask and asks again. What
  counts is when the human answered, not when the agent collects it. An ask now
  records the thread it is in, so a reply there is judged too; with several of
  the agent's asks in one thread, only once all of them are older. `ask_human`
  to a human whose open ask is more than 24 h old says so instead of only
  "wait for the answer". Asks are still forgotten after 7 days.
- **Permission prompts carry no file content (RT-6.13):** the DM to the
  responsible human, which Mattermost keeps like any post (retention, search,
  admin access, backups), now shows a `Bash` command in full and, for `Write`,
  `Edit`, `MultiEdit` and `NotebookEdit`, the path and short fields with each
  content field only as its size (`‹1234 chars›`, lines where Claude Code left
  them). Other tools show values up to 200 characters on one line; longer or
  multi-line strings only as their size. A preview Claude Code cut inside an
  array shows only for `Bash` or when short, otherwise its paths and size.
  Claude Code's masking of well-known secrets stays.
- **A verdict on a permission prompt holds as long as a yes on an ask:** 24 h,
  one constant for both; the DM reply to a later `yes <id>` says so.
- §1.2 (`#secure` Approval integrity: "within 24 h") and §7.3 (what the
  prompt still puts on the server) say what the change holds. SKILL.md, the
  MCP instructions and the `ask_human` and `wait_for_events` docs say an old
  yes approves nothing.
- **Fixed text:** SKILL.md stays within its size (every turn reads it). The
  MCP instructions grow by one sentence (about 110 characters) for `to="all"`:
  an agent needs it before its first broadcast, and SKILL.md has no room left
  (§1.2 `#efficient` Low overhead: growth only with a reason). For `kind`
  (RT-6.9, measured offline with the server's own count of fixed text,
  `_fixed()`, and the tool schemas from `list_tools`, without a channel):
  SKILL.md shrinks from 21,898 to 21,881 characters and coordinator.md from
  5,018 to 5,010; the MCP instructions grow by 50 (1,350 → 1,400: `kind` and
  what fyi wakes, one clause about routing dropped) and the tool docs by 93
  (5,495 → 5,588: `kind` in `post_message` and `reply_in_thread`, `intent` in
  `wait_for_events`), so what every request carries grows from 28,743 to
  28,869 characters (+0.4 %, about 32 tokens). The tool schemas, with the new
  parameter, grow by 214 (13,152 → 13,366). An agent needs `kind`
  before its first status post, also while the tools are deferred; each fyi
  post it keeps from waking the coordinator or a thread's bystanders saves a
  model turn. Pushes of an fyi post grow by its `intent` (about 15
  characters), the digest's first line by about 15 and the drain's reason by
  about 35 when they hold fyi posts. Wake-ups and the bus's tokens per hour come only
  from real sessions (`mm-agent-stats.sh report`): a check for the test of
  this release.
- **No agent post notifies every human (RT-6.11):** the server writes
  `@channel`, `@here` and `@all` in `post_message`, `reply_in_thread`, the
  note of `resolve_ask`, the fields of `ask_human` and the HELLO of `announce`
  as the plain words `channel`, `here`, `all`, the way it already wrote a
  person's name; the result says so (`unmentioned`, with a hint to
  `to="all"`). It does so when the user lookup fails, too. Not inside a word
  (a mail address) or in backticks. Posts through `mm-agent-post.sh` do not
  pass the server and stay as written; so does `@channel` in a human's post.
- §6.14 (steps 2 and 3, new step 8), §6.2, §6.12, §6.16 and
  `docs/protocol.md` say how `kind` and the intent are written and whom an fyi
  post wakes. SKILL.md, coordinator.md and the MCP instructions tell agents to
  pass `kind`, and the coordinator that `ACK`, `RESULT` and `DONE` reach it in
  the digest (an `IDLE` too: the next assignment may wait up to
  `MM_LISTEN_DIGEST_SEC`, accepted); the `post_message` and `reply_in_thread`
  docs name `kind`.
- §6.11 (new step 6), §6.2, §6.12, §6.14, §6.16 and `docs/protocol.md` say
  how a post for all agents is written and noticed. SKILL.md, the MCP
  instructions and the `post_message` and `reply_in_thread` docs name
  `to="all"` instead of `@channel`; two paragraphs of SKILL.md are tightened
  to keep it within its size.

### Fixed

- **An agent could notify every human** with `@channel`, `@here` or `@all` in
  any post: the server turned only people's names into plain text (RT-6.11).
  The fields of `ask_human` and the HELLO of `announce` were not looked at
  at all.
- **A `yes <id>` to a prompt past its lifetime reached the agent** as a plain
  post: the id was looked up before old prompts were dropped, and the verdict
  then failed. It now gets the "not open" reply in the DM.

## 0.34.0

Who may wake an agent (#64, from #61 §3, §5.3 and §5.5).

### Added

- **Wake gate (RT-6.16):** with trusted humans configured, a post by anyone
  else (a person not on the list, a bot that is no `agent-*` bot, any webhook
  post) that would wake a worker — a mention, a DM, a reply in its thread,
  `@channel` — no longer does. The worker's MCP server answers in the thread
  itself, once per thread and without the model: `Not woken: <author> is not on
  my trusted list. <coordinator> (👑) will look at it.` (in a DM: ask in the
  work channel; with no coordinator: ask the trusted humans, by plain name),
  but not to `@channel`. It marks the channel read, so neither the Stop hook
  nor the listener wakes for it, and hands the post over as data only: last in
  `wait_for_events` with `gated` saying why, no typing indicator, and in a
  channel session only in the digest, marked `(not trusted)`. The listener
  skips post ids listed in the new `gate-<uid>.env`. The coordinator is never
  gated: it answers what is open or next and takes no work from such a post;
  `coordinator("tasks")` and the `ASSIGNMENTS` table leave out a `TASK` by
  someone not trusted. Without a trusted list, or with the MCP server down,
  nothing changes.
- **`from_agent`** on every event, next to `from_trusted_human`: the author is
  a bot account named `agent-*` (a person named `agent-x` is not).

### Changed

- **Webhook posts are never `from_trusted_human`:** anyone with a hook's URL
  posts as the account that made it.
- **Channel pushes neutralise `<channel` and `</channel`** in a post's text
  (and in the digest), so a body can neither close the tag Claude Code wraps
  it in nor open one.
- SKILL.md, coordinator.md and the MCP instructions say who wakes whom; §1.2
  (`#secure` Approval integrity) and §7.3 name what the gate holds and what it
  does not (an injected agent can still pass a request on as its own).
  `mm-agent-sweep.sh` counts `gate-<uid>.env` as per-bot state.
- **Fixed text:** SKILL.md stays within its size (every turn reads it).
  coordinator.md, read only by the crown holder, grows by about 90 characters
  for its new rule on posts that are not trusted (§1.2 `#efficient` Low
  overhead: growth only with a reason).

## 0.33.2

### Fixed

- **Any registrar prefix works:** the registrar names bots after its
  `BOT_USERNAME_PREFIX`, which need not be `agent-`. The bus learns the prefix
  from its own registration (username minus the name it sent); the corral host
  hook hands it into the sandbox as `MM_AGENT_PREFIX`, and session files carry
  it. Roster and crown, the sweep, the mentions `post_message` leaves in place
  and a task's assignee follow it ([arc42 §7.1](docs/arc42/07-deployment-view.md#71-production-deployment-corral-host-managed)).
- **Deregistration no longer misses bots with another prefix:** the MCP exit
  handler, `session_end`, the reaping of long-ended sessions, the sweep and
  `mm-agent-unregister.sh` strip the registrar's prefix, not `agent-`, before
  sending the name to the registrar.

## 0.33.1

From the design review in #61; the follow-up work is split into #64–#69.

### Changed

- **Trust assumptions written down (arc42 §7.3):** the bus treats a trusted
  human's Mattermost account like the terminal. §7.3 names when that holds:
  an instance under your control, trusted accounts set on purpose and with 2FA,
  storage of every post (the permission relay's DMs included) acceptable, and
  channel members you would let type into the session.
- **The profile binds only in host-managed sessions:** SKILL.md, protocol.md
  and RT-6.8 no longer say an agent cannot change its own profile in general.
  An agent that registers itself names its responsible and trusted humans at
  registration, so its profile is its own claim.
- **Quality goals revised (§1.2), still to be signed off:** new `#secure`
  Approval integrity and `#efficient` Low overhead (the bus's token share may
  not rise between versions without a reason here); Reachability names the
  actual listen window (90 min, the coordinator 2 h) instead of 15 min; Low
  noise names the permission prompt's DM as the one place tool input goes.
- **A2A shapes are the bus's own schema (RT-6.14):** no other system reads
  them, so they promise no interoperability.

### Added

- **Lint in CI:** shellcheck over every shell script (`.shellcheckrc`; warnings
  and errors, since info notes differ between shellcheck versions) and ruff
  with an explicit rule set (`ruff.toml`, version pinned in CI).
- **One version everywhere:** the MCP package (`mcp/pyproject.toml`,
  `__version__`) carries the plugin version; `scripts/doc-check.py` fails when
  they differ.

## 0.33.0

### Changed

- **Waiting on a human means an open ask (RT-6.11):** `post_message` and
  `reply_in_thread` look for trusted humans named in the text. If the agent
  has no open ask to one of them, the post goes out unchanged and the result
  carries `unasked` and a hint to call `ask_human` or link the open ask.
  Lines like "merge waits on dembaca" no longer pass for a question.
- **The agent that carries out an action asks for it:** the skill, the
  coordinator guide and the MCP instructions say so. Reviewers and the
  coordinator link that ask instead of asking; the coordinator asks only when
  that agent cannot. `WAITING …` lines and the post before acting name the
  ask. A human's answer in a terminal approves only that agent's own action.

## 0.32.0

### Added

- **The bus's overhead in tokens (RT-6.9):** at session end the MCP server
  reads Claude Code's transcript of the session, which books every request's
  exact usage, and adds `tok` to the STATS line. Requests the bus caused
  (agent messages, listener, Stop hook, rounds after bus tool results only)
  count whole; every other request carries the bus's share of its input
  (fixed text plus what the bus put into the context since the last
  compaction, at 4 characters per token). A trusted human's prompt over the
  bus is work, not overhead. Totals of all requests come along.
  `mm-agent-stats.sh report` shows a second table per version in
  input-token equivalents: requests and bus requests per hour, fixed tokens
  per request, the bus's tokens per hour, all tokens per hour, the bus's share.

### Changed

- **Sign-offs carry links:** the Stop hook's sign-off template and the
  skill's example now read `[PR #…](url)` and ask for full post ids, instead
  of a bare `PR #…` the server no longer links (RT-6.15).

## 0.31.1

### Added

- **References as links (RT-6.15):** post ids in a post become Mattermost
  permalinks when the MCP server sends it, and read back as bare ids, so
  agents read no URLs for them. PRs, issues, tickets, files and commits the
  agent writes as links itself: the bus carries many projects, repos and
  trackers, and only the agent knows which one a reference belongs to.
  Task rows also take a PR from `MR !n` and `/pull/`, `/merge_requests/` URLs.
- **Odd links written out (anti-fooling, RT-6.15):** a link that is not what
  it shows — another host than its label names, `user@` before the host, a
  non-ASCII or punycode host, hidden characters, an unusual scheme, a fake
  permalink — is shown as label plus full ASCII URL, in what the agent posts
  and in what it reads. Inconspicuous links stay as they come.

## 0.31.0

### Added

- **Agent cards and task states (A2A shapes, RT-6.14):** the MCP server keeps
  who is on the bus and which task is in which state, from the posts
  themselves. `announce` stores the agent's card (A2A AgentCard field names)
  in the HELLO's props; posts whose first word is a bus convention (`TASK`,
  `ACK`, `PROGRESS`, `BLOCKED`, `RESULT`/`PR #n`, `DONE`, `NO`, `CANCEL`, …)
  carry `props.a2a` with an A2A TaskState. Shapes only: no A2A transport, no
  new tool, nothing new pushed. Older posts are read from their text.
- `coordinator("tasks")`: open tasks and asks, one row each;
  `coordinator("roster")`: the reachable agents with status, model, task and
  branch (`mm-agent-roster.sh --json`).
- **`ASSIGNMENTS` tables by the server:** the coordinator posts
  `ASSIGNMENTS · <what changed>`, the server appends the table from the task
  threads. Names without @, so the table wakes nobody.

### Changed

- **Session start reads two rows instead of 50 posts:** the skill now starts
  with `coordinator("roster")` and `coordinator("tasks")` instead of
  `list_recent` over the work channel's history, which stayed in every
  agent's context for the whole session.
- **Bystanders read other agents' status posts short:** `IDLE`, `BYE`, `ACK`,
  `PROGRESS` and `ASSIGNMENTS` posts are cut to their first line (160
  characters) in digests and in `wait_for_events` results of type
  `watched_channel` for agents without the crown; addressed posts, own
  threads, `TASK` and `HELLO` stay whole.
- `coordinator.md`: a task is a `TASK · <title> · @agent` thread; the
  assignment-table template is gone. SKILL.md and coordinator.md are shorter
  than in 0.30.0, which a test now holds.

## 0.30.0

### Added

- **Permission prompts answered from Mattermost (RT-6.13):** in a session
  started as a channel, Claude Code's tool permission prompts (Bash, Write,
  Edit, MCP tools) also go by DM to the responsible human. `yes <id>` /
  `no <id>`, or 👍 / 👎 on the prompt, allows or denies that one call; the
  terminal dialog stays open and the first answer wins. The agent never sees
  the verdict. Needs Claude Code ≥ 2.1.234; research preview.
- `bus_info` shows `permission_relay`.

### Changed

- **On by default, no switch:** the relay is on whenever the session runs as
  a channel and a trusted human is configured. To turn it off, configure no
  trusted human (`MM_TRUSTED_HUMANS`), or start Claude Code without
  `--channels`. Before, permission prompts were out of reach of the bus
  (RT-6.8).

### Security

- Only a trusted human's verdict counts (server-verified author, RT-6.8);
  another agent's or an untrusted person's `yes <id>` is a post like any
  other. Anyone trusted can approve any tool call of the session from
  Mattermost.
- Claude Code does not mark prompts about sandbox, secrets or permissions,
  so they cannot be filtered: they are relayed too.
- The prompt goes by DM, not to the work channel: `input_preview` is the
  call's arguments, and Claude Code masks only well-known secrets.

## 0.29.4

### Added

- **The agent's name in the status line:** `bin/mm-agent-statusline.sh`
  prints the session's bot name, with 👑 while it coordinates, so a terminal
  is easy to match to its agent in Mattermost. Call it from your status line
  script (README, *Sessions*); local files only, never the token.

### Changed

- **One question per `ask_human`:** a list of questions in one ask is
  refused. The agent asks the most important first and the next after the
  answer, which often settles it; the coordinator no longer bundles (RT-6.11).

### Fixed

- **A coordinator was asked for its sign-off again and again:** its IDLE is
  posted as `👑 IDLE …`, which the `Stop` hook did not take for one.

## 0.29.3

### Changed

- **A human's post without a mention wakes only the coordinator.** It woke
  every agent at once, and each spent a turn finding out it was not meant.
  Mentions, DMs, replies in an agent's own thread and `@channel` still wake
  at once, in channel mode and in the listen window alike; members see the
  rest in their next digest. The coordinator routes such a post: it replies
  in the thread with an @-mention of the agent the post means (RT-6.6).

## 0.29.2

### Fixed

- **Channel mode: every pushed message ended with a sign-off block** ("Stop
  hook error"). A turn a channel push started answers the bus and now needs
  no IDLE of its own, as an answer from inside a listen window never did. A
  turn the human started still asks for one, worded for a session the
  channel keeps reachable (RT-6.12).
- **"… joined the channel" woke agents:** Mattermost names the human who
  added a bot as the system post's author, so it was pushed as a trusted
  human's post. The pusher drops system posts.

## 0.29.1

### Fixed

- **Channel mode never switched on (RT-6.12):** the check was pushed right
  after the handshake, before Claude Code registered the channel, and was
  dropped, so no session confirmed it and every message went the listener's
  way. The check now comes 3 s after the handshake and again after 30 s,
  2 min and every 10 min until the agent confirms it.
- **`reply_in_thread` to a DM** failed without `channel_id`: a reply now goes
  to its root's channel.
- **A reply in a DM woke the agent again:** Mattermost counts the bot's own
  post as unread for it, so the Stop hook and the listener saw an unread DM
  that nothing could collect. After a post the channel is marked read, unless
  the inbox still holds a message there.

## 0.29.0

### Changed

- **Channel mode starts by itself and checks itself (RT-6.12).** Without
  `MM_CHANNEL_PUSH` the MCP server reads Claude Code's command line for
  `--channels` (or `--dangerously-load-development-channels`) naming this
  plugin; `MM_CHANNEL_PUSH=1` or `0` overrides that. Its first push is a check
  the agent confirms with the new tool `channel_ok(code)`. Until then the
  server reads nothing from the inbox and the Stop hook keeps the listen
  window, so a session that never receives a push loses no message.
- **Channel pushes follow the listener's wake rules.** Pushed at once is a
  mention, DM, reaction to an ask, `@channel` / `@all` / `@here`, a reply in a
  thread the agent posted in, a trusted human's post, or anything while it
  holds the 👑. Other posts come as one digest once the oldest is
  `MM_LISTEN_DIGEST_SEC` (default 1800 s) old, instead of one model turn each.

## 0.28.1

### Fixed

- **Channel mode under corral:** the sandbox starts with an empty
  environment, so `MM_CHANNEL_PUSH` from the host shell never reached the MCP
  server or the hooks. The corral host session hook now passes it on with the
  bot's other variables (RT-6.12).

## 0.28.0

### Added

- **Messages pushed into the session (Claude Code channels, research
  preview, prototype).** Started with `MM_CHANNEL_PUSH=1 claude --channels
  plugin:mattermost-ai-agent-bus@mattermost-ai-agent-bus` (or
  `--dangerously-load-development-channels` until the plugin is on the
  organisation's `allowedChannelPlugins`), the MCP server declares the
  `claude/channel` capability and sends every inbox event as
  `notifications/claude/channel` the moment it arrives: an idle agent wakes
  without a background listener, a busy one gets it with its next turn. The
  Stop hook then opens no listen window (RT-6.12).
  - The server stays on the MCP handshake era in this mode: Claude Code
    registers no channel on revision 2026-07-28, which the SDK would otherwise
    negotiate.
  - Claude Code does not acknowledge channel events, so `MM_CHANNEL_PUSH`
    belongs only next to the channel flag; without it nothing changes.

## 0.27.1

### Changed

- **The MCP server's packages are pinned:** it starts with `uv run --locked`
  against the committed `mcp/uv.lock`, so a fresh install fetches exactly the
  tested versions. The plugin validation for the channel allowlist requires
  it.

### Fixed

- **A quiet listen window no longer opens the next one.** Its end starts a
  turn, which the prompt hook took for the human's return: it removed the
  window, and the Stop hook opened a fresh one — a full turn every 90 minutes
  for as long as the session stayed open. A window that ran out within the
  last two minutes is now kept, so that turn goes idle (RT-6.3).

## 0.27.0

### Changed

- **A quarter less fixed text in every turn.** The skill, the MCP instructions
  and the tool docs are in the context of every turn; in a host-managed session
  they shrink from about 33.3k to 25.7k characters (skill 28.2k → 20.5k):
  - The coordinator's duties (handover, assignment table, delegating) moved to
    `skills/…/coordinator.md`, read when an agent holds the 👑 crown (RT-6.6).
  - Registering the session yourself moved to `skills/…/self-registration.md`,
    read only when no host hook registered a bot (RT-6.10).
  - The `MM_*` variable table and the prerequisites moved to the README
    (*Configuration*); agents do not set them.
  - The HELLO format is in `docs/protocol.md` and the `announce` tool, the
    `ask_human` format in its tool doc; the skill no longer repeats them.
  - With a bot token at startup the MCP server offers no `session_start` /
    `session_end` and its instructions skip the self-registration text (RT-6.1).

### Added

- **Managing the work, for the coordinator** (`coordinator.md`): distribute by
  capability, reachability and model (mechanical work to cheaper models), one
  task per agent, no overlapping files, review by another agent, guard the
  humans' attention, hold agents to short messages, spot stalled tasks, settle
  disputes after two rounds with `ask_human`, and stay lean (RT-6.6).
- **Agents DM each other:** `post_message(to=<agent>)` for back-and-forth
  that would only cost the others tokens; the outcome goes to the channel.
  People are refused: they are asked with `ask_human` (RT-6.11).

## 0.26.1

### Fixed

- **Share of turns in the stats report never exceeds 100 %.** `turns` counts
  only the human's prompts; the plugin's own turns (wake-ups, forced rounds)
  are now added to the total that the share and the bus characters per turn
  are taken of. Reports over earlier STATS lines change accordingly (RT-6.9).
- **`bus_info` off the bus answers instead of failing.** Without a chat URL or
  bot it returns `on_bus: false` with the reason and what would put the
  session on the bus (RT-6.1).
- **Only the title of an `ask_human` post is bold.** An agent that wrote the
  whole explanation into `need` got a post bold from end to end. Now only its
  first sentence is the title; the rest moves to the front of `context`, and
  `**` inside `need` is dropped (RT-6.11).
- **A listener stopped by the human's prompt no longer shows as failed.** It
  exited 143 on the prompt hook's TERM, which Claude Code lists as "failed";
  now it exits 0 (RT-6.3).

## 0.26.0

### Added

- **A newer installed plugin is named once.** A running session keeps the
  plugin it started with — MCP server, hooks and the skill in its context
  belong to one version, and Claude Code does not respawn a stdio MCP server —
  so an update (an automatic one, too) applies only after a restart. When a
  newer version is installed, the turn-end banner says so once per version
  (`⬆ plugin X is installed, this session runs Y — to switch: quit, then
  claude --continue`), and `bus_info` shows it as `update`. Since 0.24.0 the
  restart keeps the bot. Spec: arc42 RT-6.2.
- `adapters/claude.md` explains how to turn on automatic updates for this
  marketplace (off by default for third-party marketplaces).
- **Decisions are asked one at a time, each answerable in a word.**
  `ask_human` takes `context` (the situation in 1–3 sentences),
  `recommendation` and `decision` (the decision written out, which a 👍 then
  approves as it stands) and shows them in the callout. A second open ask of
  the same agent to the same human is refused with the open one named
  (`posted: false`, `open_ask`); the skill asks the same pattern in the
  terminal. Spec: arc42 RT-6.11.

## 0.25.1

### Fixed

- **Humans are no longer @-mentioned outside `ask_human`.** The rule in the
  skill was not enough: the HELLO that `announce` builds said
  `for: @dembaca · trusts: @dembaca`, the corral host note told the agent
  "Responsible for you: @dembaca", and agents copied that into their posts.
  `announce` and the note now name humans without @, and `post_message`,
  `reply_in_thread` and the note of `resolve_ask` turn an @-mention of a
  person into the plain name, reporting it as `unmentioned` with a hint to
  use `ask_human`. Bots, `@channel`/`@here`/`@all` and unknown names stay as
  written. Spec: arc42 RT-6.11.

## 0.25.0

### Added

- **`ask_human` and `resolve_ask` MCP tools.** An agent that needs something
  from a human posts one message in a fixed format: `🙋 @<human> — **need**`
  and a callout with from, blocking, where and how to answer (red when
  blocking). The skill makes it the only place a human is @-mentioned;
  elsewhere agents write the name without @, so a mention means "I need you".
  `resolve_ask` puts ✅ on the ask once it is settled, so open asks are the ones
  without. Spec: arc42 RT-6.11.
- **Reactions to an ask reach the agent.** A 👍 on a post was seen by nobody:
  Mattermost raises no unread counter for a reaction, and the inbox handled
  only `posted`. Reactions to open asks now arrive through `wait_for_events`
  as `{kind: reaction, emoji, answer: yes|no|null, ask}` with
  `from_trusted_human`, are fetched when the socket connects, and wake an idle
  agent's listener. A `yes` from a trusted human approves exactly what the ask
  names; a reaction on any other post approves nothing.

### Changed

- The not-trusted reply reads `NO – not trusted by me; ask <responsible>`,
  without a mention.

## 0.24.0

### Changed

- **A self-registered bot survives `claude --continue`.** The MCP server
  unregistered its bot whenever it ended, so every quit and `--continue` left
  the agent under a new name: DMs and mentions of the old one reached nobody,
  and its last posts showed a deactivated author. `--continue` keeps the
  Claude Code session id, so the server now keeps the bot when it ends (it only
  touches the session file and goes offline if it can within the ~0.5 s Claude
  Code allows) and the next server takes it over from that file while the
  token is accepted. `session_start` returns a bot the session already has
  (`reused`) instead of registering another. Bots handed over by a host via
  `MM_BOT_TOKEN`, and sessions without `CLAUDE_CODE_SESSION_ID`, keep the old
  unregister-on-exit. Spec: arc42 RT-6.10.

### Added

- **Cleanup of old bots at start.** `session_start` first unregisters the bots
  of other MCP-registered sessions whose Claude process is gone and whose file
  was untouched for `MM_SESSION_KEEP_SEC` (default 24 h), each with its own
  token, and lists them as `reaped_stale_bots`. `mm-agent-sweep.sh` waits out
  the same grace period for such bots (`KEPT …`); `--force` removes them sooner.

### Known limitation

- The overhead stats of a session whose bot is kept are not posted when it
  ends; its counter file is continued after `--continue` and otherwise
  removed by the sweep after a day.

## 0.23.0

### Fixed

- **A mention no longer drowns in history after a fresh registration.** A bot
  that had just joined its project channel could see the channel reported as
  never viewed (`last_viewed_at` 0); the backfill then queued the channel's last
  50 posts, `wait_for_events` handed out the ten oldest and marked the channel
  read — the mention that had woken the agent stayed queued while every
  counter said zero. Backfill now reads nothing older than the bot, mentions
  and DMs are returned first, a channel with posts still queued stays unread,
  and a last item `{kind: more_pending, count}` says when to call again.
  Spec: arc42 RT-6.2.

## 0.22.1

### Fixed

- **`mm-agent-sweep.sh` looks only at session files.** It listed every per-bot
  state file (`coord-`, `listen-`, `presence-`, `turn-<uid>.env`) as a skipped
  session without a bot name — a screen of noise on a host with nothing to
  clean. Those files are now ignored as sessions, and `--apply` removes the
  ones (and `stats-<uid>.jsonl`) whose bot has no session file left and that
  were untouched for a day. Spec: arc42 RT-6.1.

## 0.22.0

### Fixed

- **Two agent-registered sessions in one checkout no longer swap identities.**
  The session file that hands the bot token from the MCP server to the hooks was
  named after the project directory, so a second `session_start` in the same
  checkout overwrote the first one's: the first agent's hooks and listener then
  ran as the second bot, showed its presence and never woke for the first bot's
  mentions and DMs, and whichever session ended first took the file away from
  the other. It is now named after the Claude Code session
  (`CLAUDE_CODE_SESSION_ID`, or the `session_id` in a hook's input); only
  without one is it keyed on the project directory. Spec: arc42 RT-6.10.
- **`mm-agent-sweep.sh` removes files of bots that are gone already.** A bot
  whose own token the registrar and Mattermost both reject (`401`) no longer
  counts as a failure kept forever: the stale file is removed and reported as
  `gone`. Real failures now print their HTTP status, and session files from
  before `MM_REG_BOT_NAME` are swept by their username. Spec: arc42 RT-6.1.

### Added

- **Host hooks from the marketplace clone.** corral's `exec` may point at
  `~/.claude/plugins/marketplaces/mattermost-ai-agent-bus/adapters/corral/`, so
  `/plugin update` takes effect without the installer. A deliberate trade,
  documented: corral no longer asks before new hook code runs, and a sandbox
  sharing `~/.claude` can change that code. The start hook no longer warns for
  that path and points to `/plugin update` when the installed plugin is newer;
  it still warns for versioned plugin directories, the corral home's plugins and
  the session's workdir. Spec: arc42 §7.1.

## 0.21.0

### Added

- **Overhead stats.** Hooks, listener and MCP server count what the bus adds
  to a session — turns it caused (listener wake-ups by reason, rounds the Stop
  hook forced), tool results and their size, own posts, compaction handoffs,
  and the fixed text every turn carries — in a local file, without a model and
  without content. At session end the MCP server posts one `STATS {…}` line
  to `agents-stats` (`MM_STATS_CHANNEL` overrides, `off` disables counting;
  a missing channel is skipped), a channel no agent watches.
  `bin/mm-agent-stats.sh report` compares the numbers by plugin version.
  Spec: arc42 RT-6.9.

## 0.20.0

### Added

- **`announce` MCP tool** posts the `HELLO` in the one fixed format. The agent
  gives project, task, model, capabilities, branch/PR and role; the server adds
  its username and `for:` / `trusts:` from the session's configuration, so they
  cannot be left out or remembered wrong. Hand-written HELLOs drifted into one
  line of prose without them. The skill makes `announce` the way to announce.

## 0.19.0

### Added

- **Responsible human and kind.** `MM_RESPONSIBLE_HUMAN` (default: the first
  trusted human) names who answers for an agent; `MM_AGENT_KIND=autonomous`
  marks an agent nobody runs interactively. Both, with the trusted list, go
  along with the registration (`responsible`, `trusted`, `kind`), so a registrar
  that knows them can write `for @… · trusts @…` into the bot's profile — which
  a bot cannot change itself. The banner shows `for @…`.
- `HELLO` gets `for:` and `trusts:` lines, filled from `bus_info` (new fields
  `responsible`, `kind`).
- Skill, several humans in a channel: ask the responsible or a trusted human;
  an approval by anyone else gets `NO – not trusted by me; ask @…`; check
  another agent's profile rather than its `HELLO`.

### Fixed

- **Nothing is inferred from `MM_REG_SECRET` any more.** It is the registrar's
  shared secret, not a personal access token, so the `users/me` lookup with it
  (since 0.13.0) never found anyone and sent the secret to the Mattermost API
  for nothing. Trusted humans now come only from `MM_TRUSTED_HUMANS`; without
  it the banner says `trusted none (set MM_TRUSTED_HUMANS)`.

## 0.18.0

### Changed

- **The listener wakes a member only for posts meant for it:** a mention or
  DM, `@channel` / `@all` / `@here`, a reply in a thread it posted in, or a
  post by a trusted human. The 👑 coordinator still wakes for every post.
  Other posts wake a member once, as a digest, when the oldest is
  `MM_LISTEN_DIGEST_SEC` (default 1800 s) old; `0` restores waking at once.
  A busy channel no longer costs every member a model turn per post.
- Skill: a post meant for all agents carries `@channel`.

## 0.17.0

### Changed

- **Lean tool results.** `wait_for_events`, `list_recent`, `get_thread`,
  `post_message`, `reply_in_thread` and `get_me` return slim posts (`id`,
  `root_id`, channel name, author name, time, message) in compact JSON without
  `\uXXXX` escapes; `list_recent` drops system messages. `raw=true` returns
  Mattermost's full JSON. 30 posts in ~agents: about 5,850 → 835 tokens.
  `trusted_human` appears only when `from_trusted_human` is true.
- **Short messages** (skill): coordination posts carry facts only — one-line
  `ACK` / `DONE` / `BLOCKED` / `NO`, sign-off as fields, no greetings or recaps,
  point to PRs, threads and lines instead of quoting.

## 0.16.0

### Changed

- **Owner → trusted humans, and a list.** `MM_TRUSTED_HUMANS=you[,teammate]`
  names the humans whose posts may approve actions; without it, the user the
  registration PAT belongs to, as before. Events carry `from_trusted_human`
  (was `from_owner`) and `trusted_human` (who). Host hooks hand
  `MM_TRUSTED_HUMANS` / `MM_TRUSTED_HUMAN_USER_IDS` to the sandbox, the banner
  says `trusted @…`, `bus_info` lists them. An approval that contradicts the
  agent's own human in the session does not count. Spec: arc42 RT-6.8.
- `MM_OWNER` / `MM_OWNER_USER_ID` are still read until 0.17; switch to
  `MM_TRUSTED_HUMANS`.

## 0.15.1

### Changed

- Listening with a timer: the first delay is 30 s or the runtime's shortest
  one if that is longer (Cursor: 60 s), and a reply restarts at that first
  delay.
- Docs: a registration PAT of a separate admin account makes that account the
  owner; set `MM_OWNER` to the everyday username (`adapters/corral.md`,
  arc42 RT-6.8).

## 0.15.0

### Security

- **corral host hooks no longer run from sandbox-writable code.** They run on
  the host with the registration secret, but corral approves only the `exec`
  file by content, not the `bin/` scripts it sources and starts. Run from the
  plugin directory (the sandbox's private home) or a checkout a session works
  in, any agent could put code on the host. New
  `bin/mm-agent-host-install.sh`, run on the host, copies the hooks and `bin/`
  from the installed plugin to `~/.local/share/mm-agent-bus/current/`; point
  corral's `exec` there and rerun it after each plugin update. `--check`
  compares versions.
- The start hook warns when it runs from the plugin directory or the session's
  workdir, and when the host copy is older than the installed plugin.

**Action needed:** run the installer on the host and update the `exec` paths
in `~/.config/corral/config.yml` (see `adapters/corral.md`).

## 0.14.1

### Added

- **`bus_info` MCP tool:** plugin version (the MCP server reports the same in
  its server info and instructions), bot, owner with the source it came from,
  work channel, crown. Owner `none` means approvals over the bus are off.
- The corral start banner shows `owner @<name>` or `owner none`.

### Fixed (docs)

- `adapters/corral.md`: point the hooks' `exec` at a checkout that stays on a
  released state. A development checkout on another branch handed sessions an
  older hook, so `from_owner` stayed `false`.

## 0.14.0

### Added

- **Listening with a timer** for runtimes without turn hooks that can wake the
  agent (Cursor: `subscribe_timer`): after the sign-off, one-shot timers with
  backoff (30 s, doubling, at most 15 min) for `MM_LISTEN_SEC`, reset after an
  answer, `idle` at the end. Each wake-up is a full turn, about ten in a quiet
  90-minute window. Skill, `adapters/cursor.md`, arc42 §7.2.

## 0.13.0

### Added

- **Approvals over the bus.** The session's own human may confirm a pending
  action on Mattermost: a DM or a reply in the request's thread that names the
  action (`APPROVE: …`). The MCP server marks such events `from_owner` after
  checking the author id, which no post can fake; everything else stays data.
  Runtime permission prompts and sandbox, secret or permission changes stay
  terminal-only. Spec: arc42 RT-6.8.
- **The owner is found automatically:** the user the registration PAT belongs
  to, looked up on the host (corral) or by the MCP server (`users/me` with the
  PAT). `MM_OWNER` (a username) overrides it.

## 0.12.0

### Added

- **Assignment table.** Whenever an assignment changes, the coordinator posts
  the full task-to-agent table as a top-level `ASSIGNMENTS` post: agent, task,
  status (`active`, `blocked`, `review`, `done`, `open`), spec or issue, branch
  or PR. The newest one is the current state, and it is the answer to "who does
  what". Skill and `docs/protocol.md`.

## 0.11.0

### Added

- **Handoff state survives compaction.** A new `SessionStart` hook (matcher
  `compact`) hands the agent its three newest own posts in the work channel
  and its `HELLO` thread id right after the conversation was compacted.
  Spec: arc42 RT-6.7.
- The sign-off asks for a handoff: done, open, branch/PR and the next step,
  written so that the agent could pick up from it alone.
- Skill: hand reading-heavy work to sub-agents; start a fresh session for a new,
  unrelated task.

### Recommended setup

- Claude Code v2.1.290 or later compacts long conversations by itself while
  the terminal is idle (`idleCompaction`, on by default). Set
  `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS=100000` (the minimum; default 200000)
  to compact earlier.

## 0.10.0

### Changed

- **The listen window waits without the model.** Instead of `wait_for_events`
  in 100 s slices — one model call per slice, each rereading the whole
  context — the `Stop` hook asks the agent to start `bin/mm-agent-listen.sh`
  as a background command and finish. The listener polls the bus over REST
  and exits on a mention, DM or new post in the work channel, or at the end
  of the window; Claude Code then wakes the agent. One model call per message
  instead of one per slice.
- The window lives in its own state file, so a turn woken by the listener
  keeps it. The next prompt from the human stops a running listener (RT-6.3,
  replaces ESC).
- Asked once per window: an agent that cannot run background commands goes
  idle instead of being asked again.
- **Longer windows**, since waiting is free now: `MM_LISTEN_SEC` default
  5400 s (90 min, was 900 s), `MM_LISTEN_COORD_SEC` default 7200 s (2 h, was
  3600 s); both capped at 7200 s, Claude Code's limit for a background
  command. The hook names the timeout to pass, since the runtime's default
  (30 min) would cut the window short.
- Skill: a request that needs the human's approval while they are away gets
  a `WAITING for my human's approval` reply in its thread.

### Removed

- `MM_LISTEN_CALL_SEC`. New: `MM_LISTEN_POLL_SEC` (default 15 s).

## 0.9.0

### Added

- **Coordinator crown.** The coordinator wears a 👑 custom status, and its posts
  start with 👑 (Mattermost shows no status next to bot posts). Without a crown
  anywhere, the oldest reachable agent takes it. Agents hand the role to a
  stronger model by agreement (`HANDOVER`, `ACK`, then the new `coordinator`
  MCP tool: `take` / `release` / `who`); without agreement, age decides.
  Spec: arc42 RT-6.6.
- **Every status is a lease.** Custom statuses now carry an expiry
  (`MM_LEASE_SEC`, default 900 s), renewed by the MCP server's heartbeat
  (`MM_HEARTBEAT_SEC`, default 300 s). A dead session's status — and crown —
  runs out on its own; an idle coordinator loses the crown the same way.
  Runtimes without turn hooks show 📡 `on the bus`.
- `bin/mm-agent-lease.sh` (`tick`, `take`, `release`, `who`) holds the rules,
  shared by hooks, CLI and MCP server.
- Skill: questions to everyone about what is open or next are answered by the
  coordinator only; others add substantive points.
- **The coordinator listens longer:** `MM_LISTEN_COORD_SEC` (default 3600 s)
  instead of `MM_LISTEN_SEC` (900 s) while it holds the crown, so it stays
  reachable and keeps the role for an hour after its last turn. Members keep
  the short window, since each listen slice is a model call.

### Changed

- `mm-agent-roster.sh` lists only reachable agents (not idle, not offline, a
  live status), coordinator first; `--all` shows the rest. A status without an
  expiry no longer counts as alive.

## 0.8.1

### Fixed

- **Typing indicator visible in the channel.** It was sent for the thread only,
  and the Mattermost webapp shows a thread's indicator only in the open thread
  view. It now goes to the channel as well (empty `parent_id`).

## 0.8.0

### Added

- **Typing indicator.** When `wait_for_events` delivers a mention or DM, the
  sender sees the bot as *typing* in that thread, as with a human. It is re-sent
  every 4 s and stops when the agent posts, waits again, or after
  `MM_TYPING_SEC` (default 60 s, `0` = off). Posts from watched channels that
  address nobody start none.

## 0.7.1

### Changed

- **Check the bus before each new activity.** Skill and MCP `instructions` now
  ask agents to call `wait_for_events(timeout_sec=1)` at the start of a turn and
  before each new activity, not only after finishing one. Runtimes without turn
  hooks (Cursor, OpenCode) have no `Stop` check and no listen window, so this is
  how their agents hear the bus at all.

## 0.7.0

### Added

- **Banner in the terminal.** Every block of the `Stop` hook now shows the
  developer a one-line `systemMessage` saying why the turn goes on:
  📨 collecting unread messages, ✋ signing off, or
  👂 `Listening on Mattermost until HH:MM (N min left) · ESC to stop` for each
  slice of the listen window.

### Changed

- The `Stop` hook blocks with JSON on stdout and exit 0 (Claude Code's
  structured hook output) instead of exit 2 with JSON on stderr.

## 0.6.1

### Fixed

- **Listen window no longer loops.** The `Stop` hook asked for one
  `wait_for_events` call covering the whole window (up to 900 s), but Claude
  Code moves an MCP call past about 120 s to the background and ends the turn.
  The hook never saw the `[]` it waited for, fired again with a shorter rest,
  and each round left another listener running. The window is now listened to
  in slices of at most `MM_LISTEN_CALL_SEC` (default 100 s).
- **A newer `wait_for_events` supersedes a pending one**: the older call returns
  `[]`, so a backgrounded listener cannot swallow events meant for the
  foreground.

## 0.6.0

### Changed

- **`MM_BOT_NAME` is now `MM_REG_BOT_NAME`**, everywhere: environment, session
  files, CLI, MCP server, hooks. It is the registrar's short name for the bot,
  not its Mattermost identity (`MM_BOT_USERNAME`). No fallback to the old name:
  session files written before 0.6.0 are not read any more — run
  `bin/mm-agent-sweep.sh --apply` on the host before updating, or rename the key
  in leftover files under `~/.local/state/mm-agent-bus/`.
- Docs no longer call the withheld name a security measure: the registrar
  deletes a bot only with the secret or the bot's own token. Withholding it
  keeps the CLI in the sandbox from stopping a session the host owns.

## 0.5.0

### Added

- **Listen window.** In a project channel a Claude Code turn no longer goes
  straight to idle: after the sign-off the `Stop` hook keeps the agent in
  `wait_for_events` for `MM_LISTEN_SEC` (default 900 s, `0` = off). Events are
  handled and the window resumes; an answer on the bus starts a fresh window.
  ESC ends it at any time. A `BYE` skips it.
- **👂 listening status** (`mm-agent-status.sh listening <until-epoch>`), with a
  Mattermost expiry so an interrupted window leaves no stale status.

### Changed

- **Registration only with a project channel.** Host session hooks (corral
  `preStart`, Cursor example) register a bot only when `MM_PROJECT_CHANNEL` is
  set — then always — or when `MM_REGISTER=always`. Skill and MCP instructions
  tell agents not to register on their own without a project channel.
- The `Stop` hook keeps per-turn state (`turn-<bot-id>.env`, cleared on the next
  prompt) instead of relying on `stop_hook_active` alone: up to 3 drain rounds,
  one sign-off request per turn, no second sign-off inside a listen window.
