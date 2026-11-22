"""The host's Keycloak login, without containers (RT-6.34): settings, the person check,
the refresh token used once, the nkey, the DM-only notice. The device flow against a
real Keycloak is in test_login_keycloak.py."""

import asyncio
import base64
import json
import logging

import httpx
import nkeys
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fakes import FakeChat

from mattermost_ai_agent_bus import asks, keystore, login, login_notice

ISSUER = "https://kc.example.test/realms/bus"
SECRETS = ("access-secret", "refresh-secret")


def jwt(user: str, **extra) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg({'preferred_username': user, **extra})}.sig"


# --- settings -------------------------------------------------------------------

def test_both_unset_the_login_is_off_and_silent(monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG):
        assert login.settings_from_env() is None
    assert caplog.records == []


def test_one_without_the_other_is_a_logged_configuration_error(monkeypatch, caplog):
    for name in ("MM_LOGIN_ISSUER", "MM_LOGIN_CLIENT_ID"):
        monkeypatch.delenv("MM_LOGIN_ISSUER", raising=False)
        monkeypatch.delenv("MM_LOGIN_CLIENT_ID", raising=False)
        monkeypatch.setenv(name, ISSUER if name == "MM_LOGIN_ISSUER" else "agent-bus")
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            assert login.settings_from_env() is None
        assert "must both be set" in caplog.text


@pytest.mark.parametrize("issuer", [
    "http://kc.example.test/realms/bus", "ftp://localhost/x", "https://", "kc.example.test",
    "https://user:pw@kc.example.test/realms/bus"])
def test_a_wrong_issuer_turns_the_login_off(monkeypatch, caplog, issuer):
    monkeypatch.setenv("MM_LOGIN_ISSUER", issuer)
    monkeypatch.setenv("MM_LOGIN_CLIENT_ID", "agent-bus")
    with caplog.at_level(logging.ERROR):
        assert login.settings_from_env() is None
    assert "login is off" in caplog.text
    assert "pw@" not in caplog.text


@pytest.mark.parametrize("issuer", [
    "https://kc.example.test/realms/bus/", "http://localhost:8080/realms/bus",
    "http://127.0.0.1:8080/realms/bus", "http://[::1]:8080/realms/bus"])
def test_https_or_http_on_loopback_is_accepted(monkeypatch, issuer):
    monkeypatch.setenv("MM_LOGIN_ISSUER", issuer)
    monkeypatch.setenv("MM_LOGIN_CLIENT_ID", "agent-bus")
    s = login.settings_from_env()
    assert s is not None
    assert s.issuer == issuer.rstrip("/")
    assert (s.client_id, s.nkey_store) == ("agent-bus", "ephemeral")


def test_nkey_store_setting(monkeypatch, caplog):
    monkeypatch.setenv("MM_LOGIN_ISSUER", ISSUER)
    monkeypatch.setenv("MM_LOGIN_CLIENT_ID", "agent-bus")
    monkeypatch.setenv("MM_LOGIN_NKEY", "HOST")
    s = login.settings_from_env()
    assert s is not None and s.nkey_store == "host"
    monkeypatch.setenv("MM_LOGIN_NKEY", "cloud")
    with caplog.at_level(logging.ERROR):
        assert login.settings_from_env() is None
    assert "MM_LOGIN_NKEY" in caplog.text


async def test_start_without_settings_runs_nothing():
    assert login.start(lambda: asyncio.sleep(0, None)) is None
    assert login.current() is None


# --- the nkey -------------------------------------------------------------------

def _verify(public: str, nonce: bytes, sig: bytes) -> None:
    raw = base64.b32decode(public)[1:33]
    Ed25519PublicKey.from_public_bytes(raw).verify(sig, nonce)


def mk(store=None, nkey_store=None, human="alice", transport=None, notifier=None, **kw):
    http = httpx.AsyncClient(transport=transport) if transport else None
    return login.Login(login.Settings(ISSUER, "agent-bus"), store=store or keystore.Ephemeral(),
                       nkey_store=nkey_store, human=lambda: human, http=http, notifier=notifier,
                       sleep=lambda s: asyncio.sleep(0), **kw)


