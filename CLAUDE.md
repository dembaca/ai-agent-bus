# Working in this repo

## Docs move with the code

The behaviour spec lives in [docs/arc42](docs/arc42) (arc42, LEAN depth). A change
is not done until the docs say what the code now does:

- **Which doc?** [docs/doc-map.toml](docs/doc-map.toml) maps code paths to the docs
  they must update. CI fails a PR that changes a mapped path without one of its
  docs; a change that truly needs none gets the `no-doc-change` label. A rule may
  also carry an `exclude` list of globs whose matches it does not claim — the login
  service leaves out its own `*_test.go` and `*/testdata/*` that way, so a
  test-only PR needs no label. The matcher and the shipped map are pinned by
  [tests/hooks/test_doc_map.py](tests/hooks/test_doc_map.py).
- **Runtime texts are product, not docs.** `SKILL.md`, the MCP `instructions` in
  `mcp/src/…/server.py`, the hook `reason` texts and the `hooks/hooks.json`
  description are read by agents at runtime. Update them with the behaviour.
- **Scenario ids.** Section 6 scenarios are `RT-6.x` (their section number).
  Reference the id from the code that implements a scenario, from `SKILL.md`,
  and from at least one test in `tests/hooks/` (hook behaviour) or `mcp/tests/`
  (MCP server behaviour), or a `_test.go` beside a Go service under `services/`. A new scenario needs a test.
