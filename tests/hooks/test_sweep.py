"""mm-agent-sweep.sh: clean up session files a failed teardown left behind (RT-6.1)."""

from __future__ import annotations

import os

SWEEP = "bin/mm-agent-sweep.sh"
DEAD_PID = "999999"  # far above any pid in use on a test machine


def session(run, name: str, *, token: str = "bot-token-for-tests", pid: str = DEAD_PID,
            legacy: bool = False, prefix: str = "agent-") -> str:
    d = run.state_dir
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.env"
    lines = ["# ephemeral session — do not commit",
             f"{'MM_BOT_NAME' if legacy else 'MM_REG_BOT_NAME'}={name}",
             f"MM_BOT_USERNAME={prefix}{name}", f"MM_BOT_TOKEN={token}",
             f"MM_SESSION_PID={pid}"]
    f.write_text("\n".join(lines) + "\n")
    return f.name


def deletes(mm) -> list[str]:
    return [p for m, p, _ in mm.writes if m == "DELETE"]


def test_unregisters_a_dead_session_and_removes_its_file(mm, run):
    f = session(run, "mac-proj-aaa111")
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "unregistered mac-proj-aaa111" in r.stderr
    assert deletes(mm) == ["/register/v1/agents/mac-proj-aaa111"]
    assert not (run.state_dir / f).exists()


def test_a_bot_whose_token_is_rejected_is_gone_already(mm, run):
    """A 401 for the bot's own token means the bot no longer exists: the file is
    stale, not a failure to keep forever."""
    f = session(run, "mac-proj-bbb222", token="revoked")
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "gone    mac-proj-bbb222" in r.stderr and "FAILED" not in r.stderr
    assert not (run.state_dir / f).exists()
    assert "1 removed, 0 failed" in r.stderr


def test_a_failure_says_why_and_keeps_the_file(mm, run):
    f = session(run, "mac-proj-ccc333")
    mm.deny.add("/register/v1/agents/mac-proj-ccc333")
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "FAILED mac-proj-ccc333: unregister failed http=403" in r.stderr
    assert (run.state_dir / f).exists()


def test_a_live_session_is_left_alone(mm, run):
    f = session(run, "mac-proj-ddd444", pid=str(os.getpid()))
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "ACTIVE  mac-proj-ddd444" in r.stderr
    assert deletes(mm) == [] and (run.state_dir / f).exists()


def test_files_from_before_the_registration_name_are_swept_too(mm, run):
    f = session(run, "mac-proj-eee555", legacy=True)
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "unregistered mac-proj-eee555" in r.stderr
    assert not (run.state_dir / f).exists()


def state_file(run, name: str, *, days_old: float) -> str:
    """A per-bot state file (no token), aged by its mtime."""
    import time
    d = run.state_dir
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("STATE=idle\n")
    t = time.time() - days_old * 86400
    os.utime(f, (t, t))
    return name


GONE = "g" * 26
LIVE = "l" * 26


def test_state_files_are_not_sessions(mm, run):
    """RT-6.1: presence, listen, turn and coord files are no sessions to skip noisily."""
    state_file(run, f"presence-{GONE}.env", days_old=0)
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert "skip" not in r.stderr and deletes(mm) == []