def test_the_nkey_is_a_user_key_whose_signature_checks():
    key = mk().nkey()
    assert key.public_key.startswith("U")
    sig = key.sign(b"nonce-from-the-server")
    _verify(key.public_key, b"nonce-from-the-server", sig)
    with pytest.raises(InvalidSignature):
        _verify(key.public_key, b"another nonce", sig)


def test_the_ephemeral_nkey_is_new_per_process_and_the_host_one_is_kept(tmp_path):
    assert mk().nkey().public_key != mk().nkey().public_key
    store = keystore.HostFile(tmp_path / "login")
    first = mk(nkey_store=store).nkey().public_key
    assert mk(nkey_store=store).nkey().public_key == first
    # the seed is stored whole, so messages can be signed later
    seed = store.load(keystore.NKEY_SEED)
    assert seed and seed.startswith("SU")
    assert nkeys.from_seed(seed.encode()).public_key.decode() == first


def test_the_nkey_setting_picks_the_store(tmp_path):
    shared = keystore.Ephemeral()
    host = login.Login(login.Settings(ISSUER, "agent-bus", "host"), store=shared)
    host.nkey()
    assert shared.load(keystore.NKEY_SEED)
    eph = login.Login(login.Settings(ISSUER, "agent-bus", "ephemeral"), store=shared)
    assert eph.nkey().public_key != host.nkey().public_key
    assert eph.nkey().public_key == eph.nkey().public_key


def test_a_stored_seed_that_is_refused_is_replaced(tmp_path, caplog):
    d = tmp_path / "login"
    store = keystore.HostFile(d)
    store.save(keystore.NKEY_SEED, "SUAnotvalid")
    with caplog.at_level(logging.WARNING):
        key = mk(nkey_store=store).nkey()
    assert key.public_key.startswith("U")
    assert "SUAnotvalid" not in caplog.text


# --- nothing secret in a repr, a log or an error --------------------------------

def test_repr_hides_secrets():
    lg = mk()
    seed = lg.nkey()
    lg._tokens = login._Tokens(access="access-secret", expires_at=1e12)
    assert "access-secret" not in repr(lg) + repr(lg._tokens) + repr(seed)
    seed_text = lg._nkey_store.load(keystore.NKEY_SEED)
    assert seed_text and seed_text not in repr(lg) + repr(seed) + repr(lg._nkey_store)


# --- the flow against a scripted token endpoint (the person check, rotation) ----

class Script:
    """Keycloak's three endpoints, scripted: enough for the unit paths."""

    def __init__(self, user="alice", device_errors=("authorization_pending",)):
        self.user = user
        self.errors = list(device_errors)
        self.refreshed: list[str] = []
        self.n = 0
        self.invalid_grant = False
        self.device_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "device_authorization_endpoint": f"{ISSUER}/device",
                "token_endpoint": f"{ISSUER}/token"})
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        if path.endswith("/device"):
            self.device_requests += 1
            assert form["scope"] == "openid+offline_access" and form["client_id"] == "agent-bus"
            return httpx.Response(200, json={
                "device_code": "dc", "user_code": "ABCD-EFGH", "expires_in": 600, "interval": 5,
                "verification_uri": f"{ISSUER}/device-page",
                "verification_uri_complete": f"{ISSUER}/device-page?user_code=ABCD-EFGH"})
        if form["grant_type"].endswith("device_code"):
            if self.errors:
                return httpx.Response(400, json={"error": self.errors.pop(0)})
            return self._tokens()
        assert form["grant_type"] == "refresh_token"
        if self.invalid_grant or form["refresh_token"] in self.refreshed:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.refreshed.append(form["refresh_token"])
        return self._tokens()

    def _tokens(self) -> httpx.Response:
        self.n += 1
        return httpx.Response(200, json={
            "access_token": jwt(self.user, n=self.n), "expires_in": 300,
            "refresh_token": f"refresh-secret-{self.n}"})


