"""The operator console's server (ADR-015 pilot, stage 1, reading only).

One Python service: it reads the NATS homes' presence, crown claims and
memberships through the same JetStream consumers the agents read through,
serves the API of ADR-015 point 10 to the browser app (``console/web``),
and logs an operator in over Keycloak (authorization code with PKCE, a
client of its own, members of an operators group).

Pilot deviations, decided by the owner (2026-11-24): one read-only NATS
account for the whole console, lab and one tenant only — ADR-015 way A
(a credential per person, made by the login service) follows after the
pilot.
"""

__version__ = "0.1.0"
