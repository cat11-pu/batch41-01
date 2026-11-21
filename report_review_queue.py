"""Report review queue with ticket and punishment state linkage."""

import copy


DEFAULT_CONFIG = {
    "cooldown": 100,
    "appeal_window": 200,
    "evidence_retention": 300,
    "malicious_penalty": 10,
}


class ReviewQueue:
    def __init__(self, config=None, identities=None):
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)

        if self.config["evidence_retention"] < self.config["appeal_window"]:
            raise ValueError("evidence retention must cover the appeal window")

        self.identities = dict(identities or {})
        self.tickets = {}
        self._ticket_index = {}
        self._cooldowns = {}
        self._idempotency = {}
        self._next_ticket = 1
        self.reputations = {}
        self.notifications = []

    def _resolve_target(self, target_id, server_id):
        if server_id is None:
            return target_id

        candidates = (
            (server_id, target_id),
            "%s:%s" % (server_id, target_id),
        )
        for key in candidates:
            if key in self.identities:
                return self.identities[key]
        return target_id

    @staticmethod
    def _snapshot_evidence(evidence):
        if evidence is None:
            evidence = {}
        if not isinstance(evidence, dict):
            raise ValueError("evidence must be a mapping")

        snapshot = copy.deepcopy(evidence)
        for kind in ("chat", "battle", "voice"):
            value = snapshot.get(kind, [])
            if value is None:
                value = []
            if isinstance(value, tuple):
                value = list(value)
            snapshot[kind] = copy.deepcopy(value)
        return snapshot

    @staticmethod
    def _merge_evidence(current, incoming):
        merged = copy.deepcopy(current)
        for key, value in incoming.items():
            if key not in merged:
                merged[key] = copy.deepcopy(value)
                continue
            if isinstance(merged[key], list) and isinstance(value, list):
                for item in value:
                    if item not in merged[key]:
                        merged[key].append(copy.deepcopy(item))
            else:
                merged[key] = copy.deepcopy(value)
        for kind in ("chat", "battle", "voice"):
            merged.setdefault(kind, [])
        return merged

    def _get_ticket(self, ticket_id):
        try:
            return self.tickets[ticket_id]
        except KeyError:
            raise ValueError("unknown ticket: %s" % ticket_id)

    def _cooldown_key(self, target_id):
        return target_id

    def _evidence_expires_at(self, ticket):
        anchor = ticket["created_at"]
        if ticket.get("decided_at") is not None:
            anchor = max(anchor, ticket["decided_at"])
        return anchor + self.config["evidence_retention"]

    def _notify_reporters(self, ticket, verdict, now):
        reporters = list(ticket["reporters"])
        for reporter_id in reporters:
            self.notifications.append({
                "ticket_id": ticket["ticket_id"],
                "reporter_id": reporter_id,
                "verdict": verdict,
                "at": now,
            })
        ticket["notified"] = reporters
        return reporters

    def _reputation(self, reporter_id):
        return self.reputations.get(reporter_id, 100)

    def file(self, reporter_id, target_id, match_id, evidence, now=0, server_id=None):
        resolved_target = self._resolve_target(target_id, server_id)
        index_key = (match_id, resolved_target)
        snapshot = self._snapshot_evidence(evidence)

        existing_id = self._ticket_index.get(index_key)
        if existing_id is not None:
            ticket = self.tickets[existing_id]
            if reporter_id not in ticket["reporters"]:
                ticket["reporters"].append(reporter_id)
            ticket["evidence"] = self._merge_evidence(
                ticket["evidence"], snapshot
            )
            ticket["aggregated"] = True
            ticket["updated_at"] = now
            result = copy.deepcopy(ticket)
            result["aggregated"] = True
            return result

        cooldown_key = self._cooldown_key(resolved_target)
        last_report = self._cooldowns.get(cooldown_key)
        if last_report is not None and now - last_report < self.config["cooldown"]:
            raise ValueError("reported player is still in cooldown")

        ticket_id = "ticket-%d" % self._next_ticket
        self._next_ticket += 1
        ticket = {
            "ticket_id": ticket_id,
            "match_id": match_id,
            "target_id": resolved_target,
            "reported_id": target_id,
            "server_id": server_id,
            "reporters": [reporter_id],
            "evidence": snapshot,
            "created_at": now,
            "updated_at": now,
            "status": "pending",
            "reviewer_id": None,
            "verdict": None,
            "punishment": None,
            "punishment_status": None,
            "decided_at": None,
            "appeal": None,
            "notified": [],
            "aggregated": False,
        }
        self.tickets[ticket_id] = ticket
        self._ticket_index[index_key] = ticket_id
        self._cooldowns[cooldown_key] = now
        return copy.deepcopy(ticket)

    def assign(self, ticket_id, reviewer_id):
        ticket = self._get_ticket(ticket_id)
        if ticket["reviewer_id"] is not None:
            if ticket["reviewer_id"] != reviewer_id:
                raise ValueError("ticket is assigned to another reviewer")
            return copy.deepcopy(ticket)
        if ticket["status"] not in ("pending", "assigned"):
            raise ValueError("ticket is already decided")

        ticket["reviewer_id"] = reviewer_id
        ticket["status"] = "assigned"
        return copy.deepcopy(ticket)

    def _revoke_punishment(self, ticket, now):
        if ticket.get("punishment") is not None:
            ticket["revoked_punishment"] = copy.deepcopy(ticket["punishment"])
        ticket["punishment"] = None
        ticket["punishment_status"] = "revoked"
        ticket["status"] = "overturned"
        ticket["updated_at"] = now

    def _apply_guilty(self, ticket, punishment, now):
        if not isinstance(punishment, dict):
            raise ValueError("guilty verdict requires a punishment")
        ticket["punishment"] = copy.deepcopy(punishment)
        ticket["punishment_status"] = "active"
        ticket["status"] = "decided"
        ticket["decided_at"] = now
        ticket["updated_at"] = now

        appeal = ticket.get("appeal")
        if appeal and appeal.get("frozen"):
            remaining = appeal.get("remaining")
            if remaining is not None:
                ticket["punishment"]["until"] = now + remaining
            appeal["status"] = "upheld"
            appeal["resolved_at"] = now

    def _apply_malicious(self, ticket, now):
        penalty = self.config["malicious_penalty"]
        updated = {}
        for reporter_id in ticket["reporters"]:
            current = self._reputation(reporter_id)
            current = max(0, current - penalty)
            self.reputations[reporter_id] = current
            updated[reporter_id] = current

        ticket["punishment"] = None
        ticket["punishment_status"] = None
        ticket["status"] = "malicious"
        ticket["decided_at"] = now
        ticket["updated_at"] = now
        ticket["reporter_reputations"] = copy.deepcopy(updated)
        ticket["reporter_reputation"] = (
            updated[ticket["reporters"][0]]
            if len(updated) == 1
            else copy.deepcopy(updated)
        )

    def decide(self, ticket_id, reviewer_id, verdict, punishment=None,
               idempotency_key=None, now=0, appeal=False):
        if idempotency_key is not None and idempotency_key in self._idempotency:
            result = copy.deepcopy(self._idempotency[idempotency_key])
            result["duplicate"] = True
            return result

        ticket = self._get_ticket(ticket_id)

        if appeal:
            if ticket.get("verdict") is None:
                raise ValueError("ticket has not been decided yet")
            ticket["appeal_reviewer_id"] = reviewer_id
        else:
            if ticket.get("verdict") is not None:
                raise ValueError("ticket has already been decided")
            if ticket["reviewer_id"] is not None and ticket["reviewer_id"] != reviewer_id:
                raise ValueError("ticket is assigned to another reviewer")
            if ticket["reviewer_id"] is None:
                ticket["reviewer_id"] = reviewer_id

        notified = self._notify_reporters(ticket, verdict, now)
        ticket["verdict"] = verdict
        if not appeal:
            ticket["reviewer_id"] = reviewer_id
        result = {
            "ticket_id": ticket_id,
            "reviewer_id": reviewer_id,
            "verdict": verdict,
            "punishment": None,
            "notified": notified,
            "duplicate": False,
        }

        if verdict in ("overturn", "overturned", "revoked"):
            self._revoke_punishment(ticket, now)
            result["revoked"] = True
        elif verdict in ("guilty", "accept", "accepted", "upheld"):
            if punishment is None and appeal and ticket.get("punishment") is not None:
                punishment = copy.deepcopy(ticket["punishment"])
            self._apply_guilty(ticket, punishment, now)
            result["punishment"] = copy.deepcopy(ticket["punishment"])
        elif verdict in ("malicious", "abusive"):
            self._apply_malicious(ticket, now)
            result["reporter_reputation"] = copy.deepcopy(
                ticket["reporter_reputation"]
            )
            result["reporter_reputations"] = copy.deepcopy(
                ticket["reporter_reputations"]
            )
        else:
            ticket["punishment"] = None
            ticket["punishment_status"] = None
            ticket["status"] = "dismissed"
            ticket["decided_at"] = now
            ticket["updated_at"] = now
            result["punishment"] = None

        result["status"] = ticket["status"]
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = copy.deepcopy(result)
        return result

    def appeal(self, ticket_id, reason, now=0):
        ticket = self._get_ticket(ticket_id)
        if ticket.get("verdict") is None:
            raise ValueError("ticket has not been decided yet")
        if ticket["status"] in ("overturned", "revoked") or (
            ticket.get("punishment_status") == "revoked"
        ):
            raise ValueError("ticket punishment has already been revoked")
        if ticket.get("appeal") and ticket["appeal"].get("status") == "pending":
            return copy.deepcopy(ticket)

        deadline = ticket["decided_at"] + self.config["appeal_window"]
        if now > deadline:
            raise ValueError("appeal window has closed")
        if now > self._evidence_expires_at(ticket):
            raise ValueError("evidence has expired")

        state = self.enforce(ticket_id, now=now)
        appeal_record = {
            "reason": reason,
            "at": now,
            "status": "pending",
            "frozen": False,
            "remaining": None,
        }

        if state["status"] == "active":
            punishment = ticket.get("punishment") or {}
            until = punishment.get("until")
            if until is None:
                appeal_record["frozen"] = True
                ticket["punishment_status"] = "frozen"
            elif until > now:
                appeal_record["frozen"] = True
                appeal_record["remaining"] = until - now
                ticket["punishment_status"] = "frozen"

        ticket["appeal"] = appeal_record
        ticket["status"] = "appealed"
        ticket["updated_at"] = now
        return copy.deepcopy(ticket)

    def enforce(self, ticket_id, now=0):
        ticket = self._get_ticket(ticket_id)
        status = ticket.get("punishment_status")
        punishment = ticket.get("punishment")

        if status == "revoked":
            return {
                "ticket_id": ticket_id,
                "active": False,
                "status": "revoked",
                "punishment": None,
            }
        if status == "frozen":
            return {
                "ticket_id": ticket_id,
                "active": True,
                "status": "frozen",
                "punishment": copy.deepcopy(punishment),
            }
        if status == "expired":
            return {
                "ticket_id": ticket_id,
                "active": False,
                "status": "expired",
                "punishment": copy.deepcopy(punishment),
            }
        if status == "active":
            until = (punishment or {}).get("until")
            if until is None or now < until:
                return {
                    "ticket_id": ticket_id,
                    "active": True,
                    "status": "active",
                    "punishment": copy.deepcopy(punishment),
                }
            ticket["punishment_status"] = "expired"
            if ticket["status"] == "decided":
                ticket["status"] = "expired"
            ticket["updated_at"] = now
            return {
                "ticket_id": ticket_id,
                "active": False,
                "status": "expired",
                "punishment": copy.deepcopy(punishment),
            }

        return {
            "ticket_id": ticket_id,
            "active": False,
            "status": ticket["status"],
            "punishment": None,
        }

    def batch_decide(self, decisions):
        results = []
        for decision in decisions:
            results.append(self.decide(
                decision["ticket_id"],
                decision["reviewer_id"],
                decision["verdict"],
                decision.get("punishment"),
                idempotency_key=decision.get("idempotency_key"),
                now=decision.get("now", 0),
                appeal=decision.get("appeal", False),
            ))
        return results
