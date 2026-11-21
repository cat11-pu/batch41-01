import copy
import secrets


DEFAULT_POLICIES = [
    {"id": "patch", "kind": "patch_age", "weight": 30, "required": True},
    {"id": "disk_encryption", "kind": "boolean", "signal": "disk_encrypted",
     "weight": 30, "required": True},
    {"id": "edr", "kind": "boolean", "signal": "edr_running", "weight": 20,
     "required": True},
    {"id": "network", "kind": "network", "weight": 20, "required": True},
]


class PostureLoop:
    def __init__(self, ttl=300, offline_grace=3600, max_patch_days=5,
                 token_risk_drop=40, policies=None):
        self.ttl = ttl
        self.offline_grace = offline_grace
        self.max_patch_days = max_patch_days
        self.token_risk_drop = token_risk_drop
        self.policies = copy.deepcopy(policies) if policies else copy.deepcopy(DEFAULT_POLICIES)
        self.policy_version = 1
        self.devices = {}
        self.user_devices = {}
        self.cache = {}
        self.tokens = {}
        self.events = []
        self.evaluations = 0

    def _audit(self, entry):
        self.events.append(copy.deepcopy(entry))

    def _required_signal(self, policy):
        kind = policy["kind"]
        if kind == "patch_age":
            return "patched_at"
        if kind == "boolean":
            return policy.get("signal")
        if kind == "network":
            return "network"
        return None

    def _policy_passes(self, policy, signals, now):
        kind = policy["kind"]
        if kind == "patch_age":
            return now - signals["patched_at"] <= self.max_patch_days * 86400
        if kind == "boolean":
            return bool(signals.get(policy["signal"]))
        if kind == "network":
            return signals.get("network") == "corp"
        return False

    def _revoke_user_tokens(self, user_id, reason):
        for token in self.tokens.values():
            if token["user_id"] == user_id and not token["revoked"]:
                token["revoked"] = True
                token["revoke_reason"] = reason

    def _downgrade_user_tokens(self, user_id):
        for token in self.tokens.values():
            if token["user_id"] == user_id and not token["revoked"]:
                token["downgraded"] = True

    def report(self, device_id, user_id, signals, now, snapshot=None):
        if not signals:
            raise ValueError("empty posture report")

        device = self.devices.get(device_id)
        previous_snapshot = device["snapshot"] if device else 0
        if snapshot is None:
            snapshot = previous_snapshot + 1
        elif snapshot <= previous_snapshot:
            raise ValueError("snapshot version must be monotonically increasing")

        self.devices[device_id] = {
            "user_id": user_id,
            "signals": copy.deepcopy(signals),
            "snapshot": snapshot,
            "reported_at": now,
            "last_score": device["last_score"] if device else None,
        }
        self.user_devices.setdefault(user_id, [])
        if device_id not in self.user_devices[user_id]:
            self.user_devices[user_id].append(device_id)
        self.cache.pop(device_id, None)
        self._audit({
            "type": "report",
            "device_id": device_id,
            "user_id": user_id,
            "snapshot": snapshot,
            "policy_version": self.policy_version,
            "at": now,
        })

    def evaluate(self, device_id, now, timed_out=False):
        if device_id not in self.devices:
            raise ValueError("unknown device")

        device = self.devices[device_id]
        snapshot = device["snapshot"]

        if timed_out:
            result = {
                "device_id": device_id,
                "snapshot": snapshot,
                "policy_version": self.policy_version,
                "score": 0,
                "compliant": False,
                "reason": "timeout",
                "failed": [],
                "missing": [],
                "cached": False,
                "token_action": "keep",
            }
            self._audit(dict(result, type="evaluate", at=now))
            return copy.deepcopy(result)

        cached = self.cache.get(device_id)
        if (cached is not None
                and cached["snapshot"] == snapshot
                and cached["policy_version"] == self.policy_version
                and now - cached["_at"] <= self.ttl):
            return copy.deepcopy(dict(cached, cached=True))

        signals = device["signals"]
        missing = []
        for policy in self.policies:
            if not policy.get("required"):
                continue
            required_signal = self._required_signal(policy)
            if required_signal is not None and required_signal not in signals:
                missing.append(required_signal)

        failed = []
        if missing:
            failed = [policy["id"] for policy in self.policies
                      if policy.get("required")
                      and self._required_signal(policy) in missing]
            score = 0
            compliant = False
            reason = "missing_signal"
        else:
            score = 0
            for policy in self.policies:
                if self._policy_passes(policy, signals, now):
                    score += policy["weight"]
                else:
                    failed.append(policy["id"])
            reason = None
            if signals.get("offline_seconds", 0) > self.offline_grace:
                compliant = False
                reason = "offline"
            else:
                compliant = not failed

        previous_score = device["last_score"]
        if previous_score is None:
            token_action = "keep"
        elif previous_score - score >= self.token_risk_drop:
            token_action = "revoke"
            self._revoke_user_tokens(device["user_id"], "risk_jump")
        elif previous_score - score > 0:
            token_action = "downgrade"
            self._downgrade_user_tokens(device["user_id"])
        else:
            token_action = "keep"
        device["last_score"] = score

        result = {
            "device_id": device_id,
            "snapshot": snapshot,
            "policy_version": self.policy_version,
            "score": score,
            "compliant": compliant,
            "reason": reason,
            "failed": failed,
            "missing": missing,
            "cached": False,
            "token_action": token_action,
        }
        self.evaluations += 1
        self.cache[device_id] = copy.deepcopy(dict(result, _at=now))
        self._audit(dict(result, type="evaluate", at=now))
        return copy.deepcopy(result)

    def aggregate(self, user_id, now):
        device_ids = list(self.user_devices.get(user_id, ()))
        results = [(device_id, self.evaluate(device_id, now))
                   for device_id in device_ids]
        if not results:
            return {"devices": 0, "score": 0, "compliant": False,
                    "worst_device": None}
        worst_device, worst = min(results, key=lambda item: item[1]["score"])
        return {
            "devices": len(device_ids),
            "score": worst["score"],
            "compliant": worst["compliant"],
            "worst_device": worst_device,
        }

    def issue_token(self, user_id, now):
        token = secrets.token_hex(16)
        record = {
            "user_id": user_id,
            "revoked": False,
            "downgraded": False,
            "policy_version": self.policy_version,
            "issued_at": now,
        }
        self.tokens[token] = record
        self._audit({"type": "issue_token", "user_id": user_id,
                     "token": token, "policy_version": self.policy_version,
                     "at": now})
        return {"token": token, "user_id": user_id,
                "policy_version": self.policy_version}

    def token_status(self, token):
        if token not in self.tokens:
            raise ValueError("unknown token")
        record = self.tokens[token]
        return {
            "token": token,
            "user_id": record["user_id"],
            "revoked": record["revoked"],
            "downgraded": record["downgraded"],
            "policy_version": record["policy_version"],
        }

    def upgrade_policy(self, policies, now):
        self.policies = copy.deepcopy(policies)
        self.policy_version += 1
        self.cache.clear()
        for record in self.tokens.values():
            if not record["revoked"]:
                record["revoked"] = True
                record["revoke_reason"] = "policy_upgrade"
        self._audit({"type": "policy_upgrade",
                     "policy_version": self.policy_version, "at": now})

    def audit(self):
        return copy.deepcopy(self.events)

    def snapshot(self):
        return {
            "evaluations": self.evaluations,
            "devices": len(self.devices),
            "policy_version": self.policy_version,
            "ttl": self.ttl,
            "offline_grace": self.offline_grace,
            "max_patch_days": self.max_patch_days,
            "token_risk_drop": self.token_risk_drop,
            "policies": copy.deepcopy(self.policies),
            "device_states": copy.deepcopy(self.devices),
            "user_devices": copy.deepcopy(self.user_devices),
            "tokens": copy.deepcopy(self.tokens),
            "events": copy.deepcopy(self.events),
        }
