class CreditError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


_FINAL_UNIT_ACTIONS = {"REDIRECT", "RESTRICT_ACCESS", "TERMINATE"}


class CreditGuard:
    def __init__(self, session_timeout=60, max_retries=2):
        self.session_timeout = session_timeout
        self.max_retries = max_retries
        self._sessions = {}
        self._log = []

    def credit_control(self, request):
        self._validate_avps(request.get("avps") or [])
        kind = request.get("type")
        if kind == "INITIAL":
            return self.create_session(request)
        if kind == "UPDATE":
            return self.update_session(request)
        if kind in ("TERMINATE", "TERMINATION"):
            return self.terminate_session(request)
        raise CreditError("unknown_request_type", detail=kind)

    def create_session(self, request):
        session_id = request["session_id"]
        existing = self._sessions.get(session_id)
        if existing is not None:
            return self._replay(existing, request["request_number"])
        for sess in self._sessions.values():
            if sess["subscriber"] == request["subscriber"]:
                return self._replay(sess, 0)
        if not self._success(request["result_code"]):
            return self._result(request, granted=0, allowed=False)
        final_unit = request.get("final_unit")
        barred = bool(final_unit and final_unit.get("action") in _FINAL_UNIT_ACTIONS)
        granted = 0 if barred else sum(request.get("granted_units") or [])
        sess = {
            "session_id": session_id,
            "subscriber": request["subscriber"],
            "balance": granted,
            "requests": {},
            "pending": {},
            "barred": barred,
            "final_action": final_unit.get("action") if final_unit else None,
            "redirect_uri": final_unit.get("redirect") if final_unit else None,
            "last_activity": request.get("now", 0.0),
            "last_request_number": -1,
        }
        self._sessions[session_id] = sess
        result = self._result(request, granted=granted, allowed=not barred)
        result["final_action"] = sess["final_action"]
        result["redirect_uri"] = sess["redirect_uri"]
        self._commit(sess, request, result)
        return result

    def update_session(self, request):
        sess = self._sessions.get(request["session_id"])
        if sess is None:
            raise CreditError("unknown_session", detail=request["session_id"])
        number = request["request_number"]
        if number in sess["requests"]:
            return self._replay(sess, number)
        if not self._success(request["result_code"]):
            result = self._result(request, granted=0, allowed=False)
            self._commit(sess, request, result)
            return result
        if sess["barred"]:
            result = self._result(request, granted=0, allowed=False)
            result["final_action"] = sess["final_action"]
            result["redirect_uri"] = sess["redirect_uri"]
            self._commit(sess, request, result)
            return result
        granted = sum(request.get("granted_units") or [])
        used = request.get("used_units") or 0
        new_balance = sess["balance"] + granted - used
        allowed = new_balance >= 0
        sess["balance"] = max(0, new_balance)
        result = self._result(request, granted=granted, allowed=allowed)
        self._commit(sess, request, result)
        return result

    def terminate_session(self, request):
        self._sessions.pop(request["session_id"], None)
        return self._result(request, granted=0, allowed=False)

    def match_response(self, response):
        sess = self._sessions.get(response.get("session_id"))
        if sess is None:
            return None
        key = (response.get("hop_id"), response.get("end_id"))
        return sess["pending"].pop(key, None)

    def available(self, subscriber):
        return sum(
            sess["balance"]
            for sess in self._sessions.values()
            if sess["subscriber"] == subscriber
        )

    def session(self, session_id):
        return self._sessions.get(session_id)

    def active_sessions(self):
        return sorted(self._sessions)

    def cleanup(self, now):
        expired = [
            session_id
            for session_id, sess in self._sessions.items()
            if now - sess["last_activity"] > self.session_timeout
        ]
        for session_id in expired:
            del self._sessions[session_id]
        return len(expired)

    def export_state(self):
        sessions = []
        for sess in self._sessions.values():
            sessions.append({
                "session_id": sess["session_id"],
                "subscriber": sess["subscriber"],
                "balance": sess["balance"],
                "barred": sess["barred"],
                "final_action": sess["final_action"],
                "redirect_uri": sess["redirect_uri"],
                "last_activity": sess["last_activity"],
                "last_request_number": sess["last_request_number"],
                "requests": {str(n): dict(r) for n, r in sess["requests"].items()},
                "pending": [dict(rec) for rec in sess["pending"].values()],
            })
        return {
            "session_timeout": self.session_timeout,
            "sessions": sessions,
            "log": [dict(entry) for entry in self._log],
        }

    def import_state(self, state):
        self._sessions = {}
        for data in state.get("sessions", []):
            sess = dict(data)
            sess["requests"] = {
                int(n): dict(r) for n, r in data.get("requests", {}).items()
            }
            sess["pending"] = {
                (rec["hop_id"], rec["end_id"]): dict(rec)
                for rec in data.get("pending", [])
            }
            self._sessions[sess["session_id"]] = sess
        self._log = [dict(entry) for entry in state.get("log", [])]

    def log(self):
        return [dict(entry) for entry in self._log]

    @staticmethod
    def _success(result_code):
        return 2000 <= result_code < 3000

    @staticmethod
    def _validate_avps(avps):
        for avp in avps:
            vendor = avp.get("vendor", 0)
            length = avp.get("length")
            value = avp.get("value", b"")
            header = 12 if vendor else 8
            if (not isinstance(length, int) or length < header
                    or length - header != len(value)):
                raise CreditError("malformed_avp", detail=avp.get("code"))

    @staticmethod
    def _result(request, granted, allowed):
        return {
            "session_id": request["session_id"],
            "result_code": request["result_code"],
            "granted_units": granted,
            "allowed": allowed,
            "existing": False,
            "final_action": None,
            "redirect_uri": None,
        }

    @staticmethod
    def _replay(sess, number):
        result = dict(sess["requests"][number])
        result["existing"] = True
        return result

    def _commit(self, sess, request, result):
        number = request["request_number"]
        sess["requests"][number] = dict(result)
        sess["pending"][(request["hop_id"], request["end_id"])] = {
            "session_id": sess["session_id"],
            "request_number": number,
            "hop_id": request["hop_id"],
            "end_id": request["end_id"],
        }
        sess["last_request_number"] = max(sess["last_request_number"], number)
        sess["last_activity"] = request.get("now", sess["last_activity"])
        self._log.append({
            "session_id": sess["session_id"],
            "subscriber": sess["subscriber"],
            "request_number": number,
            "type": request["type"],
            "granted": result["granted_units"],
            "used": request.get("used_units") or 0,
            "result_code": request["result_code"],
            "node": request.get("node"),
            "now": request.get("now", 0.0),
        })
