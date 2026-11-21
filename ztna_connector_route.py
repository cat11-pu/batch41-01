"""Zero-trust connector routing.

Connectors are dicts with id, region, latency_ms, healthy,
cert_expires_at, policy_version, rules.  Sessions are sticky to a connector
and to a user; when a connector goes down its sessions migrate.  A
connector is eligible only when it is healthy, its certificate has not
expired at routing time, and its policy version is not behind the router's
current policy version.
"""


import copy


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
        self._user_connectors = {}
        self._policy = {"version": 0, "mapping": {}}
        self._audit = []

    def add_connector(self, connector_id, region, latency_ms, healthy=True, cert_expires_at=10 ** 9):
        """Register a connector with policy version 0 and no rules."""
        self._connectors[connector_id] = {
            "id": connector_id,
            "region": region,
            "latency_ms": latency_ms,
            "healthy": bool(healthy),
            "cert_expires_at": cert_expires_at,
            "policy_version": 0,
            "rules": [],
        }

    def set_health(self, connector_id, healthy):
        """Mark a connector healthy or unhealthy."""
        self._get_connector(connector_id)["healthy"] = bool(healthy)

    def set_latency(self, connector_id, latency_ms):
        """Update the measured latency of a connector."""
        self._get_connector(connector_id)["latency_ms"] = latency_ms

    def set_policy(self, version, mapping):
        """Publish a new policy mapping connector id -> rule list.

        Versions must increase; an older/equal version returns False and
        changes nothing.  One user with both "deny" and "allow" for the same
        connector raises ``ConnectorError("policy_conflict")``.
        """
        if version <= self._policy["version"]:
            return False

        for rules in mapping.values():
            actions = {}
            for rule in rules or []:
                if not isinstance(rule, dict):
                    continue
                user = rule.get("user")
                action = rule.get("action")
                if user is None or action not in ("allow", "deny"):
                    continue
                previous = actions.get(user)
                if previous is not None and previous != action:
                    raise ConnectorError("policy_conflict")
                actions[user] = action

        self._policy = {
            "version": version,
            "mapping": copy.deepcopy(mapping),
        }
        return True

    def policy_version(self):
        """Return the current policy version."""
        return self._policy["version"]

    def sync_policy(self, version, connector_ids=None):
        """Atomically load the full rule list into the named connectors.

        A version other than the current one raises
        ``ConnectorError("stale_version")``.  Returns the applied ids.
        """
        if version != self._policy["version"]:
            raise ConnectorError("stale_version")

        if connector_ids is None:
            connector_ids = list(self._policy["mapping"])
        else:
            connector_ids = list(dict.fromkeys(connector_ids))

        updates = []
        applied = []
        for connector_id in connector_ids:
            connector = self._connectors.get(connector_id)
            if connector is None:
                continue
            rules = self._policy["mapping"].get(connector_id, [])
            updates.append((connector, copy.deepcopy(rules)))
            applied.append(connector_id)

        for connector, rules in updates:
            connector["policy_version"] = version
            connector["rules"] = rules

        return applied

    def connector_policy(self, connector_id):
        """Return exactly ``{"version": int, "rules": [...]}``."""
        connector = self._get_connector(connector_id)
        return {
            "version": connector["policy_version"],
            "rules": copy.deepcopy(connector["rules"]),
        }

    def restart_connector(self, connector_id):
        """Reset a restarted connector to policy version 0 and no rules."""
        connector = self._get_connector(connector_id)
        connector["policy_version"] = 0
        connector["rules"] = []

    def route(self, user, region, session_id=None, now=0):
        """Pick an eligible connector and return a routing decision.

        Existing sessions and existing user sessions win (stickiness).  New
        sessions prefer the lowest latency inside the requested region, then
        the lowest latency overall.  Raises ``ConnectorError("no_connector")``
        when nothing is eligible.  The result is a new dict with connector,
        region, policy_version, latency_ms, and session_id.  Audit rows
        carry connector and policy version.
        """
        if session_id is not None and session_id in self._sessions:
            session = self._sessions[session_id]
            connector = self._connectors.get(session["connector"])
            if connector is None or not self._is_eligible(connector, now):
                raise ConnectorError("no_connector")
            decision = self._decision(connector, session_id)
            self._add_audit("route", connector, user, region, session_id)
            return decision

        if session_id is not None and len(self._sessions) >= self.session_cap:
            raise ConnectorError("session_cap")

        preferred = self._user_connectors.get(user)
        connector = self._select(region, now, preferred=preferred)
        if connector is None:
            raise ConnectorError("no_connector")

        if session_id is not None:
            self._sessions[session_id] = {
                "session_id": session_id,
                "user": user,
                "connector": connector["id"],
                "region": region,
            }
        self._user_connectors[user] = connector["id"]

        decision = self._decision(connector, session_id)
        self._add_audit("route", connector, user, region, session_id)
        return decision

    def open_session(self, session_id, user, connector_id=None, region=None):
        """Bind a session explicitly; repeating the id is idempotent.

        The table is capped by ``session_cap``; a new id beyond the cap
        raises ``ConnectorError("session_cap")``.
        """
        if session_id in self._sessions:
            session = self._sessions[session_id]
            connector = self._connectors.get(session["connector"])
            if connector is None:
                raise ConnectorError("no_connector")
            return self._decision(connector, session_id)

        if len(self._sessions) >= self.session_cap:
            raise ConnectorError("session_cap")

        if connector_id is not None:
            connector = self._connectors.get(connector_id)
            if connector is None:
                raise ConnectorError("no_connector")
        else:
            preferred = self._user_connectors.get(user)
            connector = self._select(region, 0, preferred=preferred)
            if connector is None:
                raise ConnectorError("no_connector")

        self._sessions[session_id] = {
            "session_id": session_id,
            "user": user,
            "connector": connector["id"],
            "region": region if region is not None else connector["region"],
        }
        self._user_connectors[user] = connector["id"]
        return self._decision(connector, session_id)

    def connector_down(self, connector_id, now=0):
        """Take a connector down and migrate its sessions.

        Returns ``{"migrated": {session_id: new_connector},
        "failed": [session_id]}``.
        """
        connector = self._connectors.get(connector_id)
        if connector is None:
            return {"migrated": {}, "failed": []}

        connector["healthy"] = False
        migrated = {}
        failed = []

        for session_id, session in list(self._sessions.items()):
            if session["connector"] != connector_id:
                continue

            user = session["user"]
            preferred = self._user_connectors.get(user)
            target = self._select(
                connector["region"],
                now,
                preferred=preferred,
                exclude={connector_id},
            )
            if target is None:
                failed.append(session_id)
                self._add_audit(
                    "migration_failed",
                    connector,
                    user,
                    session.get("region"),
                    session_id,
                )
                continue

            session["connector"] = target["id"]
            if session.get("region") is None:
                session["region"] = target["region"]
            self._user_connectors[user] = target["id"]
            migrated[session_id] = target["id"]
            self._add_audit(
                "migrate",
                target,
                user,
                session.get("region"),
                session_id,
                from_connector=connector_id,
            )

        return {"migrated": migrated, "failed": failed}

    def session_connector(self, session_id):
        """Return the connector bound to a session, or None."""
        session = self._sessions.get(session_id)
        return session["connector"] if session is not None else None

    def session_count(self):
        """Return the number of live sessions."""
        return len(self._sessions)

    def audit(self):
        """Return routing, migration, and failure audit rows."""
        return copy.deepcopy(self._audit)

    def _get_connector(self, connector_id):
        connector = self._connectors.get(connector_id)
        if connector is None:
            raise ConnectorError("unknown_connector")
        return connector

    def _is_eligible(self, connector, now):
        expires_at = connector["cert_expires_at"]
        cert_valid = expires_at is None or expires_at > now
        return (
            connector["healthy"]
            and cert_valid
            and connector["policy_version"] >= self._policy["version"]
        )

    def _select(self, region, now, preferred=None, exclude=None):
        excluded = exclude or set()
        if (
            preferred is not None
            and preferred not in excluded
            and preferred in self._connectors
            and self._is_eligible(self._connectors[preferred], now)
        ):
            return self._connectors[preferred]

        candidates = [
            connector
            for connector_id, connector in self._connectors.items()
            if connector_id not in excluded and self._is_eligible(connector, now)
        ]
        if not candidates:
            return None

        local = [connector for connector in candidates if connector["region"] == region]
        pool = local or candidates
        return min(pool, key=lambda connector: (connector["latency_ms"], str(connector["id"])))

    def _decision(self, connector, session_id):
        return {
            "connector": connector["id"],
            "region": connector["region"],
            "policy_version": connector["policy_version"],
            "latency_ms": connector["latency_ms"],
            "session_id": session_id,
        }

    def _add_audit(self, action, connector, user, region, session_id, **extra):
        row = {
            "action": action,
            "connector": connector["id"],
            "policy_version": connector["policy_version"],
            "user": user,
            "region": region,
            "session_id": session_id,
        }
        row.update(extra)
        self._audit.append(row)