def test_old_state_of_a_bot_without_session_is_removed(mm, run):
    """RT-6.1: leftovers of bots that are gone go; a live session's state stays."""
    session(run, "mac-proj-fff666", pid=str(os.getpid()))  # live, uid below
    (run.state_dir / "mac-proj-fff666.env").write_text(
        (run.state_dir / "mac-proj-fff666.env").read_text() + f"MM_BOT_USER_ID={LIVE}\n")
    old = [state_file(run, f"{k}-{GONE}.env", days_old=3)
           for k in ("coord", "listen", "presence", "turn")]
    old.append(state_file(run, f"stats-{GONE}.jsonl", days_old=3))
    old.append(state_file(run, f"permission-{GONE}.log", days_old=3))  # the relay log (RT-6.13)
    old.append(state_file(run, f"crown-{GONE}.log", days_old=3))  # the crown log (RT-6.6 step 11)
    fresh = state_file(run, f"coord-{'f' * 26}.env", days_old=0.1)  # touched today
    live = state_file(run, f"presence-{LIVE}.env", days_old=3)
    dry = run(SWEEP, MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert "would remove 7 state file(s)" in dry.stdout
    assert all((run.state_dir / f).exists() for f in old)
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert "removed 7 state file(s) of bots without a session" in r.stderr
    assert not any((run.state_dir / f).exists() for f in old)
    assert (run.state_dir / fresh).exists() and (run.state_dir / live).exists()


def mcp_session(run, name: str, *, hours_ago: float) -> str:
    """A bot an agent registered itself, its Claude process gone."""
    import time
    f = session(run, name)
    p = run.state_dir / f
    target = run.state_dir / f"mcp-sid-{name}.env"
    p.rename(target)
    t = time.time() - hours_ago * 3600
    os.utime(target, (t, t))
    return target.name


def test_a_bot_kept_for_claude_continue_waits_out_the_grace_period(mm, run):
    """RT-6.10: a self-registered bot outlives its process for --continue; the
    sweep leaves it alone for 24 h unless forced."""
    recent = mcp_session(run, "hogan-vtpm-aaa1", hours_ago=2)
    old = mcp_session(run, "hogan-vtpm-bbb2", hours_ago=30)
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "KEPT    hogan-vtpm-aaa1 (ended 2 h ago" in r.stderr
    assert "unregistered hogan-vtpm-bbb2" in r.stderr
    assert (run.state_dir / recent).exists() and not (run.state_dir / old).exists()
    forced = run(SWEEP, "--apply", "--force", MM_BOT_TOKEN=None)
    assert "unregistered hogan-vtpm-aaa1" in forced.stderr


def test_orphans_are_found_by_the_registrar_prefix(mm, run):
    """arc42 §7.1: without a bot of its own the sweep learns the prefix from a
    session file; only bots with that prefix can be orphans."""
    session(run, "live-one", pid=str(os.getpid()), prefix="bot-")
    mm.agent("o" * 26, "bot-orphan", 1000)
    mm.agent("f" * 26, "agent-foreign", 1000)
    r = run(SWEEP, MM_REG_SECRET="s")
    assert "would unregister bot-orphan" in r.stdout and "agent-foreign" not in r.stdout
    run(SWEEP, "--apply", MM_REG_SECRET="s")
    assert deletes(mm) == ["/register/v1/agents/orphan"]


def test_endpoint_files_of_gone_servers_are_removed(run):
    """RT-6.17 error handling: a crashed MCP server leaves its endpoint file;
    the sweep removes it once its MM_BUS_PID is dead and the file an hour old.
    A live server's file and a fresh one stay."""
    import time

    d = run.state_dir
    d.mkdir(parents=True, exist_ok=True)

    def endpoint(key: str, pid: str, hours_ago: float) -> str:
        f = d / f"endpoint-sid-{key}.env"
        f.write_text(f"MM_BUS_ENDPOINT=http://127.0.0.1:9\nMM_BUS_KEY=x\nMM_BUS_PID={pid}\n")
        t = time.time() - hours_ago * 3600
        os.utime(f, (t, t))
        return f.name

    gone = endpoint("a" * 16, DEAD_PID, 2)
    fresh = endpoint("b" * 16, DEAD_PID, 0)
    live = endpoint("c" * 16, str(os.getpid()), 2)
    dry = run(SWEEP, MM_BOT_TOKEN=None)
    assert "would remove 1 endpoint file(s)" in dry.stdout and (d / gone).exists()
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "removed 1 endpoint file(s)" in r.stderr
    assert not (d / gone).exists() and (d / fresh).exists() and (d / live).exists()


def test_a_leftover_endpoint_temp_file_is_swept_too(run):
    """A signal between writing `endpoint-*.env.tmp` and its `os.replace` leaves
    the temp file behind, and nothing else would ever remove it: the sweep took
    only `endpoint-*.env` (review of #34)."""
    import time

    d = run.state_dir
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"endpoint-sid-{'d' * 16}.env.tmp"
    tmp.write_text("MM_BUS_ENDPOINT=http://127.0.0.1:9\nMM_BUS_KEY=x\n"
                   f"MM_BUS_PID={DEAD_PID}\n")
    t = time.time() - 2 * 3600
    os.utime(tmp, (t, t))

    dry = run(SWEEP, MM_BOT_TOKEN=None)
    assert "would remove" in dry.stdout and tmp.exists()
    run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert not tmp.exists(), "the leftover temp file survived the sweep"


def test_a_bot_kept_for_the_workspace_waits_out_the_grace_period_only(mm, run):
    """RT-6.10 step 7: a server without a session id marked its end and kept
    the bot; its harness (MM_SESSION_PID) lives on, so only the grace counts."""
    import time
    files = {}
    for name, hours in (("ws-recent-aaa1", 2), ("ws-old-bbb2", 30)):
        f = run.state_dir / session(run, name, pid=str(os.getpid()))
        t = time.time() - hours * 3600
        target = run.state_dir / f"mcp-{name}.env"
        target.write_text(f.read_text() + f"MM_SERVER_ENDED={int(t)}\n")
        f.unlink()
        os.utime(target, (t, t))
        files[name] = target
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "KEPT    ws-recent-aaa1 (ended 2 h ago" in r.stderr
    assert "unregistered ws-old-bbb2" in r.stderr
    assert files["ws-recent-aaa1"].exists() and not files["ws-old-bbb2"].exists()


def test_a_corral_bot_kept_for_continue_waits_out_the_grace_period(mm, run):
    """RT-6.18 step 9 / RT-6.1 step 7: postEnd kept a corral session's bot for
    `claude --continue` (end mark, no host process, no corral pid). The sweep
    counts the grace period from the mark, not from the file's mtime, and
    past it unregisters with the bot's own token and removes its file and its
    host state directory (the same lib function as preStart's reaping)."""
    import time
    files = {}
    for name, hours in (("mac-proj-kept1", 2), ("mac-proj-kept2", 30)):
        f = run.state_dir / session(run, name, pid="")
        t = time.time() - hours * 3600
        f.write_text(f.read_text().replace("MM_SESSION_PID=\n", "")
                     + f"MM_SERVER_ENDED={int(t)}\nMM_KEPT_WORKDIR={'1' * 16}\n"
                     f"MM_KEPT_CLAUDE_SID={'2' * 16}\n")
        (run.state_dir / "hosted" / name).mkdir(parents=True)
        files[name] = f
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "KEPT    mac-proj-kept1 (ended 2 h ago" in r.stderr
    assert "unregistered mac-proj-kept2" in r.stderr
    assert deletes(mm) == ["/register/v1/agents/mac-proj-kept2"]
    assert files["mac-proj-kept1"].exists() and (run.state_dir / "hosted" / "mac-proj-kept1").is_dir()
    assert not files["mac-proj-kept2"].exists()
    assert not (run.state_dir / "hosted" / "mac-proj-kept2").exists()


def test_a_dead_takeovers_claim_goes_back(mm, run):
    """RT-6.18 step 10: a preStart that died after claiming a kept bot
    (.<session>.env.taken-<pid>) gives it back under its name, so it is
    reaped in time. A claim whose bot already has a session file is only a
    copy and goes."""
    import time
    f = run.state_dir / session(run, "mac-proj-kept3", pid="")
    f.write_text(f.read_text().replace("MM_SESSION_PID=\n", "")
                 + f"MM_BOT_USER_ID={'k' * 26}\nMM_SERVER_ENDED={int(time.time()) - 60}\n"
                 f"MM_KEPT_WORKDIR={'1' * 16}\nMM_KEPT_CLAUDE_SID={'2' * 16}\n")
    claim = run.state_dir / f".{f.name}.taken-{DEAD_PID}"
    f.rename(claim)
    copy = run.state_dir / f".other.env.taken-{DEAD_PID}"
    copy.write_text(f"MM_BOT_USER_ID={'c' * 26}\n")
    (run.state_dir / "live.env").write_text(f"MM_REG_BOT_NAME=live\nMM_BOT_USER_ID={'c' * 26}\n"
                                            f"MM_SESSION_PID={os.getpid()}\n")
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "KEPT    mac-proj-kept3" in r.stderr
    assert f.exists() and not claim.exists() and not copy.exists()


# --- state keyed by the NATS name (RT-6.1 step 7, RT-6.2 step 9, plan B piece 2b) ---

NAME = "g" * 25  # a NATS name: 25 lower-case base32 characters, no prefix (#274)
OTHER = "h" * 25
DEAD_UID = "d" * 26
# Today's name-keyed kinds and those plan B piece 14c moves to the name (any kind counts):
# the MCP server's (askmode, asks, channel, gate, notice, read, remind, stall, ...) and the
# shell's (listen, update, presence, coord, turn).
NAME_KINDS = ("last", "threads", "unanswered", "listen", "update", "presence", "coord", "turn",
              "askmode", "asks", "channel", "gate", "notice", "read", "remind", "stall")


def name_state(run, name: str, *, uid: str | None, days_old: float) -> list[str]:
    """The files of one NATS name, aged; with `uid` the marker copied-<name>.env naming it."""
    files = [state_file(run, f"{k}-{name}.env", days_old=days_old) for k in NAME_KINDS]
    files.append(state_file(run, f"stats-{name}.jsonl", days_old=days_old))
    files.append(state_file(run, f"crown-{name}.log", days_old=days_old))
    files += [state_file(run, f"{k}-{name}.lock", days_old=days_old) for k in ("asks", "remind", "stats")]
    if uid is not None:
        m = run.state_dir / f"copied-{name}.env"
        m.write_text(f"CB_COPIED_FROM={uid}\n")
        t = os.path.getmtime(run.state_dir / files[0])
        os.utime(m, (t, t))
        files.append(m.name)
    return files


def test_name_keyed_state_of_bots_without_a_session_is_removed(mm, run):
    """RT-6.1 step 7: a name whose marker names only bots without a session file, none of
    its files touched for a day, loses all of them, every kind, the marker too."""
    files = name_state(run, NAME, uid=DEAD_UID, days_old=3)
    dry = run(SWEEP, MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    n = len(files)
    assert f"would remove {n} state file(s) keyed by a NATS name" in dry.stdout
    assert all((run.state_dir / f).exists() for f in files)
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert f"removed {n} state file(s) keyed by a NATS name" in r.stderr
    assert "skip" not in r.stderr and deletes(mm) == []
    assert not any((run.state_dir / f).exists() for f in files)


def test_name_keyed_state_of_a_live_session_stays(mm, run):
    """RT-6.1 step 7: the marker ties the name to a bot whose session file is there: kept,
    however old the files; so does a bot whose MCP server still runs (its endpoint file)."""
    session(run, "mac-proj-ggg777", pid=str(os.getpid()))
    f = run.state_dir / "mac-proj-ggg777.env"
    f.write_text(f.read_text() + f"MM_BOT_USER_ID={LIVE}\n")
    kept = name_state(run, NAME, uid=LIVE, days_old=3)
    served_uid = "s" * 26
    (run.state_dir / f"endpoint-sid-{'e' * 16}.env").write_text(
        f"CB_BUS_ENDPOINT=http://127.0.0.1:9\nCB_BUS_KEY=x\nCB_BUS_PID={os.getpid()}\n"
        f"CB_MM_BOT_USER_ID={served_uid}\n")
    served = name_state(run, OTHER, uid=served_uid, days_old=3)
    m = run.state_dir / f"copied-{OTHER}.env"
    m.write_text(f"CB_COPIED_FROM={DEAD_UID}\nCB_COPIED_FROM={served_uid}\n")  # one of two bots
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert "keyed by a NATS name" not in r.stderr
    assert all((run.state_dir / x).exists() for x in kept + served)


def test_name_keyed_state_touched_today_stays(mm, run):
    """RT-6.1 step 7: one file of the name written within a day keeps the name's files."""
    files = name_state(run, NAME, uid=DEAD_UID, days_old=3)
    os.utime(run.state_dir / f"presence-{NAME}.env")
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert "keyed by a NATS name" not in r.stderr
    assert all((run.state_dir / f).exists() for f in files)


def test_name_keyed_state_without_a_marker_stays_unless_forced(mm, run):
    """RT-6.1 step 7: without copied-<name>.env nothing ties the name to a session: left,
    with a note; --force includes them by the day rule."""
    files = name_state(run, NAME, uid=None, days_old=3)
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert f"left {len(files)} state file(s) keyed by a NATS name without copied-<name>.env" in r.stderr
    assert all((run.state_dir / f).exists() for f in files)
    r = run(SWEEP, "--apply", "--force", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert f"removed {len(files)} state file(s) keyed by a NATS name" in r.stderr
    assert not any((run.state_dir / f).exists() for f in files)


def test_a_session_file_with_a_name_like_name_is_still_a_session(mm, run):
    """RT-6.1 step 7: a file that holds a bot's token is a session whatever its name; the
    name-keyed sweep never removes it, and a 26-character bot id is no NATS name."""
    f = session(run, "mac-" + "a" * 25, pid=str(os.getpid()))
    os.utime(run.state_dir / f, (0, 0))
    uid_file = state_file(run, f"presence-{LIVE}.env", days_old=0)
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert "ACTIVE  mac-" in r.stderr and "keyed by a NATS name" not in r.stderr
    assert (run.state_dir / f).exists() and (run.state_dir / uid_file).exists()


def endpoint_file(run, *, pid: str, key: str | None, uid: str | None = None) -> str:
    f = run.state_dir / f"endpoint-sid-{'e' * 16}.env"
    f.write_text("CB_BUS_ENDPOINT=http://127.0.0.1:9\nCB_BUS_KEY=x\n"
                 f"CB_BUS_PID={pid}\n" + (f"CB_MM_BOT_USER_ID={uid}\n" if uid else "")
                 + (f"CB_STATE_KEY={key}\n" if key else ""))
    return f.name


def test_a_running_server_keeps_the_name_it_keys_by(mm, run):
    """RT-6.1 step 7: a running MCP server names its state key in its endpoint file
    (CB_STATE_KEY); its name's files stay whatever the marker says (it may run as another
    bot by now, or as none), and a name without any marker too."""
    files = name_state(run, NAME, uid=DEAD_UID, days_old=3)
    others = name_state(run, OTHER, uid=None, days_old=3)
    endpoint_file(run, pid=str(os.getpid()), key=NAME)
    r = run(SWEEP, "--apply", "--force", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert all((run.state_dir / f).exists() for f in files)
    assert f"removed {len(others)} state file(s) keyed by a NATS name" in r.stderr


def test_a_gone_servers_state_key_keeps_nothing(mm, run):
    """RT-6.1 step 7: an endpoint file whose CB_BUS_PID is dead ties neither its key nor
    its bot: the name's files go by the marker's and the day's rule."""
    files = name_state(run, NAME, uid=DEAD_UID, days_old=3)
    endpoint_file(run, pid=DEAD_PID, key=NAME, uid=DEAD_UID)
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert f"removed {len(files)} state file(s) keyed by a NATS name" in r.stderr
    assert not any((run.state_dir / f).exists() for f in files)


def test_a_symlinked_marker_ties_nothing(mm, run):
    """RT-6.1 step 7: a marker that is a symlink is no marker: the name's files stay
    (untied) without --force."""
    files = name_state(run, NAME, uid=None, days_old=3)
    target = run.state_dir.parent / "elsewhere.env"
    target.write_text(f"CB_COPIED_FROM={DEAD_UID}\n")
    (run.state_dir / f"copied-{NAME}.env").symlink_to(target)
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert "without copied-<name>.env" in r.stderr and "removed" not in r.stderr.replace("0 removed", "")
    assert all((run.state_dir / f).exists() for f in files) and target.exists()
