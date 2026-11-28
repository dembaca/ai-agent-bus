# testrig — fault runs for the agent bus

The harness that measures the bus under injected faults, so that the acceptance
of phase 4 can compare against **today's** bus rather than memory.

**What it measures:** the bus itself. `bus-run` starts two real
`mattermost_ai_agent_bus` MCP servers — one posts with `post_message`, one
receives with `wait_for_events` — in front of the Mattermost stand-in
(`stub.MattermostStub`, on `gateway.Gateway`) and, when a fault is asked for,
the proxy (`proxy.GatewayProxy`). Every number it prints is about that path:
frames the sender handed to the server, frames the receiver's tool returned,
the time between, and what the stub and the relay saw. It is the same
bookkeeping (`runner.Run`) and the same formulas the echo run uses, so the two
runs' numbers compare directly.

**Over NATS too** (ADR-014 phase 1): `bus-run --transport nats` runs the same two
servers with their home over NATS, against a real nats-server, and `coord-run`
hard-stops the coordinator and measures the take-over. See
[The NATS transport](#the-nats-transport) below.

**What it does not measure:** the 100 human posts under fault of the coordinator
row, and whether an event was handled twice or not at all across a take-over:
they stay `not_yet_measurable` in `measurands.toml`, each with the reason. A
`bus-run` posts bot to bot in one home channel; it does not read with a human's
account and does not run long enough for a 14-day retention check.

`echo-run` remains: it starts a loopback echo endpoint and measures the
harness's own round trip, which proves the harness works where the bus does not.
`bus-run` needs `mcp/` synced (`uv sync --directory mcp`); without it the
command says so instead of pretending.

Not part of the bus: nothing here is imported by `mcp/`, and the bus does not
know this directory exists.

## Where the faults are injected, and why there

Two kinds of fault, deliberately in two different places:

| Fault | Where | Why there |
|---|---|---|
| Server killed (`SIGKILL`) | the operating system, on a process the harness started (`proc.kill_child`) | a fake kill leaves the victim running; the point is that the process is **gone** |
| Network cut | the proxy aborts the transport with no close frame (`--fault cut`) | a socket the harness merely stops reading still accepts writes, and a clean close is not a cut; the peer must see the connection drop |
| Frame dropped, duplicated, reordered, stalled, blackholed, half-open, cut | a WebSocket proxy in front of the endpoint (`proxy.FrameProxy`, `proxy.GatewayProxy`) | a real Mattermost cannot be made to duplicate or reorder a frame on demand. The proxy relays **the server's own bytes** and only decides whether, when and how often the client sees them: the message stays real, its delivery does not |

`stall` delays **application** frames. Control frames (ping/pong) are handled
inside each `websockets` connection and are not delayed, so a heartbeat does not
see a stall — use `cut` for "the peer notices the connection is gone".

`blackhole` silences the server-to-client direction only and keeps the
connection open; pings are still answered, so a client does not notice on its
own.

`halfopen` silences **both** directions and keeps the connection open: the
proxy calls `pause_reading()` on the socket in each direction, so it no longer
reads — and no longer answers pings — while a write still succeeds. A frozen
line is not a close: nothing gets an error, the client only notices when its
own heartbeat gives up. Accepted by a test that dials through the proxy with
`ping_interval=0.5, ping_timeout=0.5` and sees the connection fail.

The proxy is why "duplicated" and "reordered" are measurable at all. The bus
derives REST and WebSocket from one base URL, so it is pointed at
`proxy.GatewayProxy`: every HTTP request is forwarded unchanged to the stub, and
only `/api/v4/websocket` is proxied frame by frame. `FrameProxy` remains for a
bare WebSocket endpoint (the echo run uses it). Both proxies share one
implementation of the faults (`proxy.FaultRelay`).

Invariant: a frame goes out **at most once** unless the fault is `duplicate`.
Drop, reorder and stall change whether or when, never how often, which is what
keeps loss and duplication separate numbers.

## Measurands

Named in one file, [`measurands.toml`](measurands.toml), and nowhere else. Each
one cites the acceptance row of ADR-014 it belongs to, so the harness never
invents a number: it measures what the acceptance will compare.

Some measurands are `not_yet_measurable` — device approvals and forgery
refusals exist only once the login service and the NATS permission model do.
They are still reported, with `null` and the reason, so the report has the same
shape before and after the switch. Matching a different measurement plan is a
rename in that file, not a rebuild.

The run plan's own values are in that file too, under `[plan]`: 100 messages per
run, at least 10 runs per fault, 50 hard stops, 100 human posts, 30 s as the
take-over target, from ADR-014 phases 1 and 4
(`docs/arc42/09-architecture-decisions.md`). Where the plan
defines nothing — the tolerance, where a wait starts and ends — the value is
`"open"`, not a number: an invented one would appear in a report as if the plan
had asked for it.

## Running it

```
uv run --directory testrig bus-testrig measurands
uv run --directory testrig bus-testrig echo-run
uv run --directory testrig bus-testrig echo-run --fault duplicate
uv run --directory testrig bus-testrig echo-run --fault reorder --after 1
uv run --directory testrig bus-testrig echo-run --fault stall --stall-sec 0.2
uv run --directory testrig bus-testrig echo-run --fault halfopen

uv run --directory testrig bus-testrig bus-run
uv run --directory testrig bus-testrig bus-run --fault drop --after 1 --every 3 --expect-loss
uv run --directory testrig bus-testrig bus-run --fault duplicate --after 1
uv run --directory testrig bus-testrig bus-run --fault stall --stall-sec 0.2
uv run --directory testrig bus-testrig bus-run --count 20 --send-interval 0.01
```

`echo-run` needs no Lab, no test channel and no test bots: it starts a loopback
echo endpoint, puts the proxy in front of it and measures the harness's own
round trip. `bus-run` needs `uv sync --directory mcp`: it runs the real server
as a subprocess, found by asking each candidate interpreter whether it imports
the package (this project's own venv does not, by design).

Two numbers a `bus-run` reports need a word of their own:

- `--fault duplicate` shows **0 duplicates at the tool boundary** while the
  relay forwarded twice as many frames as it received. The bus's inbox hands a
  post over once by id (`websocket_inbox._seen_ids`), so the wire duplication is
  absorbed before the agent sees it. Both numbers are in the report — the wire's
  in `relay`, the bus's in `measured` — and the run says so in `notes`.
- `--fault drop` with the default `--after 0` eats the socket's handshake
  frame, so the inbox never opens and every frame is lost. That is a real
  measurement (loss equals frames sent), not a crash: `connection_closed` says
  the inbox never opened, and `stderr_tail` carries the server's own message.
  `--after 1` lets the handshake through and measures loss on the delivered
  frames.

The stub and the HTTP-passing proxy are exercised end to end by two scripts that
run the **bus's own code** over the stub, both in CI (from the repository root,
because `--directory mcp` also changes the working directory):

```
uv run --directory mcp --with httpx python ../testrig/scripts/stub_roundtrip.py
uv run --directory mcp --with httpx python ../testrig/scripts/probe_status_shape.py
```

`stub_roundtrip.py` runs the real `MattermostAdapter` against
`stub.MattermostStub` over TCP. `probe_status_shape.py` runs `lease._rows` and
`coordinator_of` over the stub's status answer: a test of the stub's own JSON
cannot tell a shape it invents from the one the bus parses, and that cost a
review round (the custom status is a JSON *string* in `props.customStatus`, not
a top-level object). Both exit non-zero on a gap.

The bus's own calls, as measured from the running server, are the stub's
`KNOWN` table plus three the first `bus-run` found and added: the heartbeat's
status write, the roster read (`GET /api/v4/users`), and the exit path's
unregister (`DELETE /register/v1/agents/<name>`). A run reports any *other*
unknown call in `notes`, and `bus-run` exits 1 on it (a new gap is a broken
rig, not a delivery number).

Three answers are computed from the posts the stub holds rather than fixed: a
channel's posts (with `since`, `per_page` and `page`), the bot's channels (with
`last_post_at`, which is what the bus's backfill tests before reading), and its
memberships. A fixed empty post list made `cut` measure the stub: after the
reconnect the bus's catch-up read found nothing, so 10 of 10 read as lost.

`bus-run`'s exit code says whether its numbers can be compared: 0 for a run
whose baseline lost nothing and whose stand-in answered every call, 1 when the
stub did not know a call, when the receiver's inbox never opened, when the run
sent no frame at all, when a baseline lost a frame, when a fault run whose
session must come back never did (a send after the window under `restart`,
`hang`, `cut` and `blackhole`; a post after the resume under `kill-client`: the
run measured a session that stayed down, not the fault), or when **frames were
sent and none arrived** and no fault explains it: `stall`, `reorder` and `duplicate`
pass every frame on, so silence under them is the path, and so is silence under
`none` — a path that carried nothing is not a measurement of the fault (issue
#47); 2 when no interpreter could start the bus at all.

A fault that discards what it sees — `drop` and `blackhole`, which are the same
discard — makes such a run a measurement only when the relay saw **every** posted
frame and discarded **every** one: a frame it never saw was lost before the
injection point, and one it forwarded and that still did not arrive is the path's
loss. `halfopen` freezes the line, so it explains the silence only when no posted
frame had been delivered before the freeze. `cut` aborts the transport and the bus
recovers by catch-up read, so nothing arriving under it means the recovery
recovered nothing — exit 1, not a measurement of the cut.

In every case the run **says** which it is, in `notes` and on stderr:

```
bus-testrig: nothing arrived: --fault drop discards every frame it sees (3 of 3
posted frame(s) discarded at the relay), so the run measures the fault
```

The counters behind that are the relay's **posted** ones: the plain ones also
count status and presence frames, and "the fault saw every frame" cannot be read
from them. A silent run that kept no counters explains nothing and exits 1.
Counting delivered frames alone cannot separate the two situations, which is what
made a run that proved nothing read like a healthy one.

The JSON report is printed in every case, because the numbers are how a failure
is read.

`--expect-loss` makes a fault run's claim ("this fault loses some and carries
the rest") part of that check: a run that lost nothing exits 1, and so does one
that lost *everything* — a dead receiver reads the same as a fault that drops
every frame, so it cannot prove that a fault works. That is why CI asks it of
a `--every 3` step rather than `--after 1` alone.

`cut` and `halfopen` are not plain delivery measurements, and the report's
`notes` says which: after a cut the inbox reconnects and backfills, so the loss
counts the catch-up read as well as the socket, and a frozen line carries
nothing until the client's own heartbeat gives up.

## The NATS transport

```
CB_NATS_SERVER=/path/to/nats-server uv run --directory testrig bus-testrig bus-run --transport nats --count 10 --summary
uv run --directory testrig bus-testrig bus-run --transport nats --fault restart --summary
uv run --directory testrig bus-testrig bus-run --transport nats --fault all --count 100 --summary
uv run --directory testrig bus-testrig coord-run --transport nats --stops 3 --summary
```

The nightly `full-nats` job writes each `--summary` table into the run's step
summary and uploads the full output as the `full-nats-results` artifact.

It needs nats-server 2.15.3 (`CB_NATS_SERVER`, `MM_NATS_SERVER`, else `nats-server`
on `PATH`; without one the run says so and exits 2) and `mcp/` synced, as `bus-run`
does. What stands in for what (`natsrig.py`):

| Part | In the rig |
|---|---|
| nats-server | the real binary, JetStream on, a store directory of its own |
| the login service's credential | a static nkey user per agent with the rights `services/auth/internal/callout/perms.go` grants for the home |
| the login service's streams and consumers | an admin user makes them, and remakes an agent's consumers from the `since` of its CONNECT, which the byte proxy hands it before the server sees it (the callout's part) |
| the Keycloak login | `natsserver.py`, a shim that installs a stand-in login with the agent's nkey and then runs the server's normal entry point; product code unchanged |
| Mattermost | the stub, for the half of a NATS-home session that still uses it (bot, roster, status) |
| the bridge's records (only with `CB_PLANB_TEST=2`) | the admin writes the bridge's signal with the bot prefix `agent-`, a bot record per agent and the owner's account record, again every 60 s (`Admin.play_bridge`) |

The config, the streams and the consumers copy the `rig` fixture and
`_consumer_specs`/`_login_service` of `mcp/tests/test_natsbus.py`; the rights copy
`perms.go` (the fixture lacks the ask record and the STATS line). A test counts
`perms.go`'s rights, and another compares the agent name with `natsbus.agent_name`.

The faults on NATS are windows, opened half way through the sends for
`--fault-sec` (default 2 s); `--fault all` runs the baseline and each in turn:

| Fault | What happens |
|---|---|
| `restart` | nats-server SIGKILLed, started again on the same port and store |
| `hang` | nats-server SIGSTOPped, SIGCONTed at the window's end (`proc.pause`) |
| `kill-client` | the receiving MCP server SIGKILLed and started again on the same identity and state directory: a resumed session |
| `cut` | the byte proxy aborts every connection, and refuses new ones in the window |
| `blackhole` | the byte proxy discards what the server sends, then aborts the connections that lost bytes (a stream with a gap cannot go on) |
| `stall` | each chunk from the server held `--stall-sec` |
| `halfopen` | both directions stop being read, and are read again at the window's end |

The byte proxy refuses a connection only while the nats-server does not listen
(`refused`); the first one after a restart is passed on at once, its INFO within
a second (#221). A session that stays away longer is the bus's, not the proxy's.

A PR-sized cut run (`--count 10`, the default 50 ms apart) has sent all its
posts within about half a second, inside the 2 s window: it measures the window,
not what comes after it. The nightly run (100 posts, 5 s) outlasts the window and
measures the posts after it.

A CONNECT whose consumer reconcile fails is aborted before the server sees it,
and the next connection of that agent retries the reconcile, as the login
service lets a client retry a refused login (#218); the failure is kept per
agent, so one agent's early attempt against a restarting server fails only that
connection. A run whose agent still had no consumers at its last connection
exits 1, and aborted CONNECTs a later one cured are counted and printed.

Isolation: each run gets its own 0700 temporary directory, its own JetStream
store and an ephemeral port, and the host's `CB_*`/`MM_*` are stripped. A rig
killed with SIGKILL leaves its nats-server and session processes behind (they
are no process group of the rig's own), but they hold no shared port or store,
so the next run is unaffected. `pgrep -fl 'bus-testrig-'` lists them (the
nats-server's conf path and the sessions' state directories name it) and
`pkill -f 'bus-testrig-'` stops them; a stray one costs a process, not a run. If
another process takes the probed port before the first start, the server starts
again on a fresh one; a restart keeps its port, since the clients hold it, and
the start checks the server's INFO so a foreign listener on that port is never
taken for it.

A send that fails under the fault is not counted as sent, and the run goes on
(`notes` counts them). Under `restart`, `hang`, `cut` and `blackhole` the run
has to show the sender's recovery: after the window it keeps posting until one
send succeeds or 90 s pass (generous: the bus retries at most every 5 s), so
such a run can send up to ~180 posts beyond `--count`; a run that never gets one
exits 1 (the session did not come back, so the numbers measure nothing). Under
`kill-client` the recovery is the receiver's: an arrival after the resume. The
report's `recovery_s` says how long that took after the window closed (with the
0.5 s recovery pace it is the bus's reconnect delay, #222). `--summary` prints a
table after the JSON: per fault the p50, p95 and maximum wait, loss and duplicates.

### Nobody told as gone during a cut (plan B piece 2)

```
CB_PLANB_TEST=2 uv run --directory testrig bus-testrig bus-run --transport nats --fault cut --count 20 --summary
```

With the roster from NATS (plan B piece 2, ADR-014 phase 5) a cut makes the roster
empty, and nobody may be told as gone then (RT-6.21). `CB_PLANB_TEST` is plan B's
test switch (`mcp/.../planb.py`): the rig reads it from its own environment, hands it
to the sessions (it strips every other `CB_*`) and sets `PYTEST_CURRENT_TEST` for
them, since the switch counts only under a test. With piece 2 in it:

- the admin plays the bridge's part of the roster (the table above), so the roster
  shows each agent as `agent-<NATS name>`, a name `departed.mentioned` reads;
- the sender waits until its roster (`coordinator("roster")`) shows the receiver, then
  every send is `TASK @agent-<receiver> @<canary> bus-run <n>`; after the window and
  the recovery, 20 more sends at the run's pace, since a cut's own sends fail;
- the run records, per window (`before`, `window`, `after`), every **told gone**: a
  `post_message` answer whose `not_reachable` names a live agent `gone …`
  (`departed.gone`, RT-6.14 step 10), or a `wait_for_events` event marked `departed`
  or answering in a thread that a live agent `is gone` (RT-6.21); and every **roster
  miss**: a live agent in `not_reachable` for another reason, or in `not_here`;
- the canary, `agent-qqq…` (no agent's name), is in every `not_reachable` whose check
  ran, so the report's `roster.checks` counts the checks per phase.

The measurand is `told_gone`; `--summary` adds the columns `told gone` and `roster
miss` (`-` without piece 2). The run exits 1 when a live agent was told as gone
(under every fault but `kill-client`, which really stops the receiver for its window,
so there the number is reported), and when no answer named the canary: a run that
never checked the roster measured nothing. Roster misses are reported, not failed.

`coord-run` starts `--sessions` servers in one home (default 3), waits for the
crown, SIGKILLs the holder's process group and waits for a survivor's crown log
(`crown-<uid>.log`) to say `gained`; then a fresh session joins and the next stop
follows (`--stops`, default 3). The take-over is that log's time minus the kill
(whole seconds). The report also carries `presence_stale_s`: when the dead
holder's presence turned stale, a lower bound. A hard-stopped holder sends no
`bye`, so the crown passes only once its presence is `PRESENCE_FRESH_SEC` (90 s)
old: three stops on 2026-11-26 measured 89, 90 and 120 s (the rig's `CB_HEARTBEAT_SEC=30` — the bus minimum, the default is 300 — adds the second beat), against the phase-4
target of 30 s.
`--transport mattermost` is accepted, but the stand-in does not carry the
Mattermost election to a crown, so that take-over is not yet measured.

CI (`.github/workflows/testrig.yml`): the `harness` job runs `--fault all --count
10`, one hard stop and, with `CB_PLANB_TEST=2`, a cut of 20 posts on every PR; the
`full-nats` job runs 100 messages per fault, 50 hard stops and, with
`CB_PLANB_TEST=2`, every fault with 100 messages again and one cut of 120 s (longer
than a presence stays fresh, 90 s), nightly and by hand (`workflow_dispatch`).

## Layout

```
testrig/
  pyproject.toml        own project: the move changes mcp/pyproject.toml, not this
  measurands.toml       the names, their acceptance row, their status
  src/bus_testrig/
    cli.py              bus-testrig measurands | echo-run | bus-run | coord-run
    gateway.py          one port speaking HTTP and the WebSocket upgrade
    stub.py             MattermostStub: the measured REST answers and a scripted socket
    bus.py              the MCP server as a subprocess, and the run that measures it
    measurands.py       reads measurands.toml; a run cannot report an undeclared name
    proxy.py            the proxies (WebSocket-only, HTTP-passing, bytes for NATS) and the faults
    proc.py             the operating-system faults, and where they cannot be produced
    runner.py           a run's bookkeeping, the percentiles, the report, the summary
    natsrig.py          the NATS transport: nats-server, the login service's part, the run
    natsserver.py       the shim a NATS-home session runs (a stand-in login)
    coord.py            hard stops of the coordinator and the take-over
  scripts/ci_fault_check.py  the CI check that a fault changes the numbers
  scripts/stub_roundtrip.py  the real adapter against the stub, over TCP
  scripts/probe_status_shape.py  the bus's status reader over the stub's answer
  tests/              against the real websockets library, a real killed process
                      and, in test_bus_process.py, the real MCP server
```
