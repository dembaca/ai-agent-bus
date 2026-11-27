"""The MCP server logs in itself (RT-6.34, #154 step 2, the ADR-014 amendment of
2026-11-26): the device approval on the first start with a browser or the
address and code on stderr, never the device code; sessions share one stored
token under the lock; a refresh writes only a complete token; under corral the
relay in the sandbox holds the login on its own store and the host process of
the keyless session (RT-6.18) asks it for tokens."""

import asyncio
import io
import json
import logging
import time

import anyio
import httpx
import pytest
from anyio.abc import SocketAttribute
from test_login import ISSUER, Note, Script, _verify, jwt, mk, put, stored

from mattermost_ai_agent_bus import hosted, keystore, login, login_notice, login_relay

DEVICE = "device-code-never-shown"
LINK = f"{ISSUER}/device-page?user_code=ABCD-EFGH"


class Device(Script):
    """Script with a device code that must appear nowhere."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        resp = super().__call__(request)
        if request.url.path.endswith("/device"):
            body = resp.json()
            body["device_code"] = DEVICE
            return httpx.Response(200, json=body)
        return resp


class Opener:
    def __init__(self, ok: bool):
        self.ok = ok
        self.opened: list[str] = []

    def __call__(self, url: str) -> bool:
        self.opened.append(url)
        return self.ok


def shown(ok: bool, issuer: str = ISSUER):
    opener, err = Opener(ok), io.StringIO()
    return login_notice.Shown(issuer, opener=opener, err=err), opener, err


# --- the first start: device flow, browser, stderr (RT-6.34) ----------------------

async def test_the_first_start_runs_the_device_flow_and_opens_a_browser(caplog):
    show, opener, err = shown(True)
    dm = Note()
    store = keystore.Ephemeral()
    lg = mk(store, transport=httpx.MockTransport(Device()), notifier=login_notice.Combined(show, dm))
    with caplog.at_level(logging.DEBUG):
        await lg.run()
    assert opener.opened == [LINK]                       # the issuer's own link, nothing else
    assert "a browser was opened" in err.getvalue() and "ABCD-EFGH" in err.getvalue()
    assert dm.events[:2] == ["start", "waiting"]         # and the DM to the responsible human
    assert stored(store)                                 # the token is kept for the next start
    assert DEVICE not in err.getvalue() + caplog.text


async def test_without_a_browser_the_address_and_code_are_shown_never_the_device_code(caplog):
    show, opener, err = shown(False)
    with caplog.at_level(logging.DEBUG):
        await mk(transport=httpx.MockTransport(Device()), notifier=show).access_token()
    text = err.getvalue()
    assert opener.opened == [LINK]
    assert LINK in text and "ABCD-EFGH" in text and "a browser was opened" not in text
    assert DEVICE not in text + caplog.text


@pytest.mark.parametrize("link", [
    "http://localhost:8080/realms/bus/device",          # http: shown, never opened
    "https://kc.example.test:8443/realms/bus/device",   # another port
    "https://evil.example.test/realms/bus/device",      # another host
])
async def test_only_an_https_link_on_the_issuers_scheme_host_and_port_opens_a_browser(link):
    show, opener, err = shown(True)
    await show.waiting(login.DeviceGrant(link, "", "ABCD-EFGH", 600))
    assert opener.opened == [] and link in err.getvalue()


async def test_the_approval_goes_on_when_one_way_reached_the_human_and_fails_when_none():
    class Down(Note):
        async def waiting(self, grant):
            raise RuntimeError("no chat")

    show, _, err = shown(False)
    await login_notice.Combined(show, Down()).waiting(login.DeviceGrant(LINK, LINK, "ABCD-EFGH", 600))
    assert "ABCD-EFGH" in err.getvalue()
    with pytest.raises(login.NotifyError):
        await login_notice.Combined(Down(), Down()).waiting(login.DeviceGrant(LINK, LINK, "C", 600))


def test_the_discovered_endpoints_must_share_the_issuers_port():
    assert login.same_site(ISSUER, f"{ISSUER}/token")
    assert login.same_site(ISSUER, "https://kc.example.test:443/realms/bus/token")
    assert not login.same_site(ISSUER, "https://kc.example.test:8443/realms/bus/token")
    assert not login.same_site("http://localhost:8080/r", "http://localhost:9090/r")


# --- several sessions, one secret (RT-6.34 step 6) -----------------------------------

async def test_a_second_session_waits_on_the_lock_and_reuses_the_stored_token(tmp_path):
    script, approving = Script(device_errors=("authorization_pending",) * 3), asyncio.Event()

    class Waits(Note):
        async def waiting(self, grant):
            await super().waiting(grant)
            approving.set()

    one = mk(keystore.HostFile(tmp_path / "login"), transport=httpx.MockTransport(script), notifier=Waits())
    two_note = Note()
    two = mk(keystore.HostFile(tmp_path / "login"), transport=httpx.MockTransport(script), notifier=two_note)
    first = asyncio.create_task(one.access_token())
    await asyncio.wait_for(approving.wait(), 2)          # the first holds the lock in its approval
    second = await two.access_token()                    # waits for it, then refreshes its token
    await first
    assert script.device_requests == 1 and two_note.events == []
    assert script.refreshed == ["refresh-secret-1"]
    assert login.payload_of(second)["preferred_username"] == "alice"


async def test_a_refresh_writes_only_a_complete_checked_token(tmp_path):
    saves: list[tuple[str, str]] = []

    class Spy(keystore.Ephemeral):
        def save(self, name, value):
            saves.append((name, value))
            super().save(name, value)

    class NoRole(Script):
        def _tokens(self):
            self.n += 1
            return httpx.Response(200, json={"access_token": jwt("alice", realm_access={"roles": []}),
                                             "expires_in": 300, "refresh_token": f"refresh-secret-{self.n}"})

    store = Spy()
    put(store, "refresh-secret-0")
    saves.clear()
    with pytest.raises(login.LoginError, match="realm role"):
        await mk(store, transport=httpx.MockTransport(NoRole()), notifier=Note()).access_token()
    assert stored(store) == "refresh-secret-0" and not [s for s in saves if s[0].startswith("refresh_")]
    await mk(store, transport=httpx.MockTransport(Script()), notifier=Note()).access_token()
    (name, value), = [s for s in saves if s[0].startswith("refresh_")]
    record = json.loads(value)                           # one whole record, never a part
    assert record == {"issuer": ISSUER, "client_id": "agent-bus", "human": "alice",
                      "token": "refresh-secret-1"}


# --- under corral: the relay holds the login (RT-6.18, RT-6.34, #154) -----------------

def test_under_corral_the_store_lies_in_the_sandboxs_home(tmp_path):
    env = {"CORRAL_SANDBOX": "1", "HOME": str(tmp_path / "sandbox-home")}
    assert keystore.store_directory(env).is_relative_to(tmp_path / "sandbox-home")


def test_the_relays_login_uses_the_default_store_and_the_offer_carries_no_secret():
    settings = login.Settings(ISSUER, "agent-bus", "host")
    offer = login_relay.offer(settings, "alice")
    assert offer == {"issuer": ISSUER, "client_id": "agent-bus", "nkey": "host", "human": "alice"}
    holder = login_relay.Holder(("127.0.0.1", 1), "k" * 64, *login_relay.settings_of(offer))
    assert holder.login._store.resolve() is keystore.default()  # keychain or the sandbox-home file
    assert holder.login._nkey_store is holder.login._store      # MM_LOGIN_NKEY=host keeps the key there
    assert not holder.login._migrate                            # the host's old store is out of reach


@pytest.mark.parametrize("value", [
    None, {}, {"issuer": "http://kc.example.test/r", "client_id": "c", "nkey": "host", "human": "a"},
    {"issuer": ISSUER, "client_id": "c", "nkey": "cloud", "human": "a"},
    {"issuer": ISSUER, "client_id": "c", "nkey": "host", "human": ""}])
def test_the_relay_logs_in_only_with_a_usable_offer(value):
    assert login_relay.settings_of(value) is None
    assert login_relay.hold(("127.0.0.1", 1), "k", value) is None


async def _host(monkeypatch, relayed):
    """The keyless session's host process, its handshake and login channel."""
    monkeypatch.setattr(login, "_current", relayed)
    host = hosted.Host("k" * 64)
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1")
    port = listener.extra(SocketAttribute.local_port)
    task = asyncio.create_task(listener.serve(host.handle))
    return port, task, listener


