"""Zero-trust connector routing.

Delivered as a failing shell: every public operation raises
``NotImplementedError`` until routing is implemented.

Connectors are dicts with id, region, latency_ms, healthy,
cert_expires_at, policy_version, rules.  Sessions are sticky to a connector
and to a user; when a connector goes down its sessions migrate.  A
connector is eligible only when it is healthy, its certificate has not
expired at routing time, and its policy version is not behind the router's
current policy version.
"""


class ConnectorError(Exception):
    """Routing failure carrying a stable ``reason`` code."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ZtnaConnectorRouter:
    """Score, stick, and migrate connector routes."""

    def __init__(self, session_cap=10000):
        self.session_cap = session_cap
        self._connectors = {}
        self._sessions = {}
        self._policy = {"version": 0, "mapping": {}}
        self._audit = []

    def add_connector(self, connector_id, region, latency_ms, healthy=True, cert_expires_at=10 ** 9):
        """Register a connector with policy version 0 and no rules."""
        raise NotImplementedError

    def set_health(self, connector_id, healthy):
        """Mark a connector healthy or unhealthy."""
        raise NotImplementedError

    def set_latency(self, connector_id, latency_ms):
        """Update the measured latency of a connector."""
        raise NotImplementedError

    def set_policy(self, version, mapping):
        """Publish a new policy mapping connector id -> rule list.

        Versions must increase; an older/equal version returns False and
        changes nothing.  One user with both "deny" and "allow" for the same
        connector raises ``ConnectorError("policy_conflict")``.
        """
        raise NotImplementedError

    def policy_version(self):
        """Return the current policy version."""
        raise NotImplementedError

    def sync_policy(self, version, connector_ids=None):
        """Atomically load the full rule list into the named connectors.

        A version other than the current one raises
        ``ConnectorError("stale_version")``.  Returns the applied ids.
        """
        raise NotImplementedError

    def connector_policy(self, connector_id):
        """Return exactly ``{"version": int, "rules": [...]}``."""
        raise NotImplementedError

    def restart_connector(self, connector_id):
        """Reset a restarted connector to policy version 0 and no rules."""
        raise NotImplementedError

    def route(self, user, region, session_id=None, now=0):
        """Pick an eligible connector and return a routing decision.

        Existing sessions and existing user sessions win (stickiness).  New
        sessions prefer the lowest latency inside the requested region, then
        the lowest latency overall.  Raises ``ConnectorError("no_connector")``
        when nothing is eligible.  The result is a new dict with connector,
        region, policy_version, latency_ms, and session_id.  Audit rows
        carry connector and policy version.
        """
        raise NotImplementedError

    def open_session(self, session_id, user, connector_id=None, region=None):
        """Bind a session explicitly; repeating the id is idempotent.

        The table is capped by ``session_cap``; a new id beyond the cap
        raises ``ConnectorError("session_cap")``.
        """
        raise NotImplementedError

    def connector_down(self, connector_id, now=0):
        """Take a connector down and migrate its sessions.

        Returns ``{"migrated": {session_id: new_connector},
        "failed": [session_id]}``.
        """
        raise NotImplementedError

    def session_connector(self, session_id):
        """Return the connector bound to a session, or None."""
        raise NotImplementedError

    def session_count(self):
        """Return the number of live sessions."""
        raise NotImplementedError

    def audit(self):
        """Return routing, migration, and failure audit rows."""
        raise NotImplementedError
