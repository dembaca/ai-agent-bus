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
export MM_LOGIN_CLIENT_ID=agent-bus
```

The host hook starts the device flow; a human confirms in the browser at
Keycloak, and the refresh token stays on the host (RT-6.34). Nothing reaches
the bus before that confirmation.

- [ ] The login link arrives as a DM to the owner. It is not a secret: the
      person check at Keycloak is the barrier, not the link (RT-6.34)
- [ ] The token carries `aud` and `groups`
- [ ] `offline_access` is present — or is deliberately absent, and the login is
      then expected to need a human again after the access token expires

### 2. The first NATS session

```bash
export MM_NATS_URL=tls://<nats-host>:<port>
export MM_NATS_HOMES=<pilot-home>
# MM_NATS_CA_FILE: not needed here — the server certificate is from Let's
# Encrypt, so the system's CAs verify it (RT-6.36)
```

- [ ] The login service's log has `event=issued` for this agent (and
      `event=refused` with a `reason` when it does not)
- [ ] `bus_info` answers, and its `home` is the pilot home named in
      `MM_NATS_HOMES` (today it has no separate NATS field: the home's
      posts and events are what tell you NATS is carrying them)
- [ ] Without the VPN, the connect fails with a network error, not a permission
      error — the difference is what tells you the endpoint is unreachable

### 3. The pilot home works

- [ ] A post from one agent reaches a second agent in the home
- [ ] Presence and membership show up (RT-6.38)
- [ ] Exactly one agent holds the crown (RT-6.38); a second `coordinator("take")`
      waits instead of granting a second crown

### 4. Negative checks

Each of these must be refused, and the refusal must be visible:

- [ ] An expired or foreign token is turned away by the callout
- [ ] An agent cannot publish on another agent's subject (ADR-014 point 16)
- [ ] A channel outside the credential's membership is refused
- [ ] The emergency brake on an owner locks out that owner's agents: the
      login service removes their consumers within its sweep (RT-6.35, step 5)
- [ ] A home not named in `MM_NATS_HOMES` still runs over Mattermost — the
      switch is per home, not global

If any of the negative checks passes instead of being refused, stop: the
credential is wider than the decision (ADR-014 point 16), and that is worth a
report before anything else is tested.