async def test_the_relay_holds_the_login_and_the_host_gets_tokens_and_the_key(monkeypatch, caplog):
    dm = Note()
    relayed = login_relay.RelayedLogin(login.Settings(ISSUER, "agent-bus", "host"), notifier=dm,
                                       human=lambda: "alice")
    port, serving, listener = await _host(monkeypatch, relayed)
    sent: list[bytes] = []
    real = login_relay._connect

    async def spy(host, p):
        reader, writer = await real(host, p)
        write = writer.write
        writer.write = lambda data: (sent.append(bytes(data)), write(data))[1]  # type: ignore[method-assign]
        return reader, writer

    show, opener, err = shown(False)
    store = keystore.Ephemeral()
    holder = login_relay.Holder(("127.0.0.1", port), "k" * 64, login.Settings(ISSUER, "agent-bus", "host"),
                                "alice", store=store, shown=show, connect=spy)
    holder.login._http = httpx.AsyncClient(transport=httpx.MockTransport(Device()))
    holder.login._sleep = lambda s: asyncio.sleep(0)
    running = asyncio.create_task(holder.run())
    try:
        with caplog.at_level(logging.DEBUG):
            token = await asyncio.wait_for(relayed.access_token(), 5)
            for _ in range(50):
                if "settled:approved" in dm.events:
                    break
                await asyncio.sleep(0.02)
        assert login.payload_of(token)["preferred_username"] == "alice"
        # the sandbox keeps the refresh token; it never crosses the channel
        assert stored(store) == "refresh-secret-1"
        wire = b"".join(sent).decode()
        assert "refresh-secret" not in wire and DEVICE not in wire
        # the relay shows the approval itself; the host DMs a link it built for the same code
        assert LINK in err.getvalue()
        assert dm.events[:1] == ["waiting"] and dm.grants[0].verification_uri_complete == f"{ISSUER}/device?user_code=ABCD-EFGH"
        assert "settled:approved" in dm.events
        # the host signs with the sandbox's key
        key = relayed.nkey()
        assert key.public_key == holder.login.nkey().public_key
        _verify(key.public_key, b"nonce", key.sign(b"nonce"))
        assert DEVICE not in caplog.text and "refresh-secret" not in caplog.text
        assert "SU" not in repr(relayed)
        # a second login channel is refused while one is connected
        assert relayed.busy()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(json.dumps({"key": "k" * 64, "login": True}).encode() + b"\n")
        assert json.loads(await r.readline())["ok"] is False
        w.close()
    finally:
        running.cancel()
        serving.cancel()
        await asyncio.gather(running, serving, return_exceptions=True)
        await listener.aclose()


