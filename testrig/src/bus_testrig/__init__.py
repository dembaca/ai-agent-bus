"""Fault runs for the agent bus (phase 1 comparison, phase 4 acceptance).

The harness measures the bus itself, never a stand-in for it: real processes,
real sockets, and the server's own frames relayed by a proxy only when timing
is the fault. See `measurands.toml` for what is measured and why.
"""

__all__ = ["measurands", "proxy", "runner", "proc"]
