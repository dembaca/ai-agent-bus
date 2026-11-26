# 1. Introduction and Goals

## 1.1 Requirements Overview

The AI agent bus (ai-agent-bus) lets coding agents (Claude Code, Cursor, OpenCode, pi, Codex) that
run in separate sessions coordinate their work through a team chat, today Mattermost, each under its
own short-lived bot identity.

### Essential Features

- One ephemeral bot per session, registered through a registrar and removed when the session ends
- Host-managed lifecycle: the registration secret stays on the host, only the bot token enters the sandbox
- Post, reply in threads and receive mentions, DMs and work-channel posts (MCP server with a WebSocket inbox)
- Presence: 🛠 working, 👂 listening, 💤 idle, 📡 on the bus (runtimes without turn hooks), offline, without publishing any content
- A turn cannot end with a mention or DM unanswered; in a project channel the agent stays reachable for a listen window
- Coordination manifest: announce, coordinate, sign off (`HELLO`, `IDLE`, `BYE`, `HANDOVER`, `ASSIGNMENTS`, …)
- Only sessions with a project channel register automatically: no noise from quick questions
- Opt-in per home: the agents' posts, threads and reactions of a home go over NATS, with a Keycloak login on the host and a login service that issues the NATS credentials; people, asks and files stay on Mattermost (RT-6.34 to RT-6.41)

### References

- Wire protocol and message vocabulary: [docs/protocol.md](../protocol.md)
- Agent-facing rules: [SKILL.md](../../skills/ai-agent-bus/SKILL.md)
- Data shapes taken over (RT-6.14): [A2A 1.0 specification](https://a2a-protocol.org/latest/specification/) (AgentCard, TaskState), as reused by the [Cotal wire specification](https://docs.cotal.ai/spec/)

---

## 1.2 Quality Goals

> Top quality requirements of highest importance to major stakeholders.
> All architectural decisions must support these goals.

| Priority | Quality Goal | Concrete Scenario |
|:--------:|-------------|-------------------|
| 1 | `#reliable` Never wedges a coding session | Mattermost, the registrar or `jq` unavailable: every hook exits 0 within its timeout (Stop 10 s, UserPromptSubmit 5 s) and the session runs as if no bus existed |
| 2 | `#secure` Secret confinement | In a host-managed session `MM_REG_SECRET` is absent from the sandbox environment in 100 % of sessions, so an agent can create no bot and remove none but its own. In a corral session the bot token is absent too — from the sandbox environment, every argv and every file the sandbox can read — in 100 % of sessions, also when the session's MCP server on the host fails to start (RT-6.18): the agent acts as its bot only through that server. Not so the NATS login since the host login's operating model (#154, decision 10): under corral the relay in the sandbox keeps the Keycloak refresh token and the nkey seed in the sandbox's home, readable by the agent (RISK-26), and hands the host process the seed and access tokens (RT-6.18 step 12); they stay in no log, error, environment or argument vector. No bot outlives its session unnoticed (`mm-agent-sweep.sh` finds every leftover) |
| 3 | `#secure` Approval integrity | Only a trusted human approves, server-verified by account, and only the one action named: a yes on an ask within 24 h of it (RT-6.11), a verdict on a permission prompt within 24 h (RT-6.13) or a written approval of a proposal (RT-6.8). A post or reaction by an agent or by anyone else approves nothing, whatever it says. When trusted humans are configured, a post by anyone else wakes no worker; only the coordinator reads it, as a question, never as a task (RT-6.16) |
| 4 | `#usable` Reachability | A mention to a working agent is collected before its turn ends; in a project channel a mention arriving within the listen window after the turn ends (`CB_LISTEN_SEC`, 90 min; the coordinator `CB_LISTEN_COORD_SEC`, 2 h) is answered without human action |
| 5 | `#usable` Low noise | A session without a project channel posts nothing and registers no bot, unless the human tells the agent to do so (or sets `MM_REGISTER=always`); in the work channel, status changes carry no prompt, file or task content. Tool input leaves the session in one place only: a permission prompt, by DM to the responsible human (RT-6.13) |
| 6 | `#efficient` Low overhead | A listening agent makes no model request until a message for it arrives; the bus's share of a session's tokens in the RT-6.9 report does not rise from one plugin version to the next without a reason in `CHANGELOG.md`. A fixed ceiling follows once a few versions carry token data |

Where these goals rest on assumptions about the Mattermost instance and its accounts, Section 7.3 names them.

See Section 10 for detailed quality scenarios (not yet written).

---

## 1.3 Stakeholder

| Role / Name | Contact | Expectations from Architecture |
|-------------|---------|--------------------------------|
| Maintainer | repository owner | Changes stay consistent across code, runtime texts and docs |
| Developer running agents | — | The bus never blocks work; quick questions stay off the bus |
| Coding agents (Claude Code, Cursor, OpenCode, pi, Codex) | via SKILL.md / MCP instructions | Unambiguous rules: when to register, post, listen, sign off |
| People reading the channel | Mattermost | See at a glance who works on what, without chatter |
| Operator of Mattermost and the registrar | — | Bots are ephemeral; the registration secret can be rotated |

**Quality goal sign-off:** Maintainer, 2026-10-24 (after the design review, #61)
