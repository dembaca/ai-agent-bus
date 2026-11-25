"""RT-6.9: the bus's token overhead, measured from a Claude Code transcript."""

import json
import os
import threading

import pytest

from mattermost_ai_agent_bus import overhead, session_file, stats
from mattermost_ai_agent_bus.overhead import CHARS_PER_TOKEN, KEYS

BUS = "mcp__plugin_ai-agent-bus_ai-agent-bus__"
FIXED = overhead.Fixed(instructions=400, tools=4000, skill=20000)
T0 = "2026-10-24T10:00:00.000Z"


class Transcript:
    def __init__(self):
        self.rows = []
        self.n = 0

    def user(self, text, origin=None, meta=False):
        row = {"type": "user", "isSidechain": False, "message": {"role": "user", "content": text},
               "timestamp": T0}
        if origin:
            row["origin"] = {"kind": origin}
        if meta:
            row["isMeta"] = True
        self.rows.append(row)

    def results(self, *pairs):
        self.rows.append({"type": "user", "isSidechain": False, "timestamp": T0, "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": body}
                                        for i, body in pairs]}})

    def request(self, *tools, read=100_000, out=200, ts=T0, w1h=0, fresh=10, w5m=1000, text="ok",
                old=False):
        """One model request: its usage on each content entry, as Claude Code writes it
        (`old`: as before the 5 min / 1 h split of cache writes, without `origin`)."""
        self.n += 1
        rid = f"req_{self.n}"
        usage = {"input_tokens": fresh, "cache_read_input_tokens": read,
                 "cache_creation_input_tokens": w5m + w1h, "output_tokens": out}
        if not old:
            usage["cache_creation"] = {"ephemeral_5m_input_tokens": w5m, "ephemeral_1h_input_tokens": w1h}
        blocks = ([{"type": "text", "text": text}] if text is not None else []) + [
            {"type": "tool_use", "id": tid, "name": name, "input": inp} for tid, name, inp in tools]
        # Thinking whose text the transcript leaves out: no characters, still a request.
        blocks = blocks or [{"type": "thinking", "thinking": "", "signature": "s"}]
        for b in blocks:
            self.rows.append({"type": "assistant", "isSidechain": False, "requestId": rid,
                              "timestamp": ts, "message": {"id": rid, "usage": usage, "content": [b]}})

    def other(self, row):
        self.rows.append(row)

    def write(self, path):
        path.write_text("\n".join(json.dumps(r) for r in self.rows) + "\n")
        return path


def measure(t, tmp_path, since=0):
    return overhead.measure(t.write(tmp_path / "t.jsonl"), since, FIXED)


def test_work_carries_only_the_fixed_share(tmp_path):
    """RT-6.9 step 2: a human's turn without the bus: the fixed text's share of
    the input, nothing of the output."""
    t = Transcript()
    t.user("fix the bug", origin="human")
    t.request(("b1", "Bash", {"command": "pytest"}))
    t.results(("b1", "ok"))
    t.request()
    r = measure(t, tmp_path)
    assert r["req"] == 2 and r["bus_req"] == 0
    share = (400 + 4000) / 4 / 101_010
    assert r["bus"]["read"] == round(2 * 100_000 * share) and r["bus"]["out"] == 0
    assert r["fixed"] == 1100 and r["skill"] == 0              # skill never loaded
    assert r["all"] == {"in": 20, "read": 200_000, "w5m": 2000, "w1h": 0, "out": 400}


def test_requests_the_bus_caused_count_whole(tmp_path):
    """RT-6.9 step 2: an agent's message, the Stop hook, the listener and a round
    after bus tool results only: their whole usage is overhead."""
    t = Transcript()
    t.user('<channel source="plugin:ai-agent-bus:ai-agent-bus" kind="mention" '
           'from_trusted_human="false">@me ACK?</channel>', origin="channel", meta=True)
    t.request(("m1", BUS + "reply_in_thread", {"root_id": "r", "message": "ACK"}))
    t.results(("m1", '{"id":"p"}'))
    t.request()                                   # after a bus result only
    t.user("Stop hook feedback:\nBefore this turn ends: … Reply in your HELLO thread", meta=True)
    t.request(("l1", "Bash", {"command": "bin/mm-agent-listen.sh --until 1", "run_in_background": True}))
    t.results(("l1", "Command running in background with ID: x"))
    t.request()
    t.user("<task-notification>\n<tool-use-id>l1</tool-use-id>\n<status>completed</status>",
           origin="task-notification")
    t.request(read=50_000)
    r = measure(t, tmp_path)
    assert r["req"] == r["bus_req"] == 5
    assert r["bus"] == r["all"]