class Note:
    def __init__(self):
        self.events: list[str] = []
        self.grants: list[login.DeviceGrant] = []

    async def waiting(self, grant):
        self.events.append("waiting")
        self.grants.append(grant)

    async def settled(self, outcome):
        self.events.append(f"settled:{outcome}")

    async def refused(self):
        self.events.append("refused")


async def test_first_approval_stores_the_refresh_token_and_serves_the_token_until_60s_are_left():
    script, note, store, clock = Script(), Note(), keystore.Ephemeral(), [1000.0]
    lg = mk(store, transport=httpx.MockTransport(script), notifier=note, now=lambda: clock[0])
    first = await lg.access_token()
    assert login.payload_of(first)["preferred_username"] == "alice"
    assert note.events == ["waiting", "settled:approved"]
    assert store.load(keystore.REFRESH_TOKEN) == "refresh-secret-1"
    clock[0] += 200
    assert await lg.access_token() == first          # 100 s left: kept
    clock[0] += 50
    second = await lg.access_token()                 # 50 s left: refreshed
    assert second != first
    assert store.load(keystore.REFRESH_TOKEN) == "refresh-secret-2"
    assert script.refreshed == ["refresh-secret-1"]
    assert script.device_requests == 1


async def test_each_refresh_token_is_used_once():
    script, store, clock = Script(), keystore.Ephemeral(), [0.0]
    lg = mk(store, transport=httpx.MockTransport(script), notifier=Note(), now=lambda: clock[0])
    for _ in range(4):
        await lg.access_token()
        clock[0] += 290
    assert len(script.refreshed) == len(set(script.refreshed)) == 3


async def test_slow_down_adds_five_seconds_and_pending_waits():
    script = Script(device_errors=("authorization_pending", "slow_down", "authorization_pending"))
    waits: list[float] = []

    async def sleep(s):
        waits.append(s)

    lg = login.Login(login.Settings(ISSUER, "agent-bus"), store=keystore.Ephemeral(), human=lambda: "alice",
                     http=httpx.AsyncClient(transport=httpx.MockTransport(script)), notifier=Note(),
                     sleep=sleep)
    await lg.access_token()
    assert waits == [5, 5, 10, 10]


@pytest.mark.parametrize("error, exc, outcome", [
    ("access_denied", login.ApprovalDenied, "denied"),
    ("expired_token", login.ApprovalExpired, "expired")])
async def test_denied_and_expired_settle_the_ask_and_keep_nothing(error, exc, outcome):
    store, note = keystore.Ephemeral(), Note()
    lg = mk(store, transport=httpx.MockTransport(Script(device_errors=(error,))), notifier=note)
    with pytest.raises(exc):
        await lg.access_token()
    assert note.events == ["waiting", f"settled:{outcome}"]
    assert store.load(keystore.REFRESH_TOKEN) is None


async def test_another_person_is_refused_dropped_and_reported_without_token_content(caplog):
    store, note = keystore.Ephemeral(), Note()
    lg = mk(store, transport=httpx.MockTransport(Script(user="mallory")), notifier=note)
    with caplog.at_level(logging.DEBUG), pytest.raises(login.WrongHuman) as err:
        await lg.access_token()
    assert note.events == ["waiting", "refused", "settled:refused"]
    assert store.load(keystore.REFRESH_TOKEN) is None
    assert lg._tokens.access == ""
    leaked = caplog.text + str(err.value) + repr(err.value)
    assert not any(s in leaked for s in SECRETS) and "mallory" not in leaked


async def test_another_person_after_a_refresh_is_refused_too():
    script, store, clock = Script(), keystore.Ephemeral(), [0.0]
    lg = mk(store, transport=httpx.MockTransport(script), notifier=Note(), now=lambda: clock[0])
    await lg.access_token()
    script.user = "mallory"
    clock[0] += 290
    with pytest.raises(login.WrongHuman):
        await lg.access_token()
    assert store.load(keystore.REFRESH_TOKEN) is None and lg._tokens.access == ""


