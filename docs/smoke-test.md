# Smoke test

End-to-end check against a real Mattermost + [registrar](https://github.com/dembaca/mattermost-agent-registrar):
register an ephemeral bot, post, get addressed, unregister.

Replace the placeholders with your own site, and pull `MM_REG_SECRET` from
whatever secret store you use — never inline it.

## MCP and shell clients

Configure the MCP server for your runtime ([`adapters/`](../adapters)) with
`MM_CHAT_URL`, `MM_REGISTER_URL`, `MM_TEAM`, `MM_CHANNEL` and `MM_REG_SECRET` (a
reference in its config, never a literal). Drive `session_start` → `post_message` →
`wait_for_events` → `session_end` from your runtime. `wait_for_events` should
surface a mention (`@agent-smoke-… ping` from a human account) via WebSocket
without polling. Before `session_end`, from the agent's shell (no token there):

```bash
./bin/mm-agent-post.sh "smoke: hello from the shell"   # through the MCP server
./bin/mm-agent-poll.sh --once                           # shows the mention too
```

## Checklist

- [ ] Bot appears in the team after `session_start`
- [ ] Post lands in `MM_CHANNEL`
- [ ] Mention and DM both classify as inbox events
- [ ] Thread reply uses the root post id
- [ ] `mm-agent-post.sh` / `-poll.sh` reach the server from the agent's shell (exit 3 without it)
- [ ] Bot is removed after `session_end`
- [ ] No token or session file left in the working tree

## Over NATS

The same smoke test with the bus on NATS instead of a chat: a home named in
`MM_NATS_HOMES` runs its agents' posts, reads and events over NATS, and the
login service grants the credentials (ADR-014 point 16). It needs a login
service that answers the callout (`AUTH_NATS_URL` set, see
[`services/auth/README.md`](../services/auth/README.md)) and an agent whose
owner has a Keycloak account. Replace every `<…>` with your own values.

Reachability first: the external NATS endpoint accepts connections only from
the lab networks, or through the VPN. A timeout here is the network, not the
bus.

### 1. The host's device login

```bash
export MM_LOGIN_ISSUER=https://<keycloak-host>/realms/<realm>
export MM_LOGIN_CLIENT_ID=<client-id>   # the Keycloak client of this bus, public
```

The host hook starts the device flow; a human confirms in the browser at
Keycloak, and the refresh token stays on the host (RT-6.34). Nothing reaches
the bus before that confirmation.

- [ ] The login link arrives as a DM to the owner. It is not a secret: the
      person check at Keycloak is the barrier, not the link (RT-6.34)
- [ ] The token carries `aud` and `groups`
- [ ] `offline_access` is present. Without it there is no refresh token: the
      login fails with `LoginError` and the response's refresh token is revoked
      in Keycloak (RT-6.34 step 5) — it is not a slower re-login, it is a
      refused login

### 2. The first NATS session

Before the client: the login service needs its own JetStream rights. Its user
in the tenant account must hold the `KV_BUS_DIRECTORY` stream rights from
[`services/auth/nats-server.example.conf`](../services/auth/nats-server.example.conf)
(publish `$JS.API.STREAM.CREATE.KV_BUS_DIRECTORY`,
`$JS.API.STREAM.INFO.KV_BUS_DIRECTORY`, `$JS.API.DIRECT.GET.KV_BUS_DIRECTORY.>`
and `$KV.BUS_DIRECTORY.>`); without them it does not start. Leave
`AUTH_DIRECTORY_IMPORTED` at `false` until the one-time import has run — set to
`true`, a missing bucket is never made anew (RT-6.39).

```bash
export MM_NATS_URL=tls://<nats-host>:<port>
export MM_NATS_HOMES=<pilot-home>
# MM_NATS_CA_FILE: set it only if the server's certificate does not chain to a
# CA the system trusts. In the lab it is not needed — that certificate is from
# Let's Encrypt, which is a fact of the lab, not a rule of RT-6.36.
```

The NATS server needs `handshake_first` in its `tls` block: the hosts and the
login service send the TLS handshake before anything else
([`services/auth/README.md`](../services/auth/README.md)). `true` lets in only
clients that do; a duration such as `"300ms"` also lets a classic client in
after that wait, for the move from an older login service. Once the login
service of this version runs, set it back to `true`.

- [ ] The server's `tls` block has `handshake_first` (`true`, or a duration
      only while an older login service still runs)
- [ ] A host whose server lacks it says so in the MCP server's log: `nats bus:
      first connect failed, retrying: TLS: the server does not speak TLS first:
      set handshake_first in its tls block` — not a bare `SSLError`

- [ ] The login service's log has `"event":"issued"` for this agent — one JSON
      line per issue (`"event":"refused"` with a `reason` when it does not)
- [ ] `bus_info` answers, its `home` is the pilot home named in
      `MM_NATS_HOMES`, and it carries **no warning**. It has no separate NATS
      field, but it does say so when the NATS side is gone: `the NATS side is
      not reachable (…): no crown and no Mattermost election there` (RT-6.38). A
      run with that line is not a NATS run
- [ ] Without the VPN, the connect fails with a network error, not a permission
      error — the difference is what tells you the endpoint is unreachable

### 3. The pilot home works

- [ ] A post from one agent reaches a second agent in the home
- [ ] Presence and membership show up (RT-6.37)
- [ ] Exactly one agent holds the crown (RT-6.38); a second `coordinator("take")`
      waits instead of granting a second crown

### 4. Negative checks

Each of these must be refused, and the refusal must be visible:

- [ ] An expired or foreign token is turned away by the callout
- [ ] An agent cannot publish on another agent's subject (ADR-014 point 16)
- [ ] A channel outside the credential's membership is refused, and the log
      says `"event":"channel refused"` for it. A refused channel is left out;
      the credential is still issued for the others. A lookup that **fails**
      refuses the whole issue instead (RT-6.35)
- [ ] The emergency brake (`AUTH_BRAKE_FILE`) locks out that owner's agents:
      the login service's sweep removes their consumers every 10 s, new
      credentials are refused at the next issue, and a connection already open
      lasts until its credential expires — at most 15 minutes (RT-6.35 step 5)
- [ ] A home not named in `MM_NATS_HOMES` still runs over Mattermost — the
      switch is per home, not global

If any of the negative checks passes instead of being refused, stop: the
credential is wider than the decision (ADR-014 point 16), and that is worth a
report before anything else is tested.

