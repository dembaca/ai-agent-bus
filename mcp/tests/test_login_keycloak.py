"""The device flow against a real Keycloak (RT-6.34): no fake of the flow. The test plays the
human by driving Keycloak's own HTML forms. Needs docker; skipped without it.

Image pinned exactly, realm from tests/keycloak/realm.json (public client `agent-bus`, device
grant only, consent required, audience and group mappers; users alice and mallory; device code
lifespan 10 s). Run: `uv run --directory mcp pytest -q -m keycloak`.
"""

import asyncio
import os
import html
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest

from mattermost_ai_agent_bus import keystore, login

pytestmark = pytest.mark.keycloak

IMAGE = "quay.io/keycloak/keycloak:26.4.2"
REALM = Path(__file__).parent / "keycloak" / "realm.json"
PASSWORDS = {"alice": "alice-test-pw", "mallory": "mallory-test-pw"}
CLIENT = "agent-bus"


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(scope="module")
def issuer() -> Iterator[str]:
    # Only in their own workflow (keycloak.yml): a runner with docker would
    # otherwise start Keycloak in every test job too.
    if os.environ.get("MM_TEST_KEYCLOAK") != "1":
        pytest.skip("MM_TEST_KEYCLOAK=1 is not set (keycloak.yml sets it)")
    if not _docker_ok():
        pytest.skip("docker is not available")
    name = f"mm-login-kc-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::8080",
         "-e", "KC_BOOTSTRAP_ADMIN_USERNAME=admin", "-e", "KC_BOOTSTRAP_ADMIN_PASSWORD=admin-test-only",
         "-v", f"{REALM}:/opt/keycloak/data/import/realm.json:ro",
         IMAGE, "start-dev", "--import-realm"],
        capture_output=True, text=True, timeout=600)
    if run.returncode != 0:
        pytest.skip(f"keycloak container did not start: {run.stderr.strip()[:200]}")
    try:
        mapping = subprocess.run(["docker", "port", name, "8080/tcp"], capture_output=True, text=True,
                                 check=True).stdout.split()[0]
        url = f"http://127.0.0.1:{mapping.rsplit(':', 1)[1]}/realms/bus"
        deadline = time.time() + 240
        while True:
            try:
                if httpx.get(f"{url}/.well-known/openid-configuration", timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                logs = subprocess.run(["docker", "logs", "--tail", "30", name], capture_output=True, text=True)
                pytest.fail(f"keycloak did not come up:\n{logs.stdout[-1500:]}{logs.stderr[-1500:]}")
            time.sleep(2)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


class _Form(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.forms.append({"action": html.unescape(a.get("action") or ""), "fields": {}, "buttons": {}})
        elif tag in ("input", "button") and self.forms and a.get("name"):
            kind = (a.get("type") or "text").lower()
            bucket = "buttons" if kind in ("submit", "button") or tag == "button" else "fields"
            self.forms[-1][bucket][a["name"]] = a.get("value") or ""


async def play_human(link: str, user: str, *, approve: bool = True) -> None:
    """Open the approval link, log in and answer the consent page, as a person in a browser."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as browser:
        page = await browser.get(link)
        for _ in range(8):
            parser = _Form()
            parser.feed(page.text)
            if not parser.forms:
                return
            form = parser.forms[0]
            data = dict(form["fields"])
            if "username" in data or "password" in data:
                data["username"], data["password"] = user, PASSWORDS[user]
            elif form["buttons"]:
                want = "accept" if approve else "cancel"
                assert want in form["buttons"], f"no {want} button: {sorted(form['buttons'])}"
                data[want] = form["buttons"][want] or ("Yes" if approve else "No")
            page = await browser.post(str(page.url.join(form["action"])), data=data)
        raise AssertionError("Keycloak's forms did not end")


class Human:
    """A notifier whose human opens the link at once, in the background."""

    def __init__(self, user: str, *, approve: bool = True, answer: bool = True) -> None:
        self.user, self.approve, self.answer = user, approve, answer
        self.events: list[str] = []
        self.links: list[str] = []
        self._tasks: list[asyncio.Task] = []

    async def waiting(self, grant: login.DeviceGrant) -> None:
        self.events.append("waiting")
        self.links.append(grant.verification_uri_complete)
        if self.answer:
            task = asyncio.create_task(play_human(grant.verification_uri_complete, self.user, approve=self.approve))
            # A failure in the forms shows in the log at once, not only as an
            # expired code later.
            task.add_done_callback(lambda t: t.cancelled() or t.exception() is None
                                   or print(f"play_human failed: {t.exception()!r}", flush=True))
            self._tasks.append(task)

    async def settled(self, outcome: str) -> None:
        self.events.append(f"settled:{outcome}")
        for t in self._tasks:
            await t

    async def refused(self) -> None:
        self.events.append("refused")


def make(issuer: str, store: keystore.KeyStore, human: str, who: Human, **kw) -> login.Login:
    return login.Login(login.Settings(issuer, CLIENT, kw.pop("nkey", "ephemeral")), store=store,
                       human=lambda: human, notifier=who, **kw)


async def test_first_approval_delivers_a_token_for_the_client(issuer):
    store, who = keystore.Ephemeral(), Human("alice")
    token = await make(issuer, store, "alice", who).access_token()
    claims = login.payload_of(token)
    assert claims["preferred_username"] == "alice"
    assert CLIENT in (claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]])
    assert claims["azp"] == CLIENT and claims["groups"] == ["approvers"]
    assert who.events == ["waiting", "settled:approved"]
    assert store.load(keystore.REFRESH_TOKEN)


class Recorder(httpx.AsyncHTTPTransport):
    """The real transport, noting every refresh token the host sends."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        m = re.search(rb"(?:^|&)refresh_token=([^&]+)", request.content)
        if m:
            self.sent.append(m.group(1).decode())
        return await super().handle_async_request(request)


async def test_refresh_rotates_and_the_old_token_is_never_used_again(issuer):
    store, who, clock, rec = keystore.Ephemeral(), Human("alice"), [time.time()], Recorder()
    lg = make(issuer, store, "alice", who, now=lambda: clock[0], http=httpx.AsyncClient(transport=rec))
    first = await lg.access_token()
    r1 = store.load(keystore.REFRESH_TOKEN)
    clock[0] += 290  # 10 s left in the host's view: a refresh
    second = await lg.access_token()
    r2 = store.load(keystore.REFRESH_TOKEN)
    assert second != first and r1 and r2 and r1 != r2
    assert who.events == ["waiting", "settled:approved"]  # no second approval
    clock[0] += 290
    await lg.access_token()
    r3 = store.load(keystore.REFRESH_TOKEN)
    assert r3 and r3 not in (r1, r2)
    assert rec.sent == [r1, r2]  # each one sent once, never again once replaced


async def test_a_stored_refresh_token_resumes_without_a_new_approval(issuer, tmp_path):
    files = keystore.HostFile(tmp_path / "login")
    await make(issuer, files, "alice", Human("alice")).access_token()
    again = Human("alice", answer=False)
    token = await make(issuer, files, "alice", again).access_token()
    assert login.payload_of(token)["preferred_username"] == "alice"
    assert again.events == []


async def test_a_revoked_refresh_token_starts_a_new_approval(issuer):
    store = keystore.Ephemeral()
    store.save(keystore.REFRESH_TOKEN, "not-a-token-keycloak-knows")
    who = Human("alice")
    await make(issuer, store, "alice", who).access_token()
    assert who.events == ["waiting", "settled:approved"]


async def test_a_denied_approval(issuer):
    store, who = keystore.Ephemeral(), Human("alice", approve=False)
    with pytest.raises(login.ApprovalDenied):
        await make(issuer, store, "alice", who).access_token()
    assert who.events == ["waiting", "settled:denied"]
    assert store.load(keystore.REFRESH_TOKEN) is None


async def test_an_expired_code(issuer):
    store, who = keystore.Ephemeral(), Human("alice", answer=False)
    short = login.Login(login.Settings(issuer, "agent-bus-short", "ephemeral"), store=store,
                        human=lambda: "alice", notifier=who)
    with pytest.raises(login.ApprovalExpired):
        await short.access_token()  # this client's device codes live 5 s
    assert who.events == ["waiting", "settled:expired"]
    assert store.load(keystore.REFRESH_TOKEN) is None


async def test_approval_by_another_user_is_refused_and_the_tokens_are_dropped(issuer, caplog):
    store, who = keystore.Ephemeral(), Human("mallory")
    lg = make(issuer, store, "alice", who)
    with pytest.raises(login.WrongHuman):
        await lg.access_token()
    assert who.events == ["waiting", "refused", "settled:refused"]
    assert store.load(keystore.REFRESH_TOKEN) is None
    assert re.search(r"eyJ", caplog.text) is None


@pytest.mark.parametrize("nkey", ["ephemeral", "host"])
async def test_both_nkey_stores_with_a_real_login(issuer, tmp_path, nkey):
    files = keystore.HostFile(tmp_path / "login")
    lg = make(issuer, files, "alice", Human("alice"), nkey=nkey)
    await lg.access_token()
    public = lg.nkey().public_key
    again = make(issuer, files, "alice", Human("alice", answer=False), nkey=nkey)
    assert (again.nkey().public_key == public) is (nkey == "host")
    assert (files.load(keystore.NKEY_SEED) is not None) is (nkey == "host")
    assert files.load(keystore.REFRESH_TOKEN)  # the refresh token persists either way