def _relayed(dm=None, clock=None, **kw):
    now = (lambda: clock[0]) if clock else time.monotonic
    return login_relay.RelayedLogin(login.Settings(ISSUER, "agent-bus"), notifier=dm,
                                    human=lambda: "alice", now=now, **kw)


@pytest.mark.parametrize("event", [
    {"uri": "https://kc.example.test/realms/bus/x\n\n[click](https://evil.example)", "code": "ABCD-EFGH"},
    {"code": "ABCD-EFGH\n[click](https://evil.example)"},
    {"code": "AB CD"}, {"code": "abcd-efgh"}, {"code": ""}, {"code": 1234}])
async def test_the_host_dms_only_its_own_link_with_a_strict_user_code(event):
    """RT-6.34, #154: the agent holds the session key and can open a login channel;
    nothing it writes but a plain user code reaches the human's DM."""
    dm = Note()
    relayed = _relayed(dm)
    await relayed._on({"event": "waiting", "complete": event.get("uri", ""), **event})
    await asyncio.sleep(0)
    if "\n" in str(event["code"]) or event["code"] != "ABCD-EFGH":
        assert dm.grants == []
    else:  # a link of the sandbox's is ignored: the host builds its own
        (grant,) = dm.grants
        assert grant.verification_uri == f"{ISSUER}/device"
        assert grant.verification_uri_complete == f"{ISSUER}/device?user_code=ABCD-EFGH"