@pytest.mark.parametrize("prefix", [BUS, "mcp__mattermost-agent-bus__"])
def test_a_server_added_by_hand_under_its_old_key_still_counts(tmp_path, prefix):
    """RT-6.9: a server added by hand before 0.96.0 runs on under the key
    mattermost-agent-bus; its rounds are the bus's as before."""
    t = Transcript()
    t.user("check the bus", origin="human")
    t.request(("m1", prefix + "wait_for_events", {"timeout_sec": 1}))
    t.results(("m1", "[]"))
    t.request()                                   # after a bus result only
    assert measure(t, tmp_path)["bus_req"] == 1


def test_a_trusted_humans_message_is_work(tmp_path):
    """RT-6.9: a prompt over the bus stands for one typed in the terminal."""
    t = Transcript()
    t.user('<channel source="plugin:ai-agent-bus:ai-agent-bus" kind="dm" '
           'from_trusted_human="true">please fix the bug</channel>', origin="channel", meta=True)
    t.request()
    assert measure(t, tmp_path)["bus_req"] == 0


def test_bus_data_stays_in_the_context_until_compaction(tmp_path):
    """RT-6.9 step 2: tool results, loaded schemas and the skill raise the share of
    every later request; a compaction drops what the bus put in."""
    t = Transcript()
    t.other({"type": "attachment", "attachment": {"type": "deferred_tools_delta",
                                                   "addedNames": [BUS + "get_thread"]}})
    t.user("look at the thread", origin="human")
    t.request(("s1", "ToolSearch", {"query": "select:get_thread"}),
              ("k1", "Skill", {"skill": "ai-agent-bus:ai-agent-bus"}))
    schema = '<function>{"name": "' + BUS + 'get_thread"}</function>'
    t.results(("s1", f"<functions>{schema}<function>other</function></functions>"), ("k1", "loaded"))
    t.request(("g1", BUS + "get_thread", {"post_id": "x" * 26}), ("b1", "Bash", {"command": "ls"}))
    t.results(("g1", "y" * 40_000), ("b1", "z" * 40_000))
    t.request(out=10_000)
    t.other({"type": "system", "subtype": "compact_boundary"})
    t.user("go on", origin="human")
    t.request()
    r = measure(t, tmp_path)
    tool_call = len(json.dumps({"post_id": "x" * 26}))
    assert r["bus_req"] == 0 and r["fixed"] == (400 + 20000) // 4 and r["skill"] == 1
    shares = [400 / 4 / 101_010,                                         # deferred, no skill yet
              (20400 + len(schema)) / 4 / 101_010,                      # skill and schema loaded
              (20400 + len(schema) + tool_call + 40_000) / 4 / 101_010,  # + the bus's result
              20400 / 4 / 101_010]                                      # compacted
    assert r["bus"]["read"] == round(sum(100_000 * s for s in shares))
    assert r["bus"]["out"] == round(tool_call / 4)


def test_only_requests_of_this_session_count(tmp_path):
    t = Transcript()
    t.user("earlier", origin="human")
    t.request(ts="2026-10-23T10:00:00.000Z")
    t.user("now", origin="human")
    t.request()
    since = overhead._ts({"timestamp": "2026-10-24T09:00:00.000Z"})
    assert measure(t, tmp_path, since)["req"] == 1
    assert overhead.measure(tmp_path / "missing.jsonl", 0, FIXED) is None


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", "u" * 26)
    monkeypatch.delenv("MM_STATS_CHANNEL", raising=False)
    return tmp_path


def test_the_summary_carries_the_tokens(state, tmp_path):
    """RT-6.9 step 2: the hook names the transcript, the summary measures it."""
    t = Transcript()
    t.user("hi", origin="human")
    t.request(ts="2030-01-01T00:00:00.000Z")
    path = t.write(tmp_path / "t.jsonl")
    stats.record("turn")
    stats.record("transcript", path=str(path))
    s = stats.summary("0.32.0", FIXED)
    assert s["tok"]["req"] == 1 and s["tok"]["all"]["read"] == 100_000
    stats.record("transcript", path=str(tmp_path / "gone.jsonl"))
    assert "tok" not in stats.summary("0.32.0", FIXED)


