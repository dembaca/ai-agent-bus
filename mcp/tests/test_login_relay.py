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
    assert holder.login._store is keystore.default()     # keychain or the sandbox-home file
    assert holder.login._nkey_store is keystore.default()  # the host key is kept too (decision 10)


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
        # the relay shows the approval itself; the host DMs the human the same link
        assert LINK in err.getvalue()
        assert dm.events[:1] == ["waiting"] and dm.grants[0].verification_uri_complete == LINK
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


async def test_the_host_dms_only_a_link_on_the_issuers_site_and_checks_the_person(monkeypatch):
    dm = Note()
    relayed = login_relay.RelayedLogin(login.Settings(ISSUER, "agent-bus"), notifier=dm,
                                       human=lambda: "alice")
    await relayed._on({"event": "waiting", "uri": "https://evil.example.test/x", "complete": "",
                       "code": "ABCD", "expires_in": 600})
    await relayed._on({"event": "waiting", "uri": f"{ISSUER}/device", "complete": "", "code": "AB CD"})
    await asyncio.sleep(0)
    assert dm.events == []
    lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def send_line(data: bytes) -> None:
        msg = json.loads(data)
        await lines.put(json.dumps({"id": msg["id"], "token": jwt("mallory")}).encode())

    serving = asyncio.create_task(relayed.serve(lines.get, send_line))
    try:
        with pytest.raises(login.LoginError, match="not the responsible human"):
            await asyncio.wait_for(relayed.access_token(), 2)
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)


async def test_the_relays_handshake_answer_offers_the_login_only_with_a_relayed_login(monkeypatch):
    assert hosted._login_offer() == {}
    relayed = login_relay.RelayedLogin(login.Settings(ISSUER, "agent-bus"), human=lambda: "alice")
    monkeypatch.setattr(login, "_current", relayed)
    assert hosted._login_offer() == {"login": {"issuer": ISSUER, "client_id": "agent-bus",
                                               "nkey": "host", "human": "alice"}}