async def test_the_relayed_grant_serves_the_terminal_and_ends_with_its_approval():
    """RT-6.34 step 4: the sandbox's terminal shows the host-built link once, and a
    finished approval leaves no link behind."""
    dm, clock = Note(), [1000.0]
    relayed = _relayed(dm, clock=clock)
    assert relayed.pending_line() is None
    await relayed._on({"event": "waiting", "code": "ABCD-EFGH", "expires_in": 600})
    await asyncio.sleep(0)
    link, code, expires = relayed.pending_line()
    assert link == f"{ISSUER}/device?user_code=ABCD-EFGH" and code == "ABCD-EFGH" and expires == 600
    clock[0] += 540  # the seconds a harness shows are the time left, not the span
    assert relayed.pending_line()[2] == 60
    clock[0] += 60
    assert relayed.pending_line() is None
    await relayed._on({"event": "waiting", "code": "WXYZ-1234", "expires_in": 600})
    relayed.shown()
    assert relayed.pending_line() is None
    await relayed._on({"event": "waiting", "code": "WXYZ-1234"})
    assert relayed.pending_line() is not None
    await relayed._on({"event": "settled", "outcome": "approved"})
    await asyncio.sleep(0)
    assert relayed.pending() is None
    # A host without a notifier (no chat) still serves the line and drops it (#198).
    quiet = _relayed()
    await quiet._on({"event": "waiting", "code": "WXYZ-1234"})
    assert quiet.pending_line() is not None
    await quiet._on({"event": "settled", "outcome": "denied"})
    assert quiet.pending() is None


async def test_one_open_approval_at_a_time_and_one_per_cooldown_and_refusals_rate_limited():
    dm, clock = Note(), [1000.0]
    relayed = _relayed(dm, clock)
    waiting = {"event": "waiting", "code": "ABCD-EFGH", "expires_in": 600}
    for _ in range(5):
        await relayed._on(dict(waiting))
    await asyncio.sleep(0)
    assert dm.events == ["waiting"]                    # one DM, one ask, not five
    await relayed._on({"event": "settled", "outcome": "expired"})
    await relayed._on({"event": "settled", "outcome": "expired"})   # nothing open: ignored
    await relayed._on(dict(waiting))                   # within the cooldown: deferred, not now
    clock[0] += login.COOLDOWN_SEC
    await relayed._on(dict(waiting))
    for _ in range(3):
        await relayed._on({"event": "refused"})
    clock[0] += login.COOLDOWN_SEC
    await relayed._on({"event": "refused"})
    await asyncio.sleep(0)
    assert dm.events == ["waiting", "settled:expired", "waiting", "refused", "refused"]


async def test_a_new_approval_settles_the_earlier_ask_in_the_home(tmp_path, monkeypatch):
    from fakes import FakeChat

    from mattermost_ai_agent_bus import asks
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    chat = FakeChat(people={"u1": "alice"}, post_id="ask1")
    note = login_notice.DMNotifier(lambda: asyncio.sleep(0, chat), lambda: "alice")
    grant = login.DeviceGrant(f"{ISSUER}/device", "", "ABCD-EFGH", 600)
    await note.waiting(grant)
    assert asks.is_open(chat.me.id, "ask1")
    chat.post_id = "ask2"
    await note.waiting(grant)
    assert not asks.is_open(chat.me.id, "ask1") and asks.is_open(chat.me.id, "ask2")


async def _channel(relayed, reply):
    lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def send_line(data: bytes) -> None:
        msg = json.loads(data)
        await lines.put(json.dumps({"id": msg["id"], **reply}).encode())

    claim = relayed.claim()
    return asyncio.create_task(relayed.serve(claim, lines.get, send_line))


