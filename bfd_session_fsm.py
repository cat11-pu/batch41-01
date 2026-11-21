import json

MIN_DETECT_MULT = 2
DEFAULT_DETECT_MULT = 3
DEFAULT_MIN_TX = 100
DEFAULT_MIN_RX = 100
MULTI_HOP_MIN_TTL = 250
FLAP_THRESHOLD = 3


class SessionFsm:
    def __init__(self):
        self.sessions = {}

    def _get(self, session_id):
        if session_id not in self.sessions:
            raise KeyError("unknown session: %s" % session_id)
        return self.sessions[session_id]

    def open(self, session_id, config, now):
        if session_id in self.sessions:
            raise ValueError("duplicate session id: %s" % session_id)
        detect_mult = config.get("detect_mult", DEFAULT_DETECT_MULT)
        if detect_mult < MIN_DETECT_MULT:
            raise ValueError("detect_mult must be >= %d" % MIN_DETECT_MULT)
        discriminator = config.get("discriminator")
        if discriminator is not None:
            for other in self.sessions.values():
                if other["discriminator"] == discriminator:
                    raise ValueError(
                        "duplicate local discriminator: %s" % discriminator
                    )
        desired_min_tx = config.get("desired_min_tx", DEFAULT_MIN_TX)
        required_min_rx = config.get("required_min_rx", DEFAULT_MIN_RX)
        self.sessions[session_id] = {
            "id": session_id,
            "state": "down",
            "admin_down": False,
            "discriminator": discriminator,
            "remote_discriminator": None,
            "desired_min_tx": desired_min_tx,
            "required_min_rx": required_min_rx,
            "negotiated_tx": desired_min_tx,
            "negotiated_rx": required_min_rx,
            "detect_mult": detect_mult,
            "multi_hop": bool(config.get("multi_hop", False)),
            "echo_interface": config.get("echo_interface"),
            "auth_keys": {},
            "last_rx": None,
            "last_final": False,
            "flaps": [],
            "opened_at": now,
        }
        return {
            "state": "down",
            "negotiated_tx": desired_min_tx,
            "negotiated_rx": required_min_rx,
            "local_discriminator": discriminator,
        }

    def _reject(self, session, reason):
        return {
            "accepted": False,
            "reason": reason,
            "state": session["state"],
            "final": False,
        }

    def _key_valid(self, session, key_id, now):
        if key_id is None:
            return False
        retired_at = session["auth_keys"].get(str(key_id), "missing")
        if retired_at == "missing":
            return False
        return retired_at is None or now <= retired_at

    def _transition(self, session, remote_state, now):
        current = session["state"]
        if current == "down":
            if remote_state == "down":
                session["state"] = "init"
            elif remote_state == "init":
                session["state"] = "up"
        elif current == "init":
            if remote_state == "init":
                session["state"] = "up"
        elif current == "up":
            if remote_state == "down":
                session["state"] = "down"
                session["flaps"].append(now)

    def recv(self, session_id, packet, now, interface=None):
        session = self._get(session_id)
        if session["auth_keys"]:
            if not self._key_valid(session, packet.get("auth_key_id"), now):
                return self._reject(session, "auth")
        if session["multi_hop"] and packet.get("ttl", 255) < MULTI_HOP_MIN_TTL:
            return self._reject(session, "ttl")
        if packet.get("echo"):
            expected = session["echo_interface"]
            if expected is not None and interface != expected:
                return self._reject(session, "echo_interface")
            session["last_rx"] = now
            return {"accepted": True, "state": session["state"], "final": False}
        last_rx = session["last_rx"]
        if last_rx is not None and now - last_rx < session["negotiated_rx"]:
            return self._reject(session, "rx_too_fast")
        session["last_rx"] = now
        session["remote_discriminator"] = packet.get("discriminator")
        session["negotiated_tx"] = max(
            session["desired_min_tx"], packet.get("min_rx", 0)
        )
        session["negotiated_rx"] = max(
            session["required_min_rx"], packet.get("min_tx", 0)
        )
        if not session["admin_down"]:
            self._transition(session, packet.get("state"), now)
        final = bool(packet.get("poll"))
        if final:
            session["last_final"] = True
        return {"accepted": True, "state": session["state"], "final": final}

    def tick(self, session_id, now):
        session = self._get(session_id)
        if not session["admin_down"] and session["state"] in ("init", "up"):
            last_rx = session["last_rx"]
            if last_rx is not None:
                timeout = session["detect_mult"] * session["negotiated_rx"]
                if now - last_rx > timeout:
                    if session["state"] == "up":
                        session["flaps"].append(now)
                    session["state"] = "down"
        return {"state": session["state"]}

    def admin_down(self, session_id, now):
        session = self._get(session_id)
        session["state"] = "down"
        session["admin_down"] = True
        return {"state": "down"}

    def admin_up(self, session_id, now):
        session = self._get(session_id)
        session["admin_down"] = False
        session["state"] = "init"
        session["last_rx"] = None
        return {"state": "init"}

    def rotate_auth(self, session_id, key_id, now, overlap=30):
        session = self._get(session_id)
        for existing, retired_at in session["auth_keys"].items():
            if retired_at is None:
                session["auth_keys"][existing] = now + overlap
        session["auth_keys"][str(key_id)] = None
        return {"active_key": key_id}

    def state(self, session_id):
        session = self._get(session_id)
        return {
            "state": session["state"],
            "admin_down": session["admin_down"],
            "local_discriminator": session["discriminator"],
            "remote_discriminator": session["remote_discriminator"],
            "negotiated_tx": session["negotiated_tx"],
            "negotiated_rx": session["negotiated_rx"],
            "detect_mult": session["detect_mult"],
            "last_final": session["last_final"],
        }

    def damped(self, session_id, now, window=10):
        session = self._get(session_id)
        recent = [
            flap for flap in session["flaps"] if now - window <= flap <= now
        ]
        return len(recent) >= FLAP_THRESHOLD

    def flush(self, path):
        with open(path, "w") as handle:
            json.dump({"sessions": self.sessions}, handle)

    @classmethod
    def load(cls, path):
        with open(path) as handle:
            data = json.load(handle)
        store = cls()
        store.sessions = data["sessions"]
        return store

    def snapshot(self):
        return {
            "sessions": len(self.sessions),
            "up": sum(
                1 for s in self.sessions.values() if s["state"] == "up"
            ),
            "init": sum(
                1 for s in self.sessions.values() if s["state"] == "init"
            ),
            "down": sum(
                1 for s in self.sessions.values() if s["state"] == "down"
            ),
            "admin_down": sum(
                1 for s in self.sessions.values() if s["admin_down"]
            ),
        }