def test_never_through_a_symlink(tmp_path):
    """RT-6.18 (security): the transcript is opened with O_NOFOLLOW; a symlink
    in its place, which the agent could plant, measures nothing."""
    t = Transcript()
    t.user("fix the bug", origin="human")
    t.request()
    real = t.write(tmp_path / "t.jsonl")
    assert overhead.measure(real, 0, FIXED) is not None
    (tmp_path / "link.jsonl").symlink_to(real)
    assert overhead.measure(tmp_path / "link.jsonl", 0, FIXED) is None


def _small(tmp_path):
    t = Transcript()
    t.user("fix the bug", origin="human")
    t.request()
    return t


def test_a_fifo_is_not_waited_for(tmp_path):
    """RT-6.18 (security): a FIFO the agent plants as its transcript is opened
    non-blocking and refused as no regular file; the reader never hangs."""
    fifo = tmp_path / "t.jsonl"
    os.mkfifo(fifo)
    got: list[object] = []
    reader = threading.Thread(target=lambda: got.append(overhead.measure(fifo, 0, FIXED)), daemon=True)
    reader.start()
    reader.join(5)
    assert not reader.is_alive() and got == [None]


def test_a_symlink_at_the_end_is_refused(tmp_path):
    """RT-6.18 (security): never through a symlink in the last part of the path."""
    real = _small(tmp_path).write(tmp_path / "t.jsonl")
    (tmp_path / "link.jsonl").symlink_to(real)
    assert session_file.read_transcript(real) is not None
    assert session_file.read_transcript(tmp_path / "link.jsonl") is None


def test_an_oversize_transcript_is_skipped(tmp_path, monkeypatch):
    """RT-6.18 (security): a transcript over TRANSCRIPT_MAX is not read at all."""
    path = _small(tmp_path).write(tmp_path / "t.jsonl")
    assert overhead.measure(path, 0, FIXED) is not None
    monkeypatch.setattr(session_file, "TRANSCRIPT_MAX", path.stat().st_size - 1)
    assert overhead.measure(path, 0, FIXED) is None
    monkeypatch.setattr(session_file, "TRANSCRIPT_MAX", path.stat().st_size)
    assert overhead.measure(path, 0, FIXED) is not None


def test_the_host_checks_the_opened_file_once_more(tmp_path, monkeypatch):
    """RT-6.18 (security): in the host process of a keyless session, a directory
    under ~/.claude/projects swapped for a symlink to elsewhere after the path
    check (simulated: measured without stats.transcript) reads nothing; a real
    transcript there still counts."""
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = tmp_path / ".claude" / "projects"
    (projects / "p").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _small(tmp_path).write(outside / "t.jsonl")
    (projects / "swapped").symlink_to(outside)
    inside = _small(tmp_path).write(projects / "p" / "t.jsonl")
    session_file.use_hosted_dir(tmp_path / "state" / "hosted" / "s")
    assert overhead.measure(inside, 0, FIXED) is not None
    assert overhead.measure(projects / "swapped" / "t.jsonl", 0, FIXED) is None
    monkeypatch.setattr(session_file, "_fd_path", lambda fd: None)
    assert overhead.measure(inside, 0, FIXED) is None  # cannot tell where it is: nothing



# --- the token split by category (RT-6.9 step 2, method 1: an estimate) -------------------

INPUT = ("in", "read", "w5m", "w1h")
SRC = 'source="plugin:ai-agent-bus:ai-agent-bus"'


def tag(body, kind="mention", trusted=False, post_id="p" * 26, agent=True):
    """A pushed tag as Claude Code writes it: envelope and body."""
    head = (f'<channel {SRC} kind="{kind}"' + (f' post_id="{post_id}"' if post_id else "")
            + f' from_trusted_human="{str(trusted).lower()}" from_agent="{str(agent).lower()}">')
    return head + body + "</channel>", len(head) + len("</channel>")


def inputs(r, cat):
    return sum(r[cat][k] for k in INPUT)


def task(r):
    return {k: r["all"][k] - r["ovh"][k] - r["coord"][k] for k in KEYS}


def holds(r):
    """The invariant: per token type, overhead and coordination never exceed the exact
    total, so task work (the rest) is 0 or more and the three add up to `all`."""
    assert r["m"] == 1
    for k in KEYS:
        assert 0 <= r["ovh"][k] and 0 <= r["coord"][k] and task(r)[k] >= 0
        assert r["ovh"][k] + r["coord"][k] + task(r)[k] == r["all"][k]
    return r


