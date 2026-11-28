"""The plugin's MCP entry point (entry.py): host process and relay to hosted.py
(RT-6.18), everything else the plain stdio server, without hosted.py."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from mattermost_ai_agent_bus import entry, hosted

MCP = Path(__file__).resolve().parents[1]


def test_the_console_script_and_python_m_start_the_entry_point():
    scripts = tomllib.loads((MCP / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts == {"mattermost-ai-agent-bus-mcp": "mattermost_ai_agent_bus.entry:main"}
    main = (MCP / "src" / "mattermost_ai_agent_bus" / "__main__.py").read_text()
    assert "from .entry import main" in main


def test_host_goes_to_the_host_process(monkeypatch):
    """RT-6.18 step 1: ``--host`` is the host process preStart starts."""
    seen = []
    monkeypatch.setattr(hosted, "host_main", lambda args: seen.append(args) or 4)
    with pytest.raises(SystemExit) as exc:
        entry.main(["--host", "--state-dir", "/x"])
    assert exc.value.code == 4 and seen == [["--state-dir", "/x"]]


def test_the_plain_path_runs_the_stdio_server(monkeypatch):
    for name in ("CB_BUS_MCP_ENDPOINT", "MM_BUS_MCP_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    from mattermost_ai_agent_bus import server

    ran = []
    monkeypatch.setattr(server, "main", lambda: ran.append(True))
    monkeypatch.setattr(hosted, "relay", lambda env: pytest.fail("relayed"))
    entry.main([])
    assert ran == [True]


def test_the_plain_path_never_imports_hosted():
    """Plan B piece 14d: without --host and the relay environment nothing loads
    hosted.py, so the cutover release can delete it."""
    code = (
        "import sys\n"
        "from mattermost_ai_agent_bus import entry, server\n"
        "server.main = lambda: None\n"
        "entry.main([])\n"
        "print('hosted' if 'mattermost_ai_agent_bus.hosted' in sys.modules else 'clean')\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("CB_BUS_MCP_ENDPOINT", "MM_BUS_MCP_ENDPOINT", "CB_BUS_KEY", "MM_BUS_KEY")}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "clean"
