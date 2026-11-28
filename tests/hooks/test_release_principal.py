"""RT-6.29, ADR-011: the release key's principal is `cb-release` (#343), changed in the allowed line
and in the parser of the `verify-tag` line together. The principal is only a local label of the
allowed-signers file, not part of a signature: the tests of the signed releases (test_release_*,
test_host_install_release) verify real signatures through it, so a mismatch between the allowed
line and the parser fails them; this file pins the name itself and that no place still says the
old one."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = (ROOT / "bin" / "mm-agent-lib.sh").read_text()


def test_the_allowed_line_and_the_parser_name_cb_release():
    assert 'CB_REL_ALLOWED+="cb-release namespaces=\\"git\\" $k"' in LIB
    assert "'s/^Good \"git\" signature for cb-release with " in LIB


def test_no_place_still_names_mm_release():
    """The scripts, the docs and the owner's signing recipe in CLAUDE.md agree: a stale name in
    the recipe would build an allowed file the host's own parser would not match."""
    out = subprocess.run(["git", "grep", "-n", "mm-release", "--", ".", ":!CHANGELOG.md",
                          ":!tests/hooks/test_release_principal.py"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 1 and out.stdout == "", (out.returncode, out.stdout, out.stderr)  # 1: no match
