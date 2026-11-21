import hashlib
import hmac
from collections import deque
from copy import deepcopy


MIN_DIGEST_LEN = 12
TIME_WINDOW_SECONDS = 150
AUDIT_LIMIT = 1000

REPORT_OIDS = {
    "1.3.6.1.6.3.15.1.1.1.0": "usmStatsUnsupportedSecLevels",
    "1.3.6.1.6.3.15.1.1.2.0": "usmStatsNotInTimeWindows",
    "1.3.6.1.6.3.15.1.1.3.0": "usmStatsUnknownUserNames",
    "1.3.6.1.6.3.15.1.1.4.0": "usmStatsUnknownEngineIDs",
    "1.3.6.1.6.3.15.1.1.5.0": "usmStatsWrongDigests",
    "1.3.6.1.6.3.15.1.1.6.0": "usmStatsDecryptionErrors",
}

DEFAULT_USERS = {
    "admin": {
        "auth_key": "admin-auth",
        "read_view": "1.3.6.1",
        "write_view": "1.3.6.1.4.1",
    },
    "ro": {
        "auth_key": "ro-auth",
        "read_view": "1.3.6.1.2.1",
        "write_view": None,
    },
}


def _oid_components(oid):
    return [int(part) for part in str(oid).split(".") if part]


class UsmGuard:
    """SNMPv3 USM engine discovery, authentication and view control."""

    def __init__(self, engine_id='8000000001020304', users=None,
                 max_bulk=100, trusted_engines=None):
        self.engine_id = engine_id
        self.users = deepcopy(users) if users is not None else deepcopy(DEFAULT_USERS)
        self.max_bulk = max_bulk
        self.trusted_engines = set(trusted_engines or ())
        self.known_engines = {engine_id}
        self.engine_boots = 1
        self.engine_time = 0
        self.audit = deque(maxlen=AUDIT_LIMIT)

    def _drop(self, reason, packet=None):
        entry = {"reason": reason}
        if isinstance(packet, dict):
            entry["user"] = packet.get("user")
            entry["engine_id"] = packet.get("engine_id")
        self.audit.append(entry)
        return entry

    def discover(self, response):
        engine_id = response.get("engine_id")
        if not engine_id or (
                engine_id != self.engine_id
                and engine_id not in self.trusted_engines):
            self._drop("untrusted engine", response)
            return {"accepted": False, "reason": "untrusted engine"}
        self.known_engines.add(engine_id)
        return {"accepted": True, "engine_id": engine_id}

    def _auth_failure(self, reason, message=None):
        self._drop(reason, message)
        return {"error": "authenticationFailure"}

    def authenticate(self, message):
        level = message.get("level")
        if level not in ("authNoPriv", "authPriv"):
            return self._auth_failure("authentication required", message)

        record = self.users.get(message.get("user"))
        if record is None:
            return self._auth_failure("unknown user", message)

        digest_len = message.get("digest_len", 0)
        digest = message.get("auth_digest", "")
        if (not isinstance(digest_len, int)
                or digest_len < MIN_DIGEST_LEN
                or len(digest) < MIN_DIGEST_LEN):
            return self._auth_failure("truncated digest", message)

        payload = message.get("payload", "")
        expected = hashlib.sha256(
            (record["auth_key"] + ":" + str(payload)).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(expected[:digest_len], digest):
            return self._auth_failure("wrong digest", message)

        if level == "authPriv" and not message.get("priv_params"):
            self._drop("empty privacy parameters", message)
            return {"error": "decryptionError"}

        return {"ok": True}

    def check_timeliness(self, message, now):
        boots = message.get("engine_boots")
        if boots is None or boots < self.engine_boots:
            self._drop("stale engine boots", message)
            return {"ok": False, "reason": "notInTimeWindow"}
        if boots == self.engine_boots:
            engine_time = message.get("engine_time", 0)
            if abs(now - engine_time) > TIME_WINDOW_SECONDS:
                self._drop("message outside time window", message)
                return {"ok": False, "reason": "notInTimeWindow"}
        return {"ok": True}

    def advance_boot(self):
        self.engine_boots += 1
        self.engine_time = 0
        return self.engine_boots

    @staticmethod
    def _oid_within(view, oid):
        base = _oid_components(view)
        target = _oid_components(oid)
        return target[:len(base)] == base

    def _view_allows(self, user, oid, write):
        record = self.users.get(user)
        if record is None:
            return False
        view = record["write_view"] if write else record["read_view"]
        if not view:
            return False
        return self._oid_within(view, oid)

    def authorize_view(self, user, oid, write=False):
        allowed = self._view_allows(user, oid, write)
        if not allowed:
            self._drop(
                "write view denied" if write else "read view denied",
                {"user": user, "engine_id": self.engine_id},
            )
        return allowed

    def get_bulk(self, user, oid, repetitions):
        if (not isinstance(repetitions, int)
                or repetitions < 1
                or repetitions > self.max_bulk):
            self._drop("max repetitions exceeded",
                       {"user": user, "engine_id": self.engine_id})
            return {"ok": False, "reason": "max repetitions exceeded"}
        if not self.authorize_view(user, oid, write=False):
            return {"ok": False, "reason": "notInView"}
        return {"ok": True, "repetitions": repetitions}

    def match_response(self, request_id, response):
        if response.get("msg_id") != request_id:
            self._drop("response msg_id mismatch", response)
            return {"ok": False, "reason": "msgID mismatch"}
        if response.get("engine_id") != self.engine_id:
            self._drop("response engine mismatch", response)
            return {"ok": False, "reason": "engineID mismatch"}
        return {"ok": True, "payload": response.get("payload")}

    def handle_report(self, report):
        kind = REPORT_OIDS.get(report.get("oid"))
        if kind is None:
            self._drop("unknown report oid", report)
            return {"ok": False, "reason": "unknown report oid"}
        return {"ok": True, "kind": kind}

    def build_notification(self, kind):
        if kind not in ("trap", "inform"):
            raise ValueError("unsupported notification kind: %r" % (kind,))
        return {"kind": kind, "engine_id": self.engine_id}

    def export_state(self):
        return {
            "engine_id": self.engine_id,
            "engine_boots": self.engine_boots,
            "engine_time": self.engine_time,
        }

    def restore_state(self, state):
        if state.get("engine_id", self.engine_id) != self.engine_id:
            self._drop("state belongs to another engine", state)
            return
        self.engine_boots = int(state["engine_boots"])
        self.engine_time = int(state.get("engine_time", 0))

    def snapshot(self):
        return {
            "engine_id": self.engine_id,
            "engine_boots": self.engine_boots,
            "engine_time": self.engine_time,
            "trusted_engines": sorted(self.trusted_engines),
            "users": sorted(self.users),
            "audit": list(self.audit),
        }