def test_a_peer_task_drives_one_request_then_the_work_is_task(tmp_path):
    """RT-6.9 step 2: a peer's TASK wakes the turn, three Read rounds follow. The body is
    coordination input, the envelope overhead; the first request's text output goes by its
    driver (the message), every later request's to task work, while the message stays in
    the context as coordination input of each later request."""
    t = Transcript()
    text, env = tag("TASK " + "b" * 395)
    t.user(text, origin="channel", meta=True)
    read = ("r1", "Read", {"file_path": "/x"})
    t.request(read, read=0, w5m=0, fresh=100_000, out=1000)
    for i in range(3):
        t.results((f"r{i + 1}", "z" * 8000))
        t.request(*([(f"r{i + 2}", "Read", {"file_path": "/x"})] if i < 2 else []),
                  read=0, w5m=0, fresh=100_000, out=1000)
    r = holds(measure(t, tmp_path))
    call = len(json.dumps({"file_path": "/x"})) / CHARS_PER_TOKEN
    assert r["coord"]["out"] == round((1000 - call) * 400 / (400 + env))
    assert r["ovh"]["out"] == round((1000 - call) * env / (400 + env))
    # The body counts in each of the four requests; the first request's text ("ok") stays
    # in the context of the three later ones, by its driver.
    assert r["coord"]["in"] == round(4 * 400 / CHARS_PER_TOKEN + 3 * 2 * 400 / (400 + env) / CHARS_PER_TOKEN)
    assert r["req_coord"] == 1 and r["req_ovh"] == 0


def test_empty_polls_are_overhead(tmp_path):
    """RT-6.9 step 2: an empty wait_for_events (call and result) is overhead, and the
    request it drives counts as overhead whole."""
    t = Transcript()
    t.user("check the bus", origin="human")
    t.request(("w1", BUS + "wait_for_events", {"timeout_sec": 30}), text=None, out=50)
    t.results(("w1", '{"result":"[]"}'))
    t.request(out=40)
    r = holds(measure(t, tmp_path))
    assert r["ovh"]["out"] == round(len(json.dumps({"timeout_sec": 30})) / 4) + 40
    assert r["coord"] == dict.fromkeys(KEYS, 0)
    assert r["req_ovh"] == 1 and r["req_coord"] == 0


def test_a_poll_with_messages_splits_envelope_and_bodies(tmp_path):
    """RT-6.9 step 2: a wait_for_events result: the bodies of the agents' posts are
    coordination, a trusted human's task work, the server's own events and the JSON
    around them overhead."""
    items = [{"kind": "mention", "from_agent": True, "from_trusted_human": False,
              "post": {"id": "p" * 26, "message": "RESULT " + "c" * 93}},
             {"kind": "dm", "from_agent": False, "from_trusted_human": True,
              "post": {"id": "q" * 26, "message": "please " + "h" * 193}},
             {"kind": "stall", "from_agent": False, "from_trusted_human": False,
              "post": {"message": "s" * 400}}]
    body = json.dumps({"result": json.dumps(items)})
    t = Transcript()
    t.user("go", origin="human")
    t.request(("w1", BUS + "wait_for_events", {}), text=None, out=0)
    t.results(("w1", body))
    t.request(text=None, out=0)
    r = holds(measure(t, tmp_path))
    assert inputs(r, "coord") == round(100 / 4)
    fixed = (400 + 4000) / 4
    assert inputs(r, "ovh") == round(2 * fixed + len("{}") / 4 + (len(body) - 300) / 4)


@pytest.mark.parametrize("word,cat", [("IDLE", "ovh"), ("DONE", "coord")])
def test_an_idle_post_is_overhead_a_done_coordination(tmp_path, word, cat):
    """RT-6.9 step 2: own posts go by their bus word: HELLO, IDLE, BYE are overhead,
    the other words and plain replies coordination; the call is sized exactly."""
    t = Transcript()
    t.user("finish", origin="human")
    inp = {"root_id": "r" * 26, "message": f"{word} " + "x" * 395}
    t.request(("p1", BUS + "reply_in_thread", inp), text=None, out=500)
    r = holds(measure(t, tmp_path))
    other = "coord" if cat == "ovh" else "ovh"
    assert r[cat]["out"] == round(len(json.dumps(inp)) / 4) and r[other]["out"] == 0


def test_a_trusted_humans_prompt_over_the_bus_is_task_work(tmp_path):
    """RT-6.9 step 2: a trusted human's prompt over the bus stands for a typed one: its
    body is task work, only the envelope overhead; nothing is coordination."""
    t = Transcript()
    text, env = tag("please fix the bug " + "x" * 381, kind="dm", trusted=True, agent=False)
    t.user(text, origin="channel", meta=True)
    t.request(read=0, w5m=0, fresh=100_000, out=100)
    r = holds(measure(t, tmp_path))
    assert r["coord"] == dict.fromkeys(KEYS, 0)
    assert r["ovh"]["in"] == round((400 + 4000 + env) / 4)
    assert r["ovh"]["out"] == round(100 * env / (env + 400))


