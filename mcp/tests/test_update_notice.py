"""bus_info names a newer installed plugin (RT-6.2): a session keeps its version."""

import json

from mattermost_ai_agent_bus import server

KEY = "ai-agent-bus@ai-agent-bus"


def _install(home, version, key=KEY, folder=None):
    """Register one install under `key`, in a folder named after its version by
    default: the path never names the plugin, which is the point (issue #5)."""
    d = home / "cache" / (folder or f"install-{version}")
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    reg = home / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    plugins = json.loads(reg.read_text())["plugins"] if reg.exists() else {}
    plugins.setdefault(key, []).append({"installPath": str(d)})
    reg.write_text(json.dumps({"plugins": plugins}))


def test_newer_installed_version_is_found(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(server, "VERSION", "0.25.1")
    _install(tmp_path, "0.24.0")
    assert server._newer_installed() is None
    _install(tmp_path, "0.26.0")
    _install(tmp_path, "0.9.0")  # numeric, not lexical order
    assert server._newer_installed() == "0.26.0"


def test_the_old_plugin_name_is_still_found(monkeypatch, tmp_path):
    """Before 0.96.0 the plugin was mattermost-ai-agent-bus; an install of it
    still counts, or an update would go unnoticed across the rename."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(server, "VERSION", "0.25.1")
    _install(tmp_path, "0.26.0", key="mattermost-ai-agent-bus@old")
    assert server._newer_installed() == "0.26.0"


def test_another_plugin_in_a_lookalike_path_is_ignored(monkeypatch, tmp_path):
    """Issue #5: the install path does not name the plugin. A decoy whose
    directory holds "ai-agent-bus" but whose key is another plugin must not be
    reported, and an install under a plain folder must be."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(server, "VERSION", "0.25.1")
    _install(tmp_path, "99.9.9", key="other-plugin@m", folder="decoy/ai-agent-bus-bundle")
    _install(tmp_path, "98.0.0", key="not-ai-agent-bus@m", folder="not-ai-agent-bus")
    assert server._newer_installed() is None, "a decoy was taken for this plugin"
    _install(tmp_path, "0.27.0", folder="src/bus")
    assert server._newer_installed() == "0.27.0"


def test_no_registry_no_update(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert server._newer_installed() is None


def test_the_restart_is_by_resume_with_the_own_session_id(monkeypatch):
    """RT-6.2 step 8: `--continue` takes the directory's newest session, which
    may be another one's; `update.how` names `--resume` and this session's id."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "529416e5-aaaa")
    how = server._restart_how()
    assert "claude --resume 529416e5-aaaa" in how
    assert "corral run -- --resume 529416e5-aaaa" in how
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert "claude --resume and pick this session" in server._restart_how()
