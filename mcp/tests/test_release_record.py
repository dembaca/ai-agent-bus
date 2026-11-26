"""Cross-language: a record the Go bridge signed, verified by the hosts' verifier.

#151 step 4: the bridge signs the release record with its own key; the hosts
verify it with `attest.verify_release` and their pinned keys. The fixture below
was produced by `internal/registrar/core.Attestor` (the bridge's code) with a
fixed key and fixed times, so a drift on either side fails here.
"""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from mattermost_ai_agent_bus import attest

FIXTURE = "eyJhbGciOiJFZERTQSIsInR5cCI6Im1tLWFnZW50LXJlbGVhc2Urand0Iiwia2lkIjoiWmJZR2M5YnRpRXZ3SEN3aSJ9.eyJzaWduZXJzIjpbInNzaC1lZDI1NTE5IEFBQUFDM056YUMxbFpESTFOVEU1QUFBQUlIbTFWaTZQNWxUNVFIaXhFdWlwaTZlUUg0VTY1cFcrMStEamtRdXRCSlprIl0sInZlcnNpb24iOiJsYXRlc3QiLCJzZXRfYXRfbXMiOjE4MDAwMDAwMDAwMDAsImlhdCI6MTgwMDAwMDAwMCwiZXhwIjoxODAwMDAzNjAwfQ.gbEsXcchpynO-lciXP6lm1oL30C55Zgex9NfHALjXBTIdu-iu9cgFQfEWZ_vaXS_OMNnl1M4J7TmgeMeIluxCA"
KID = "ZbYGc9btiEvwHCwi"
PUB = bytes.fromhex("79b5562e8fe654f94078b112e8a98ba7901f853ae695bed7e0e3910bad049664")
SIGNER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHm1Vi6P5lT5QHixEuipi6eQH4U65pW+1+DjkQutBJZk"


def test_a_go_signed_release_record_verifies() -> None:
    key = Ed25519PublicKey.from_public_bytes(PUB)
    rec, why = attest.verify_release(FIXTURE, {KID: key}, now=1_800_000_500)
    assert rec is not None, why
    assert rec.signers == (SIGNER,)
    assert rec.version == "latest"
    assert rec.set_at_ms == 1_800_000_000_000
    assert rec.iat == 1_800_000_000 and rec.exp == 1_800_003_600
    assert rec.kid == KID


def test_the_same_record_with_another_pin_is_refused() -> None:
    other = Ed25519PublicKey.from_public_bytes(bytes(32))
    rec, why = attest.verify_release(FIXTURE, {"other": other}, now=1_800_000_500)
    assert rec is None and why