def test_deferred_tools_loaded_by_search(tmp_path):
    """RT-6.9 step 2: with deferred tools the bus's schemas are not in the fixed text; once
    loaded by tool search they are overhead, the other schemas of the result task work."""
    def run(bus_schema):
        t = Transcript()
        t.other({"type": "attachment", "attachment": {"type": "deferred_tools_delta",
                                                       "addedNames": [BUS + "get_thread"]}})
        t.user("look", origin="human")
        t.request(("s1", "ToolSearch", {"query": "select:get_thread"}), text=None, out=0)
        t.results(("s1", f"<functions>{bus_schema}<function>{'o' * 400}</function></functions>"))
        t.request(text=None, out=0)
        return holds(measure(t, tmp_path))

    schema = '<function>{"name": "' + BUS + 'get_thread", "d": "' + "d" * 300 + '"}</function>'
    with_bus, without = run(schema), run("")
    assert inputs(without, "ovh") == round(2 * 400 / 4)          # the instructions only
    assert inputs(with_bus, "ovh") == round(2 * 400 / 4 + len(schema) / 4)


def test_a_compaction_resets_the_segments_and_splits_its_summary(tmp_path):
    """RT-6.9 step 2: a compaction drops the segments; its summary is split by the shares
    of the context before it."""
    t = Transcript()
    t.user("a" * 400, origin="human")
    text, env = tag("TASK " + "b" * 395)
    t.user(text, origin="channel", meta=True)
    t.request(text=None, out=0)
    t.other({"type": "system", "subtype": "compact_boundary"})
    t.other({"type": "user", "isSidechain": False, "isCompactSummary": True, "timestamp": T0,
             "message": {"role": "user", "content": "s" * 1000}})
    t.request(text=None, out=0)
    r = holds(measure(t, tmp_path))
    before = 400 / 4
    after = 1000 * 400 / (800 + env) / 4
    assert inputs(r, "coord") == round(before + after)


def test_the_positional_cache_split(tmp_path):
    """RT-6.9 step 2: the harness caches from the front, so the bus's fixed text and the
    old messages fall into cache reads, the newest message into fresh input."""
    t = Transcript()
    t.user("a" * 4000, origin="human")
    text, env = tag("TASK " + "b" * 3995, post_id="p" * 28)
    pad = (-env) % 4
    text, env = tag("TASK " + "b" * 3995, post_id="p" * (28 + pad))
    t.user(text, origin="channel", meta=True)
    t.request(text=None, out=0, read=2000 + 1100 + 1000, w5m=env // 4, fresh=1000)
    r = holds(measure(t, tmp_path))
    assert r["ovh"] == {"in": 0, "read": 1100, "w5m": env // 4, "w1h": 0, "out": 0}
    assert r["coord"] == {"in": 1000, "read": 0, "w5m": 0, "w1h": 0, "out": 0}
    assert task(r)["read"] == 3000


def test_the_split_never_exceeds_the_total(tmp_path):
    """RT-6.9 step 2: segments that would exceed the exact input are scaled down to it; a
    tool call larger than the booked output is held to it. The three still add up."""
    t = Transcript()
    text, _env = tag("TASK " + "b" * 40_000)
    t.user(text, origin="channel", meta=True)
    t.request(("p1", BUS + "reply_in_thread", {"message": "ACK " + "x" * 9000}), read=50, w5m=50, fresh=50,
              w1h=50, out=10)
    r = holds(measure(t, tmp_path))
    assert inputs(r, "ovh") + inputs(r, "coord") <= 200 and r["coord"]["out"] == 10


def test_an_older_transcript_still_splits(tmp_path):
    """RT-6.9: a transcript of an older format (no `origin`, no 5 min / 1 h split of the
    cache writes, a tag as plain text) splits the same way; its writes count as 5 min."""
    t = Transcript()
    text, env = tag("TASK " + "b" * 395)
    t.user(text)
    t.request(read=0, w5m=0, fresh=100_000, out=0, text=None, old=True)
    r = holds(measure(t, tmp_path))
    assert r["coord"]["in"] == 100 and r["ovh"]["in"] == round((4400 + env) / 4)
    assert r["all"]["w1h"] == 0