async def test_a_resumed_refresh_token_needs_no_approval():
    script, note = Script(), Note()
    store = keystore.Ephemeral()
    await mk(store, transport=httpx.MockTransport(script), notifier=note).access_token()
    note2 = Note()
    await mk(store, transport=httpx.MockTransport(script), notifier=note2).access_token()
    assert note2.events == [] and script.device_requests == 1


async def test_invalid_grant_drops_the_stored_token_and_starts_a_new_approval():
    script, note = Script(), Note()
    store = keystore.Ephemeral()
    store.save(keystore.REFRESH_TOKEN, "stale")
    script.invalid_grant = True
    lg = mk(store, transport=httpx.MockTransport(script), notifier=note)
    script.refreshed.append("stale")
    await lg.access_token()
    assert note.events == ["waiting", "settled:approved"]
    assert store.load(keystore.REFRESH_TOKEN) == "refresh-secret-1"


async def test_without_a_responsible_human_there_is_no_approval():
    note = Note()
    lg = mk(human="", transport=httpx.MockTransport(Script()), notifier=note)
    with pytest.raises(login.LoginError):
        await lg.access_token()
    assert note.events == []


async def test_a_link_that_cannot_be_delivered_ends_the_approval():
    class Down(Note):
        async def waiting(self, grant):
            raise RuntimeError(f"cannot reach chat {grant.verification_uri_complete}")

    lg = mk(transport=httpx.MockTransport(Script()), notifier=Down())
    with pytest.raises(login.NotifyError) as err:
        await lg.access_token()
    assert "user_code" not in str(err.value) and "ABCD" not in str(err.value)


async def test_run_never_raises_and_the_current_login_is_reachable(monkeypatch, caplog):
    lg = mk(transport=httpx.MockTransport(Script(user="mallory")), notifier=Note())
    with caplog.at_level(logging.WARNING):
        await lg.run()
    assert "login" in caplog.text


# --- the notice: the link in a DM only ----------------------------------------

@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


async def test_the_link_goes_to_the_human_by_dm_and_the_home_only_gets_the_ask(state):
    chat = FakeChat(people={"u1": "alice"}, post_id="ask1")
    note = login_notice.DMNotifier(lambda: asyncio.sleep(0, chat), lambda: "alice")
    grant = login.DeviceGrant("https://kc/device", "https://kc/device?user_code=ABCD-EFGH", "ABCD-EFGH", 600)
    await note.waiting(grant)
    dm, home = chat.posted
    assert dm["channel_id"] == "dmu1"
    assert "https://kc/device?user_code=ABCD-EFGH" in dm["message"] and "ABCD-EFGH" in dm["message"]
    assert home["channel_id"] is None
    assert "kc/device" not in home["message"] and "ABCD" not in home["message"]
    assert "@alice" in home["message"] and "direct messages" in home["message"]
    assert asks.is_open(chat.me.id, "ask1")
    assert [h for h in asks.waiting(chat.me.id)] and asks.waiting(chat.me.id)[0][2] == "alice"
    await note.settled("approved")
    assert not asks.is_open(chat.me.id, "ask1")


async def test_a_human_the_chat_does_not_know_gets_no_link_and_no_ask(state):
    chat = FakeChat(post_id="ask1")
    note = login_notice.DMNotifier(lambda: asyncio.sleep(0, chat), lambda: "alice")
    with pytest.raises(login.NotifyError):
        await note.waiting(login.DeviceGrant("u", "u", "c", 600))
    assert chat.posted == []


async def test_a_refusal_goes_to_the_dm_without_a_name_or_token(state):
    chat = FakeChat(people={"u1": "alice"})
    await login_notice.DMNotifier(lambda: asyncio.sleep(0, chat), lambda: "alice").refused()
    (dm,) = chat.posted
    assert dm["channel_id"] == "dmu1" and "not you" in dm["message"]