@pytest.mark.parametrize("reply, error", [
    ({"token": jwt("mallory")}, login.WrongHuman),
    ({"error": "the approval was declined", "kind": "denied"}, login.ApprovalDenied),
    ({"error": "the approving person is not the responsible human", "kind": "wrong_human"}, login.WrongHuman),
    ({"error": "MM_LOGIN_ISSUER must be https", "kind": "config"}, login.ConfigError),
    ({"error": "Keycloak not reachable", "kind": "login"}, login.LoginError),
])
async def test_the_host_keeps_the_kind_of_a_final_error_so_the_port_stops(reply, error):
    from mattermost_ai_agent_bus import nats_port
    relayed = _relayed()
    serving = await _channel(relayed, reply)
    try:
        with pytest.raises(error) as exc:
            await asyncio.wait_for(relayed.access_token(), 2)
        assert type(exc.value) is error
        assert nats_port._final(exc.value) is (error is not login.LoginError)
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)


async def test_the_relay_sends_the_kind_of_its_login_error():
    sent: list[dict] = []

    class Writer:
        def write(self, data):
            sent.append(json.loads(data))

        async def drain(self):
            pass

    holder = login_relay.Holder(("127.0.0.1", 1), "k", login.Settings(ISSUER, "agent-bus"), "alice",
                                store=keystore.Ephemeral())

    async def denied():
        raise login.ApprovalDenied("the approval was declined")

    holder.login.access_token = denied  # type: ignore[method-assign]
    await holder._answer(Writer(), 7)  # type: ignore[arg-type]
    assert sent == [{"id": 7, "error": "the approval was declined", "kind": "denied"}]


async def test_without_a_login_channel_the_host_warns_and_a_token_request_fails(caplog):
    relayed = _relayed(Note(), channel_wait_sec=0.05)
    with caplog.at_level(logging.WARNING):
        await relayed.run()
    assert "no login channel from the relay" in caplog.text
    with pytest.raises(login.LoginError, match="no login channel"):
        await relayed.access_token()


async def test_two_login_channels_at_once_get_one_slot(monkeypatch):
    relayed = _relayed(Note())
    port, serving, listener = await _host(monkeypatch, relayed)
    try:
        async def handshake():
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(json.dumps({"key": "k" * 64, "login": True}).encode() + b"\n")
            return json.loads(await r.readline())["ok"], w

        results = await asyncio.gather(handshake(), handshake(), handshake())
        assert sorted(ok for ok, _ in results) == [False, False, True]
        for ok, w in results:
            if ok:
                w.close()
        for _ in range(50):
            if not relayed.busy():
                break
            await asyncio.sleep(0.02)
        assert not relayed.busy()                          # the closed one freed its slot
        ok, w = await handshake()
        assert ok
        w.close()
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        await listener.aclose()


async def test_outside_corral_the_old_host_store_moves_once_before_the_lock(tmp_path, monkeypatch):
    calls = []

    def migrate(old, new, names):
        calls.append((old.directory, new, list(names)))
        return []

    monkeypatch.setattr(keystore, "migrate_from", migrate)
    store = keystore.Ephemeral()
    lg = mk(store, transport=httpx.MockTransport(Script()), notifier=Note(), migrate=True)
    await lg.access_token()
    lg._tokens = login._Tokens()
    await lg.access_token()
    ((old, new, names),) = calls                       # once per process
    assert old == tmp_path / "state" / "mm-agent-bus" / "login" and new is store
    assert names == [keystore.refresh_entry(ISSUER, "agent-bus", "alice"),
                     keystore.cooldown_entry(ISSUER, "agent-bus", "alice"), keystore.NKEY_SEED]


async def test_the_migration_carries_the_old_token_over(tmp_path):
    old = keystore.HostFile(tmp_path / "state" / "mm-agent-bus" / "login")
    put(old, "refresh-secret-old")
    store = keystore.HostFile(tmp_path / "new")
    script = Script()
    await mk(store, transport=httpx.MockTransport(script), notifier=Note(), migrate=True).access_token()
    assert script.refreshed == ["refresh-secret-old"] and script.device_requests == 0
    assert stored(old) is None and stored(store) == "refresh-secret-1"


async def test_the_store_is_chosen_off_the_event_loop(monkeypatch):
    import threading
    where = []

    def choose():
        where.append(threading.current_thread() is threading.main_thread())
        return keystore.Ephemeral()

    monkeypatch.setattr(keystore, "default", choose)
    lg = login.Login(login.Settings(ISSUER, "agent-bus"), store=login.default_store(), human=lambda: "alice",
                     http=httpx.AsyncClient(transport=httpx.MockTransport(Script())), notifier=Note(),
                     sleep=lambda s: asyncio.sleep(0))
    assert where == []                                 # nothing chosen at construction
    await lg.access_token()
    assert where == [False]


