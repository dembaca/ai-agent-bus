"""Cross-language: a record the Go bridge signed, verified by the hosts' verifier.

#151 step 4: the bridge signs the release record with its own key; the hosts
verify it with `attest.pinned` and their own verifier. `testdata/release_record.txt`
is the record `internal/release`'s Go test also asserts (same seed, same times),
so a change on either side fails here.
"""

from pathlib import Path

from mattermost_ai_agent_bus import attest

FIXTURE = (Path(__file__).parent / "testdata" / "release_record.txt").read_text().strip()
KID = "ZbYGc9btiEvwHCwi"
PUB_B64 = "ebVWLo_mVPlAeLES6KmLp5AfhTrmlb7X4OORC60ElmQ"
SIGNER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHm1Vi6P5lT5QHixEuipi6eQH4U65pW+1+DjkQutBJZk"


def test_a_go_signed_release_record_verifies() -> None:
    keys = attest.pinned(PUB_B64)  # the pin derives the kid, as a host's does
    assert list(keys) == [KID]
    rec, why = attest.verify_release(FIXTURE, keys, now=1_800_000_500)
    assert rec is not None, why
    assert rec.signers == (SIGNER,)
    assert rec.version == "latest"
    assert rec.set_at_ms == 1_800_000_000_000
    assert rec.iat == 1_800_000_000 and rec.exp == 1_800_003_600
    assert rec.kid == KID


def test_the_same_record_with_another_pin_is_refused() -> None:
    other = attest.pinned("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    rec, why = attest.verify_release(FIXTURE, other, now=1_800_000_500)
    assert rec is None and why
