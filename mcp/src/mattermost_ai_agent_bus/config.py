"""The host's settings as the harness passes them: environment variables,
a plugin manifest's unexpanded ``${VAR}`` read as unset. client.py
re-exports env.

From 0.103.0 the bus's own names carry the ``CB_`` prefix (docs/env-names.md,
issue #169). Every renamed setting is read through this module: the new name
first, else the old name(s) with one deprecation warning per process and old
name; two of its names set to different values refuse the start
(``EnvConflict``), naming the names and never a value. Empty counts as set;
an unexpanded placeholder counts as unset. No warning for a value the bus
writes for itself (INTERNAL) or inside a keyless sandbox (keyless_sandbox). A
file the bus wrote is read by ``value``: the last line of either spelling
wins, never a refusal. The registrar's names
(``MM_REG_*``, ``MM_REGISTER_URL``, ``MM_REGISTRAR_KEYS``, ``MM_AGENT_PREFIX``,
``MM_BOT_ATTESTATION``) are not renamed and read as they are.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from collections.abc import Mapping, MutableMapping

log = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")

# New name -> its old names, in the order they are read (docs/env-names.md,
# the tables "The MCP server and the plugin", "Hooks and scripts" and "Keys in
# the session files").
RENAMED: dict[str, tuple[str, ...]] = {
    # The MCP server and the plugin.
    "CB_ACK_WAIT_SEC": ("MM_ACK_WAIT_SEC",),
    "CB_AGENT_KIND": ("MM_AGENT_KIND",),
    "CB_ASK_REMIND_SEC": ("MM_ASK_REMIND_SEC",),
    "CB_ASK_ROUTE": ("MM_ASK_ROUTE",),
    "CB_ASK_ROUTE_WAIT_MIN": ("MM_ASK_ROUTE_WAIT_MIN",),
    "CB_MM_BOT_TOKEN": ("MM_BOT_TOKEN", "MATTERMOST_TOKEN"),
    "CB_MM_BOT_USER_ID": ("MM_BOT_USER_ID",),
    "CB_MM_BOT_USERNAME": ("MM_BOT_USERNAME",),
    "CB_MM_BRIDGE_BOT": ("MM_BRIDGE_BOT",),
    "CB_BUS_ENDPOINT": ("MM_BUS_ENDPOINT",),
    "CB_BUS_KEPT_SID": ("MM_BUS_KEPT_SID",),
    "CB_BUS_KEY": ("MM_BUS_KEY",),
    "CB_BUS_MCP_ENDPOINT": ("MM_BUS_MCP_ENDPOINT",),
    "CB_BUS_UNAVAILABLE": ("MM_BUS_UNAVAILABLE",),
    "CB_MM_CHANNEL": ("MM_CHANNEL",),
    "CB_MM_CHANNEL_ID": ("MM_CHANNEL_ID",),
    "CB_CHANNEL_PUSH": ("MM_CHANNEL_PUSH",),
    "CB_MM_CHAT_URL": ("MM_CHAT_URL", "MATTERMOST_URL"),
    "CB_CROWN_BEAT_SEC": ("MM_CROWN_BEAT_SEC",),
    "CB_CROWN_LEASE_SEC": ("MM_CROWN_LEASE_SEC",),
    "CB_HEARTBEAT_SEC": ("MM_HEARTBEAT_SEC",),
    "CB_LEASE_SEC": ("MM_LEASE_SEC",),
    "CB_LISTEN_COORD_SEC": ("MM_LISTEN_COORD_SEC",),
    "CB_LISTEN_DIGEST_SEC": ("MM_LISTEN_DIGEST_SEC",),
    "CB_LISTEN_SEC": ("MM_LISTEN_SEC",),
    "CB_LOGIN_CLIENT_ID": ("MM_LOGIN_CLIENT_ID",),
    "CB_LOGIN_ISSUER": ("MM_LOGIN_ISSUER",),
    "CB_LOGIN_NKEY": ("MM_LOGIN_NKEY",),
    "CB_NATS_CA_FILE": ("MM_NATS_CA_FILE",),
    "CB_NATS_HOMES": ("MM_NATS_HOMES",),
    "CB_NATS_MEMORY": ("MM_NATS_MEMORY",),
    "CB_NATS_URL": ("MM_NATS_URL",),
    "CB_OWNER": ("MM_OWNER",),
    "CB_MM_OWNER_USER_ID": ("MM_OWNER_USER_ID",),
    "CB_MM_PROJECT_CHANNEL": ("MM_PROJECT_CHANNEL",),
    "CB_MM_PROJECT_CHANNEL_ID": ("MM_PROJECT_CHANNEL_ID",),
    "CB_PROPS_PREFIX": ("MM_PROPS_PREFIX",),
    "CB_REMIND_MAX_SEC": ("MM_REMIND_MAX_SEC",),
    "CB_RESPONSIBLE_HUMAN": ("MM_RESPONSIBLE_HUMAN",),
    "CB_SESSION_KEEP_SEC": ("MM_SESSION_KEEP_SEC",),
    "CB_STALL_SEC": ("MM_STALL_SEC",),
    "CB_MM_STATS_CHANNEL": ("MM_STATS_CHANNEL",),
    "CB_MM_TEAM": ("MM_TEAM",),
    "CB_MM_TRUSTED_HUMAN_USER_IDS": ("MM_TRUSTED_HUMAN_USER_IDS",),
    "CB_TRUSTED_HUMANS": ("MM_TRUSTED_HUMANS",),
    "CB_MM_TYPING_SEC": ("MM_TYPING_SEC",),
    "CB_WORKSPACE": ("MM_WORKSPACE",),
    "CB_RELEASE_SIGNERS": ("MM_RELEASE_SIGNERS",),
    "CB_RELEASE_URL": ("MM_RELEASE_URL",),
    # Plan B piece 14b: new, no old spelling (ADR-014, the owner's decision of
    # 2026-11-27; docs/env-names.md, "Goes with the registrar"). Unset: off.
    "CB_RECORDS_URL": (),
    # Hooks and scripts (the ones this package reads or passes on).
    "CB_AGENT_SESSION_FILE": ("MM_AGENT_SESSION_FILE",),
    "CB_MM_BOT_TOKEN_FD": ("MM_BOT_TOKEN_FD",),
    "CB_BUS_DEADLINE": ("MM_BUS_DEADLINE",),
    "CB_BUS_HOOK_DISPATCH": ("MM_BUS_HOOK_DISPATCH",),
    "CB_BUS_HOST_DIR": ("MM_BUS_HOST_DIR",),
    "CB_BUS_MAX_TIME": ("MM_BUS_MAX_TIME",),
    "CB_BUS_UPLOAD": ("MM_BUS_UPLOAD",),
    "CB_LISTEN_POLL_SEC": ("MM_LISTEN_POLL_SEC",),
    "CB_MM_TEAM_ID": ("MM_TEAM_ID",),
    "CB_REL_GIT_DIR": ("MM_REL_GIT_DIR",),
    "CB_REL_TMP": ("MM_REL_TMP",),
    # Keys in the session files.
    "CB_BUS_CLIENT": ("MM_BUS_CLIENT",),
    "CB_BUS_HOST_PID": ("MM_BUS_HOST_PID",),
    "CB_BUS_HOST_START": ("MM_BUS_HOST_START",),
    "CB_BUS_PID": ("MM_BUS_PID",),
    "CB_CLAUDE_SID": ("MM_CLAUDE_SID",),
    "CB_KEPT_CLAUDE_SID": ("MM_KEPT_CLAUDE_SID",),
    "CB_KEPT_WORKDIR": ("MM_KEPT_WORKDIR",),
    "CB_SERVER_ENDED": ("MM_SERVER_ENDED",),
    "CB_SESSION_PID": ("MM_SESSION_PID",),
}
# Old name -> new name.
NEW_NAME: dict[str, str] = {old: new for new, olds in RENAMED.items() for old in olds}

# Values the bus sets for itself (docs/env-names.md, "Values the bus writes for
# itself"): written under both names, so an old name there is no operator's
# setting and earns no deprecation warning.
INTERNAL = frozenset({
    "CB_BUS_ENDPOINT", "CB_BUS_KEPT_SID", "CB_BUS_KEY", "CB_BUS_MCP_ENDPOINT", "CB_BUS_UNAVAILABLE",
    "CB_CHANNEL_PUSH", "CB_MM_BOT_TOKEN_FD", "CB_BUS_DEADLINE", "CB_BUS_UPLOAD", "CB_REL_GIT_DIR",
    "CB_REL_TMP", "CB_BUS_CLIENT", "CB_BUS_HOST_PID", "CB_BUS_HOST_START", "CB_BUS_PID", "CB_CLAUDE_SID",
    "CB_KEPT_CLAUDE_SID", "CB_KEPT_WORKDIR", "CB_SERVER_ENDED", "CB_SESSION_PID", "CB_BUS_HOOK_DISPATCH",
})

# CB_ names that are no setting of this server but belong to something else in the
# same environment (docs/env-names.md: the other services' tables, the hooks' and
# scripts' own variables, the tests): never reported as unknown. tests/hooks/
# test_env_names.py pins that every name that page lists is known here or by prefix.
OTHER_PREFIXES = ("CB_AUTH_", "CB_AUTHD_", "CB_BRIDGE_", "CB_CONSOLE_", "CB_TEST_", "CB_TESTRIG_", "CB_MM_TEST_",
                  "CB_REL_", "CB_ENV_", "CB_BUS_")
OTHER = frozenset({
    "CB_AGENT_BUS_ROOT", "CB_NATS_SERVER", "CB_PLANB_TEST", "CB_SESSION_NONCE", "CB_COPIED_FROM", "CB_STATE_KEY",
    "CB_TIE", "CB_TAKEN_BY", "CB_ANSWER",
    "CB_AUTO_FILE", "CB_AUTO_LOG", "CB_CREDS_FROM_FD", "CB_CROWN", "CB_CROWN_NEEDS_BOT", "CB_CROWN_UNTIL",
    "CB_HOLD_FILE",
    "CB_HOOK_INPUT", "CB_INBOX", "CB_LISTENING", "CB_MM_API_BODY", "CB_MM_API_STATUS", "CB_NEEDS_RE",
    "CB_SIGNERS_FILE", "CB_SIGNERS_MAX", "CB_SIGNOFF", "CB_UPDATE", "CB_WITHDRAWN_FILE",
})


def known(name: str) -> bool:
    """`name` is a setting of the bus (or something else in the project's table)."""
    return (name in RENAMED or name in INTERNAL or name in OTHER
            or name.startswith(OTHER_PREFIXES))


UNKNOWN_CAP = 10
_PLAIN_NAME_RE = re.compile(r"[A-Z0-9_]{1,64}")


def unknown_names(environ: Mapping[str, str] | None = None) -> list[str]:
    """One line per `CB_` name in the environment that nothing knows (#261): a typo or a
    wrong prefix would otherwise leave its setting silently unread. With the closest known
    name, the `CB_MM_` prefix swap first. Names only, never a value. Only names of the
    form `[A-Z0-9_]{1,64}` are reported, at most UNKNOWN_CAP of them (#269); the odd ones and
    the rest are counted in a last line, "and N more"."""
    found = sorted(n for n in (os.environ if environ is None else environ)
                   if n.startswith("CB_") and not known(n))
    plain = [n for n in found if _PLAIN_NAME_RE.fullmatch(n)]
    names = plain[:UNKNOWN_CAP]
    more = len(found) - len(names)
    pool = sorted(RENAMED)
    out = []
    for name in names:
        swap = ("CB_" + name[len("CB_MM_"):]) if name.startswith("CB_MM_") else "CB_MM_" + name[len("CB_"):]
        near = [swap] if swap in RENAMED else difflib.get_close_matches(name, pool, n=1, cutoff=0.8)
        hint = f" (did you mean {near[0]}?)" if near else ""
        out.append(f"unknown setting {name}{hint}")
    if more:
        out.append(f"and {more} more unknown CB_ settings")
    return out


# Old names whose deprecation this process has logged (once each).
_warned: set[str] = set()


class EnvConflict(RuntimeError):
    """Two names of one setting hold different values.

    The message names the names, never a value: some of them are secrets.
    """


def spellings(name: str) -> tuple[str, ...]:
    """Every name of one setting, the new one first; any of its names will do."""
    new = NEW_NAME.get(name, name)
    return (new, *RENAMED.get(new, ()))


def _given(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None or _PLACEHOLDER_RE.match(value):
        return None
    return value


def keyless_sandbox(environ: Mapping[str, str] | None = None) -> bool:
    """The host hook started this session's MCP server and wrote the settings
    here (CB_BUS_MCP_ENDPOINT, or MM_BUS_MCP_ENDPOINT from an older hook): an
    old name there is the hook's doing, no operator's, and earns no warning."""
    env = os.environ if environ is None else environ
    return any(_given(env, n) is not None for n in ("CB_BUS_MCP_ENDPOINT", "MM_BUS_MCP_ENDPOINT"))


def lookup(name: str, environ: Mapping[str, str] | None = None, *, quiet: bool = False) -> str | None:
    """The setting's value, or None when none of its names is set.

    Empty counts as set: an empty new name never falls back to an old one. A
    value that is exactly an unexpanded ``${VAR}`` counts as unset. Raises
    EnvConflict when two of its names that are set differ. ``quiet``: the
    mapping is not an operator's environment, so reading an old name there
    logs nothing; nor does it in a keyless sandbox (keyless_sandbox).
    """
    env = os.environ if environ is None else environ
    names = spellings(name)
    found = [(n, v) for n in names if (v := _given(env, n)) is not None]
    if not found:
        return None
    if len({v for _, v in found}) > 1:
        listed = ", ".join(n for n, _ in found)
        raise EnvConflict(f"{listed} are set to different values; set only {names[0]}")
    first, value = found[0]
    if (first != names[0] and not quiet and names[0] not in INTERNAL and first not in _warned
            and not keyless_sandbox(env)):
        _warned.add(first)
        log.warning("%s is deprecated; set %s instead (the old name goes in a later release)",
                    first, names[0])
    return value


def env(name: str, default: str = "", environ: Mapping[str, str] | None = None) -> str:
    """Read a setting, treating an unexpanded ``${VAR}`` and an empty value as unset.

    A plugin manifest declares env as ``"CB_MM_PROJECT_CHANNEL": "${CB_MM_PROJECT_CHANNEL}"``.
    When the variable is not set in the environment, the placeholder can arrive
    verbatim — and a literal "${CB_MM_PROJECT_CHANNEL}" is not a channel name.
    A renamed setting is read by its new name, else its old one (lookup).
    """
    got = lookup(name, environ)
    return got if got else default


def value(data: Mapping[str, str], name: str, default: str = "") -> str:
    """A key of one of the bus's own files (session, endpoint), quietly.

    The last line of either spelling wins and keys that differ never refuse:
    a file is the bus's own record, and a downgraded writer appends under the
    old key after the new one. ``data`` keeps the file's order, each key at its
    last line (session_file.parse); bin/mm-agent-lib.sh's mm_kv reads alike.
    """
    names = spellings(name)
    got = None
    for k, v in data.items():
        if k in names:
            got = v
    return got if got else default


def both(name: str, value: str) -> dict[str, str]:
    """A value the bus writes for itself, under every name of its setting."""
    return {n: value for n in spellings(name)}


def put(name: str, value: str, environ: MutableMapping[str, str] | None = None) -> None:
    """Set a value the bus sets for itself (or its children) under every name."""
    env = os.environ if environ is None else environ
    env.update(both(name, value))


def drop(name: str, environ: MutableMapping[str, str] | None = None) -> None:
    """Remove every name of a setting."""
    env = os.environ if environ is None else environ
    for n in spellings(name):
        env.pop(n, None)


def check(environ: Mapping[str, str] | None = None) -> None:
    """Refuse the start on any renamed setting whose names disagree.

    Raises EnvConflict naming each such setting's names (no values); logs the
    deprecation of each old name in use, once.
    """
    errors = []
    for new in RENAMED:
        try:
            lookup(new, environ)
        except EnvConflict as exc:
            errors.append(str(exc))
    if errors:
        raise EnvConflict("; ".join(errors))
