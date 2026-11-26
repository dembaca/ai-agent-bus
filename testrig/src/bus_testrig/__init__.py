"""Fault runs for the agent bus.

This harness measures the bus itself now (`bus.bus_run`): two real
`mattermost_ai_agent_bus` processes, one posting and one receiving, through the
Mattermost stub and the proxy. `runner.echo_run` remains the loop that proves
the harness works where the bus does not run. See `measurands.toml` for what is
measured and why. `natsrig` runs the same measurement for a home over NATS,
and `coord` hard-stops the coordinator and measures the take-over (ADR-014
phase 1).
"""

__all__ = ["bus", "cli", "coord", "measurands", "natsrig", "natsserver", "proc", "proxy", "runner"]
