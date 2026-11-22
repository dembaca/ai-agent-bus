"""Fault runs for the agent bus.

This harness measures its own echo loop today (`runner.echo_run`): a real
`websockets` server, the real proxy in front of it and its own client. Driving
the bus itself is the follow-up PR. See `measurands.toml` for what is measured
and why.
"""

__all__ = ["cli", "measurands", "proc", "proxy", "runner"]