async def test_the_relays_handshake_answer_offers_the_login_only_with_a_relayed_login(monkeypatch):
    assert hosted._login_offer() == {}
    relayed = login_relay.RelayedLogin(login.Settings(ISSUER, "agent-bus"), human=lambda: "alice")
    monkeypatch.setattr(login, "_current", relayed)
    assert hosted._login_offer() == {"login": {"issuer": ISSUER, "client_id": "agent-bus",
                                               "nkey": "ephemeral", "human": "alice"}}


# --- review fixes: the store kept, approvals that end with their relay -------------------

async def test_the_migration_leaves_a_chosen_file_store_at_the_old_place_alone(tmp_path, monkeypatch):
    """RT-6.34, #154: with keystore.default() a HostFile at <state dir>/login (no keychain),
    the real migrate_from must see the store itself, not its Deferred wrapper, or it
    would move the entries onto themselves and delete them."""
    place = tmp_path / "state" / "mm-agent-bus" / "login"
    monkeypatch.setattr(keystore, "_chosen", keystore.HostFile(place))
    put(keystore.HostFile(place), "refresh-secret-0")
    # a seed in the same place, as MM_LOGIN_NKEY=host keeps it
    kept = login.Login(login.Settings(ISSUER, "agent-bus", "host"), store=keystore.Ephemeral()).export_seed()
    keystore.HostFile(place).save(keystore.NKEY_SEED, kept)
    script = Script()
    lg = login.Login(login.Settings(ISSUER, "agent-bus", "host"), store=login.default_store(),
                     human=lambda: "alice", notifier=Note(), migrate=True,
                     http=httpx.AsyncClient(transport=httpx.MockTransport(script)),
                     sleep=lambda s: asyncio.sleep(0))
    for _ in range(2):  # every start
        lg._migrate, lg._tokens = True, login._Tokens()
        await lg.access_token()
    files = keystore.HostFile(place)
    assert stored(files) == "refresh-secret-2" and files.load(keystore.NKEY_SEED) == kept
    assert script.device_requests == 0


async def test_a_stored_seed_moves_before_the_key_is_made(tmp_path):
    old = keystore.HostFile(tmp_path / "state" / "mm-agent-bus" / "login")
    seed = login.Login(login.Settings(ISSUER, "agent-bus", "host"), store=keystore.Ephemeral()).export_seed()
    old.save(keystore.NKEY_SEED, seed)
    store = keystore.HostFile(tmp_path / "new")
    lg = login.Login(login.Settings(ISSUER, "agent-bus", "host"), store=store, human=lambda: "alice",
                     notifier=Note(), migrate=True, sleep=lambda s: asyncio.sleep(0),
                     http=httpx.AsyncClient(transport=httpx.MockTransport(Script())))
    await lg.access_token()
    assert store.load(keystore.NKEY_SEED) == seed and lg.export_seed() == seed
    assert old.load(keystore.NKEY_SEED) is None


