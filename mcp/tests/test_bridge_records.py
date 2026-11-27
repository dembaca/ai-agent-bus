"""The bridge's own mode, mandate and debug records (issue #151 step 3): the
bytes its Go test signs with a fixed seed and clock must pass the MCP checker
against the bridge key a host pins.

The fixtures are the contract: ``services/bridge/internal/commands`` compares
the exact strings against these files, and this test verifies them here.
"""

import base64
from pathlib import Path

from mattermost_ai_agent_bus import attest

# The Go test's fixed seed is bytes(range(32)); this is its Ed25519 public key and
# kid, the value a host puts into MM_REGISTRAR_KEYS.
PUBLIC = "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg"
KID = "Vkdap1RjR0wChd9d"
# The fixed clock both sides use: 2027-01-15T08:00:00Z.
NOW = 1800000000
SET_AT_MS = NOW * 1000
CHANNEL = "c0000000000000000000000000"
HOME = "c1111111111111111111111111"
SET_BY = "dembaca"
SET_BY_ID = "u0000000000000000000000000"  # the Go test's fixed human, a Mattermost id


def _fixture(name: str) -> str:
    return (Path(__file__).parent / "testdata" / name).read_text(encoding="ascii").strip()


def _keys():
    raw = base64.urlsafe_b64decode(PUBLIC + "=" * (-len(PUBLIC) % 4))
    assert attest.kid_of(raw) == KID
    # The record's header names the same kid, so the pin finds the key.
    assert f'"kid":"{KID}"' in base64.urlsafe_b64decode(_fixture("bridge-mode.jws").split(".")[0]).decode()
    return attest.pinned(PUBLIC)


def test_the_bridge_mode_record_passes_the_checker():
    token = _fixture("bridge-mode.jws")
    rec, why = attest.verify_mode(token, _keys(), channel_id=CHANNEL, now=NOW)
    assert why == "" and rec is not None
    assert (rec.mode, rec.set_by, rec.set_by_id) == ("coordinator", SET_BY, SET_BY_ID)
    assert (rec.set_at_ms, rec.iat, rec.exp, rec.kid) == (SET_AT_MS, NOW, NOW + 3600, KID)
    # Its own type only, its own home only, its own hour only.
    assert attest.verify_mode(token, _keys(), channel_id=HOME, now=NOW) == (None, "channel_id mismatch")
    assert attest.verify_mode(token, _keys(), channel_id=CHANNEL, now=NOW + 3600 + attest.SKEW_SEC) == (
        None, "expired")
    assert attest.verify_mandate(token, _keys(), channel_id=CHANNEL, now=NOW)[0] is None
    assert attest.verify_debug(token, _keys(), channel_id=CHANNEL, now=NOW)[0] is None


def test_the_bridge_mandate_record_passes_the_checker():
    token = _fixture("bridge-mandate.jws")
    rec, why = attest.verify_mandate(token, _keys(), channel_id=CHANNEL, now=NOW)
    assert why == "" and rec is not None
    assert (rec.home, rec.set_by, rec.set_by_id) == (HOME, SET_BY, SET_BY_ID)
    assert (rec.until, rec.note) == ("2027-01-20", "ship it")
    assert (rec.set_at_ms, rec.iat, rec.exp, rec.kid) == (SET_AT_MS, NOW, NOW + 3600, KID)
    assert attest.verify_mandate(token, _keys(), channel_id=HOME, now=NOW) == (None, "channel_id mismatch")
    assert attest.verify_mode(token, _keys(), channel_id=CHANNEL, now=NOW)[0] is None
    assert attest.verify_debug(token, _keys(), channel_id=CHANNEL, now=NOW)[0] is None


def test_the_bridge_debug_record_passes_the_checker():
    token = _fixture("bridge-debug.jws")
    rec, why = attest.verify_debug(token, _keys(), channel_id=CHANNEL, now=NOW)
    assert why == "" and rec is not None
    # The canonical order, whatever order the command named them in.
    assert rec.categories == ("wake", "props")
    assert (rec.set_by, rec.set_by_id) == (SET_BY, SET_BY_ID)
    assert (rec.set_at_ms, rec.until_ms) == (SET_AT_MS, SET_AT_MS + 30 * 60 * 1000)
    assert (rec.iat, rec.exp, rec.kid) == (NOW, NOW + 3600, KID)
    assert attest.verify_debug(token, _keys(), channel_id=HOME, now=NOW) == (None, "channel_id mismatch")
    # The command set it for 30 minutes; the checker refuses it as ended after,
    # while the signature itself is still an hour fresh.
    assert attest.verify_debug(token, _keys(), channel_id=CHANNEL,
                               now=NOW + 30 * 60 + attest.SKEW_SEC) == (None, "ended")
    assert attest.verify_mode(token, _keys(), channel_id=CHANNEL, now=NOW)[0] is None
    assert attest.verify_mandate(token, _keys(), channel_id=CHANNEL, now=NOW)[0] is None