- **The website is product copy too.** [`site/`](site) (published at
  https://dembaca.github.io/ai-agent-bus/) promises features, a
  harness matrix, security answers and a quick start. A PR that adds or changes
  a feature, a harness's support, a security property or an install step
  updates `site/index.html` and its illustrations in the same PR, or says in
  its body why the site needs no change.
- **Describe once, link elsewhere.** README, adapter docs and `docs/protocol.md`
  link to the arc42 section instead of restating behaviour.
- **Version bump → CHANGELOG.md entry** for that version, and the same version in
  `.claude-plugin/plugin.json` (the one doc-check reads), `mcp/pyproject.toml`,
  `mcp/src/…/__init__.py` and `mcp/uv.lock` and `console/server/uv.lock` (it pins the MCP package;
  doc-check compares the first three).
- **Which number?** SemVer from 0.72.0. Minor: a new tool, argument or
  command, a noticeably changed behaviour, anything breaking (under
  `### Breaking` in its CHANGELOG entry). Patch: fixes that change no
  behaviour, docs, tests.

## Releasing

The PR that bumps the version also checks the website against its CHANGELOG
entry: the features, harness matrix, Security Q&A and quick start in `site/`
say what the version does. Fix what is off in that PR, before the owner is
asked to sign.

A version reaches hosts as a release tag (ADR-011, RT-6.29); every tag is signed,
since hosts that pin the release keys install nothing else. After the
merge the owner, and only the owner, creates the annotated SSH-signed tag
`v<version>` (the version in `.claude-plugin/plugin.json`) on the merge commit
and pushes it: `git tag -s v<version> -m v<version> <merge commit>`
(`gpg.format=ssh`, the owner's release key), checks it with
`git verify-tag v<version>` (`gpg.ssh.allowedSignersFile` with the pinned keys),
then `git push origin v<version>`. Never `-f`: a tag is never moved or re-signed
(hosts refuse a moved tag). Agents never sign and never create or push version
tags. `CB_RELEASE_URL` as a local path is for a mirror and the tests.

With the key on a YubiKey (FIDO2, `sk-ssh-ed25519`), macOS's own `ssh-keygen`
cannot sign ("No FIDO SecurityKeyProvider specified"); Homebrew's can
(`brew install openssh`). `CB_RELEASE_SIGNERS` holds bare keys, `verify-tag`
needs the principal in front:

```bash
echo "cb-release $(cat ~/.ssh/id_ed25519_sk.pub)" > "$TMPDIR/allowed"
git -c gpg.format=ssh -c gpg.ssh.program=/opt/homebrew/bin/ssh-keygen \
    -c user.signingkey="$HOME/.ssh/id_ed25519_sk" \
    tag -s v<version> -m v<version> <merge commit>     # touch the key
git -c gpg.ssh.program=/opt/homebrew/bin/ssh-keygen \
    -c gpg.ssh.allowedSignersFile="$TMPDIR/allowed" \
    verify-tag v<version> && git push origin v<version>
```

What a release needs first goes into its CHANGELOG entry as a line of its own,
`Needs: CB_PROPS_PREFIX alike on every server`: only `CB_*` and `MM_*` names, versions
`x.y.z` and lowercase words of at most 15 letters, each with an optional `,` or `.`, at
most 120 characters, since every agent's notice shows it (RT-6.30). A host before
0.103.0 drops a line with a `CB_*` name, so the `Needs:` lines of 0.103.0 and of the
release that removes the old-name fallbacks name no `CB_*` name: old `MM_*` names,
versions and words only, the new names go into the entry's text
([docs/env-names.md](docs/env-names.md#the-needs-rule)). A release with a known bad predecessor says
`Withdrawn: v<x.y.z>[, v<x.y.z>]`. doc-check enforces both forms.

## Small changes

A change of at most about 50 lines, in one area, with no new behaviour and nothing
in login, tokens, rights, hook trust, NATS permissions or release and signing, may
skip an ADR and repeated review rounds. Changes in login, tokens, rights, hook trust,
NATS permissions or release and signing always take the full way. It keeps:

- **One review** by a model with the overview of the whole (Opus or Fable, never a
  cheaper one because the diff looks small), which reads the change against its
  callers, the runtime texts and the docs, for corner cases.
- **Green CI for the areas it touches** (`checks.yml` runs each job only for the
  areas a PR changes; a file no area claims runs all of them).
- **The merge go** as usual.

**Basis rule** (the merge approver's call, agreed with the ~chat-next coordinator on
2026-11-28 at the owner's request). A PR whose CI went green on an older `main` may
merge without a new run when all of these hold:

- the areas `changes`' script sets for `main`'s commits since the PR's base and those it
  sets for the PR (its outputs `py`, `opencode`, `pi`, `site`, `console`, not
  directories: `mcp/` sets four) share none, and neither side touches a path that runs everything
  (`checks.yml`, an unclaimed path, a deletion or a rename);
- `main`'s commits touch none of `docs/doc-map.toml`, `scripts/`, `ruff.toml` and
  `.shellcheckrc`, and the `docs` job's two commands pass on the merged tree
  (`arc42-lint.py docs/arc42 --strict`, `doc-check.py`): doc-check reads all of
  `docs/arc42/`, `docs/env-names.md`, the versions and every tracked file for
  scenario ids and environment names, so a PR and `main` can each pass it alone and
  fail together;
- the PR and those commits do not both touch the paths of another workflow
  (`auth.yml`, `bridge.yml`, `keycloak.yml`, `testrig.yml`);
- `main`'s newest completed run is green and no run of a merge made under this rule
  is still going, so at most one such merge is unproven at a time;
- GitHub says mergeable and clean with no commit since the run, and the PR's
  CHANGELOG lines read clean against `main`.

A request under it names the base, `main`'s head, the areas of both diffs and the
result of the two docs commands on a trial merge, with the two commits it was made
from (the PR's head and `main`'s head; a result for an older `main` does not count).
A trial merge that needs a conflict resolved by hand turns the rule off for that PR:
merge `main` in and rerun. The approver takes both file lists from GitHub and runs
the script's `case` on them.

**Conflicts.** Resolve a conflict in text by taking whole sides or writing it by
hand, never with a character- or word-level merge script (one garbled the CHANGELOG
in #295 and #305), and read a merge's CHANGELOG and doc hunks as text.

## Before you push

```bash
python3 scripts/arc42-lint/arc42-lint.py docs/arc42 --strict
python3 scripts/doc-check.py --base origin/main
uv run --directory mcp pytest -q
uv run --directory mcp pytest -q ../tests/hooks
git ls-files -z '*.sh' '*.sh.example' | xargs -0 shellcheck -S warning
uvx ruff@0.17.2 check
uv run --directory mcp mypy
npm --prefix site ci && npm --prefix site run typecheck && npm --prefix site run build   # when site/ changed
```

`scripts/arc42-lint/` is vendored from MSiccDev/arc42-toolkit — do not edit it;
own checks go into `scripts/doc-check.py`.