def test_two_threads_asking_for_the_key_get_one():
    import threading
    lg = login.Login(login.Settings(ISSUER, "agent-bus", "host"), store=keystore.Ephemeral())
    keys: list[str] = []
    threads = [threading.Thread(target=lambda: keys.append(lg.nkey().public_key)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(keys)) == 1 and lg.export_seed()


async def test_a_relay_that_dies_mid_approval_settles_its_ask_and_the_next_gets_a_dm():
    dm, clock = Note(), [1000.0]
    relayed = _relayed(dm, clock)
    lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def send_line(data: bytes) -> None:
        pass

    serving = asyncio.create_task(relayed.serve(relayed.claim(), lines.get, send_line))
    await asyncio.sleep(0)
    await lines.put(json.dumps({"event": "waiting", "code": "ABCD-EFGH", "expires_in": 600}).encode())
    await asyncio.sleep(0.01)
    await lines.put(b"")                               # the relay is gone
    await asyncio.gather(serving, return_exceptions=True)
    await asyncio.sleep(0)
    assert dm.events == ["waiting", "settled:failed"] and not relayed.busy()
    clock[0] += login.COOLDOWN_SEC
    await relayed._on({"event": "waiting", "code": "WXYZ-QRST", "expires_in": 600})
    await asyncio.sleep(0)
    assert dm.events[-1] == "waiting" and dm.grants[-1].user_code == "WXYZ-QRST"


async def test_a_restart_within_the_cooldown_gets_its_dm_when_it_ends():
    dm, clock, waited = Note(), [1000.0], []

    async def sleep(sec):
        waited.append(sec)
        clock[0] += sec

    relayed = _relayed(dm, clock, sleep=sleep)
    await relayed._on({"event": "waiting", "code": "ABCD-EFGH", "expires_in": 600})
    await relayed._on({"event": "settled", "outcome": "failed"})   # the relay restarted
    clock[0] += 10
    await relayed._on({"event": "waiting", "code": "IJKL-MNOP", "expires_in": 600})
    await relayed._on({"event": "waiting", "code": "WXYZ-QRST", "expires_in": 600})  # the newest wins
    for _ in range(5):
        await asyncio.sleep(0)
    assert waited == [login.COOLDOWN_SEC - 10]            # the floor stays
    assert dm.events == ["waiting", "settled:failed", "waiting"]
    assert dm.grants[-1].user_code == "WXYZ-QRST" and dm.grants[-1].expires_in == 550


async def test_an_open_approval_ends_at_its_expiry():
    dm, clock = Note(), [1000.0]
    relayed = _relayed(dm, clock)
    await relayed._on({"event": "waiting", "code": "ABCD-EFGH", "expires_in": 120})
    clock[0] += 120
    await relayed._on({"event": "waiting", "code": "WXYZ-QRST", "expires_in": 120})
    for _ in range(3):
        await asyncio.sleep(0)
    assert dm.events == ["waiting", "settled:expired", "waiting"]


def test_the_host_moves_the_old_host_store_to_its_own_but_never_onto_itself(tmp_path, monkeypatch):
    settings = login.Settings(ISSUER, "agent-bus", "host")
    old = keystore.HostFile(tmp_path / "state" / "mm-agent-bus" / "login")
    put(old, "refresh-secret-old")
    old.save(keystore.NKEY_SEED, "SUold")
    monkeypatch.setattr(keystore, "_chosen", old)          # the host's own store is that place
    assert login_relay.clear_old_host_store(settings, "alice") == []
    assert stored(old) == "refresh-secret-old"
    mine = keystore.HostFile(tmp_path / "keychain-stand-in")
    monkeypatch.setattr(keystore, "_chosen", mine)
    held = mine.try_lock(keystore.NKEY_SEED)               # one entry in use: left for later
    moved = login_relay.clear_old_host_store(settings, "alice")
    mine.unlock(held)
    assert keystore.refresh_entry(ISSUER, "agent-bus", "alice") in moved
    assert stored(old) is None and stored(mine) == "refresh-secret-old"   # kept, not lost
    assert old.load(keystore.NKEY_SEED) == "SUold" and mine.load(keystore.NKEY_SEED) is None
    assert keystore.NKEY_SEED in login_relay.clear_old_host_store(settings, "alice")
    assert old.load(keystore.NKEY_SEED) is None and mine.load(keystore.NKEY_SEED) == "SUold"


async def test_warnings_the_sandbox_can_repeat_are_rate_limited(caplog):
    dm, clock = Note(), [1000.0]
    relayed = _relayed(dm, clock)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            await relayed._on({"event": "waiting", "code": "bad code"})
        await relayed._on({"event": "waiting", "code": "ABCD-EFGH", "expires_in": 600})
        for _ in range(5):
            await relayed._on({"event": "waiting", "code": "WXYZ-QRST", "expires_in": 600})
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned.count("login: the sandbox's user code is not one; no DM") == 1
    assert warned.count("login: an approval is already open; no second DM") == 1
    clock[0] += login.COOLDOWN_SEC
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await relayed._on({"event": "waiting", "code": "bad code"})
    assert "user code is not one" in caplog.text           # again after the cooldown
