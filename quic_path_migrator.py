import copy
import secrets


class PathMigrator:
    """Server-side QUIC path migration state machine."""

    _PN_WINDOW = 4096

    def __init__(self, migration_enabled=True, validation_timeout=5.0, amplification_factor=3):
        self._migration_enabled = bool(migration_enabled)
        self._validation_timeout = max(0.0, float(validation_timeout))
        self._amplification_factor = max(0, amplification_factor)
        self._paths = {}
        self._active_path_id = None
        self._cid_routes = {}
        self._zero_rtt_seen = set()
        self._epoch = 0
        self._last_now = 0.0
        self._candidate_seq = 0

    def add_path(self, path_id, addr, cwnd=10.0, rtt=0.05, worker='worker-1'):
        normalized_addr = self._normalize_addr(addr)
        if path_id in self._paths:
            path = self._paths[path_id]
            if path["state"] != "active":
                if normalized_addr != path["addr"]:
                    path["addr"] = normalized_addr
                    path.update({
                        "state": "new",
                        "token": None,
                        "challenge_at": None,
                        "deadline": None,
                        "challenge_epoch": None,
                        "validated_at": None,
                    })
                    self._reset_amplification(path)
                    self._reset_pn(path)
            path["cwnd"] = float(cwnd)
            path["rtt"] = float(rtt)
            path["worker"] = worker
            return self._path_view(path_id)

        self._paths[path_id] = self._new_path(normalized_addr, cwnd, rtt, worker)
        return self._path_view(path_id)

    def activate(self, path_id):
        path = self._paths.get(path_id)
        if path is None:
            return False

        previous = self._active_path_id
        if previous is not None and previous != path_id:
            old_path = self._paths.get(previous)
            if old_path is not None:
                old_path["state"] = "inactive"
            self._epoch += 1

        path["state"] = "active"
        path["validated_at"] = None
        path["deadline"] = None
        self._active_path_id = path_id
        self._reset_pn(path)
        for dcid in list(self._cid_routes):
            self._cid_routes[dcid] = path_id
        return True

    def active_path(self):
        return self._active_path_id

    def path_addr(self, path_id):
        path = self._paths.get(path_id)
        if path is None:
            return None
        return path["addr"]

    def path_state(self, path_id):
        path = self._paths.get(path_id)
        if path is None:
            return None
        return path["state"]

    def _challenge(self, path_id, now, addr=None):
        path = self._paths.get(path_id)
        if path is None:
            return {"action": "reject", "path_id": path_id, "reason": "unknown path"}

        normalized_addr = path["addr"] if addr is None else self._normalize_addr(addr)
        if normalized_addr != path["addr"]:
            path["addr"] = normalized_addr
            self._reset_amplification(path)
            self._reset_pn(path)

        now = max(self._last_now, float(now))
        self._last_now = now
        token = secrets.token_urlsafe(24)
        path.update({
            "state": "validating",
            "token": token,
            "challenge_at": now,
            "deadline": now + self._validation_timeout,
            "challenge_epoch": self._epoch,
            "validated_at": None,
        })
        return {
            "action": "challenge",
            "path_id": path_id,
            "token": token,
            "addr": normalized_addr,
        }

    def on_packet(self, packet):
        path_id = packet.get("path_id")
        if path_id is None:
            return {"action": "reject", "reason": "missing path id"}

        now = float(packet.get("now", self._last_now))
        now = max(self._last_now, now)
        self._last_now = now
        self._expire_validations(now)

        addr_value = packet.get("addr")
        if addr_value is None:
            path = self._paths.get(path_id)
            addr = path["addr"] if path is not None else None
        else:
            addr = self._normalize_addr(addr_value)

        zero_rtt_id = packet.get("zero_rtt_id")
        if zero_rtt_id is not None and zero_rtt_id in self._zero_rtt_seen:
            return {
                "action": "replay_dropped",
                "path_id": path_id,
                "reason": "duplicate 0-rtt payload",
            }

        if path_id not in self._paths:
            if not self._migration_enabled:
                return {
                    "action": "reject",
                    "path_id": path_id,
                    "reason": "migration disabled",
                }
            if addr is None:
                return {"action": "reject", "path_id": path_id, "reason": "missing address"}
            worker = self._active_worker()
            self.add_path(path_id, addr, worker=worker)

        path = self._paths[path_id]
        current_addr = path["addr"]
        if addr is None:
            addr = current_addr

        if addr != current_addr:
            if not self._migration_enabled:
                if path["state"] != "active":
                    path["state"] = "rejected"
                return {
                    "action": "reject",
                    "path_id": path_id,
                    "reason": "migration disabled",
                }

            if path["state"] == "active" and self._same_host(addr, current_addr):
                path["addr"] = addr
                self._reset_amplification(path)
                self._reset_pn(path)
                if zero_rtt_id is not None:
                    self._zero_rtt_seen.add(zero_rtt_id)
                return {"action": "nat_rebind", "path_id": path_id, "addr": addr}

            if path["state"] == "active":
                candidate_id = self._new_candidate_id(path_id)
                self.add_path(
                    candidate_id,
                    addr,
                    cwnd=path["cwnd"],
                    rtt=path["rtt"],
                    worker=path["worker"],
                )
                return self._challenge(candidate_id, now)

            return self._challenge(path_id, now, addr)

        state = path["state"]
        if state == "active":
            return self._accept_active_packet(path_id, zero_rtt_id, packet.get("pn"))

        if state == "validating":
            if path["token"] is None:
                return self._challenge(path_id, now)
            return {
                "action": "challenge",
                "path_id": path_id,
                "token": path["token"],
                "addr": addr,
            }

        if state == "validated":
            return {
                "action": "pending",
                "path_id": path_id,
                "token": path["token"],
                "addr": addr,
            }

        if state in ("stale", "failed", "rejected", "inactive", "new"):
            if not self._migration_enabled:
                path["state"] = "rejected"
                return {
                    "action": "reject",
                    "path_id": path_id,
                    "reason": "migration disabled",
                }
            return self._challenge(path_id, now)

        return {"action": "reject", "path_id": path_id, "reason": "invalid path state"}

    def validate(self, path_id, token):
        path = self._paths.get(path_id)
        if path is None:
            return {"validated": False, "path_id": path_id, "reason": "unknown path"}

        if path["state"] == "stale":
            return {"validated": False, "path_id": path_id, "reason": "stale candidate"}

        if path["state"] not in ("validating", "validated"):
            return {"validated": False, "path_id": path_id, "reason": "not validating"}

        if path["challenge_epoch"] != self._epoch:
            path["state"] = "stale"
            return {"validated": False, "path_id": path_id, "reason": "stale candidate"}

        if token is None or token != path["token"]:
            return {"validated": False, "path_id": path_id, "reason": "invalid token"}

        if path["state"] == "validating":
            path["state"] = "validated"
            path["validated_at"] = self._last_now

        return {
            "validated": True,
            "path_id": path_id,
            "state": path["state"],
            "addr": path["addr"],
        }

    def commit(self, path_id):
        path = self._paths.get(path_id)
        if path is None:
            return {"committed": False, "path_id": path_id, "reason": "unknown path"}

        if path["state"] == "active":
            return {"committed": False, "path_id": path_id, "reason": "already active"}

        if path["state"] == "stale":
            return {"committed": False, "path_id": path_id, "reason": "stale candidate"}

        if path["state"] != "validated":
            return {"committed": False, "path_id": path_id, "reason": "not validated"}

        if path["challenge_epoch"] != self._epoch:
            path["state"] = "stale"
            return {"committed": False, "path_id": path_id, "reason": "stale candidate"}

        previous = self._active_path_id
        if previous is not None and previous != path_id:
            old_path = self._paths.get(previous)
            if old_path is not None:
                old_path["state"] = "inactive"

        path["state"] = "active"
        path["deadline"] = None
        self._active_path_id = path_id
        self._reset_pn(path)
        self._epoch += 1

        for other_id, other in self._paths.items():
            if other_id == path_id:
                continue
            if other["state"] in ("validating", "validated") and (
                other["challenge_epoch"] is None
                or other["challenge_epoch"] < self._epoch
            ):
                other["state"] = "stale"

        for dcid in list(self._cid_routes):
            self._cid_routes[dcid] = path_id

        return {
            "committed": True,
            "path_id": path_id,
            "previous": previous,
            "active": path_id,
        }

    def check_timeout(self, now):
        now = max(self._last_now, float(now))
        self._last_now = now
        failed = self._expire_validations(now)
        return {
            "rolled_back": bool(failed),
            "failed": failed,
            "active": self._active_path_id,
        }

    def send_budget(self, path_id):
        path = self._paths.get(path_id)
        if path is None:
            return 0
        if path["state"] in ("validated", "active"):
            return float("inf")
        if path["state"] != "validating":
            return 0
        limit = self._amplification_factor * path["received"]
        return max(0, limit - path["sent"])

    def record_received(self, path_id, nbytes):
        path = self._paths.get(path_id)
        if path is None:
            return 0
        amount = max(0, nbytes)
        if path["state"] == "validating":
            path["received"] += amount
        return path["received"]

    def send(self, path_id, nbytes):
        path = self._paths.get(path_id)
        if path is None:
            return 0
        amount = max(0, nbytes)
        if path["state"] in ("validated", "active"):
            path["sent"] += amount
            return amount
        if path["state"] != "validating":
            return 0
        allowed = min(amount, self.send_budget(path_id))
        path["sent"] += allowed
        return allowed

    def congestion(self):
        path = self._paths.get(self._active_path_id)
        if path is None:
            return {"path_id": None, "cwnd": 0.0, "rtt": 0.0}
        return {
            "path_id": self._active_path_id,
            "cwnd": path["cwnd"],
            "rtt": path["rtt"],
        }

    def register_cid(self, dcid, path_id):
        self._cid_routes[dcid] = path_id
        return True

    def route(self, dcid):
        path_id = self._cid_routes.get(dcid, self._active_path_id)
        path = self._paths.get(path_id)
        if path is None:
            path = self._paths.get(self._active_path_id)
        if path is None:
            return None
        return path["worker"]

    def accept_pn(self, path_id, pn):
        path = self._paths.get(path_id)
        if path is None or pn is None:
            return False
        try:
            pn = int(pn)
        except (TypeError, ValueError):
            return False
        if pn < 0:
            return False

        seen = path["pn_seen"]
        largest = path["pn_largest"]
        if pn in seen:
            return False
        if largest is not None and pn < largest - self._PN_WINDOW:
            return False

        seen.add(pn)
        if largest is None or pn > largest:
            path["pn_largest"] = pn
        self._prune_pn(path)
        return True

    def offer_preferred(self, path_id, addr, cwnd=20.0, rtt=0.03, worker='worker-1'):
        if not self._migration_enabled:
            return {
                "action": "reject",
                "path_id": path_id,
                "reason": "migration disabled",
            }

        if path_id not in self._paths:
            self.add_path(path_id, addr, cwnd=cwnd, rtt=rtt, worker=worker)
        else:
            path = self._paths[path_id]
            normalized_addr = self._normalize_addr(addr)
            if path["state"] != "active" and normalized_addr != path["addr"]:
                path["addr"] = normalized_addr
                self._reset_amplification(path)
                self._reset_pn(path)
            path["cwnd"] = float(cwnd)
            path["rtt"] = float(rtt)
            path["worker"] = worker

        return self._challenge(path_id, self._last_now)

    def export_state(self):
        paths = {}
        for path_id, path in self._paths.items():
            exported = copy.deepcopy(path)
            if isinstance(path["addr"], (list, tuple)):
                exported["addr"] = list(path["addr"])
            exported["pn_seen"] = sorted(path["pn_seen"])
            paths[path_id] = exported

        return {
            "version": 1,
            "migration_enabled": self._migration_enabled,
            "validation_timeout": self._validation_timeout,
            "amplification_factor": self._amplification_factor,
            "active_path": self._active_path_id,
            "epoch": self._epoch,
            "last_now": self._last_now,
            "candidate_seq": self._candidate_seq,
            "paths": paths,
            "cid_routes": dict(self._cid_routes),
            "zero_rtt_seen": list(self._zero_rtt_seen),
        }

    def load_state(self, state):
        if not isinstance(state, dict):
            raise TypeError("state must be a dictionary")

        self._migration_enabled = bool(
            state.get("migration_enabled", self._migration_enabled)
        )
        self._validation_timeout = max(
            0.0,
            float(state.get("validation_timeout", self._validation_timeout)),
        )
        self._amplification_factor = max(
            0,
            state.get("amplification_factor", self._amplification_factor),
        )
        self._active_path_id = state.get("active_path")
        self._epoch = int(state.get("epoch", 0))
        self._last_now = float(state.get("last_now", 0.0))
        self._candidate_seq = int(state.get("candidate_seq", 0))
        self._cid_routes = dict(state.get("cid_routes", {}))
        self._zero_rtt_seen = set(state.get("zero_rtt_seen", []))
        self._paths = {}

        for path_id, raw in state.get("paths", {}).items():
            path = self._new_path(
                self._normalize_addr(raw.get("addr")),
                raw.get("cwnd", 10.0),
                raw.get("rtt", 0.05),
                raw.get("worker", "worker-1"),
            )
            for key, value in raw.items():
                if key in ("addr", "cwnd", "rtt", "worker"):
                    continue
                if key == "pn_seen":
                    path["pn_seen"] = {int(pn) for pn in value}
                else:
                    path[key] = copy.deepcopy(value)
            path["cwnd"] = float(path["cwnd"])
            path["rtt"] = float(path["rtt"])
            path["received"] = float(path["received"])
            path["sent"] = float(path["sent"])
            if path["pn_largest"] is not None:
                path["pn_largest"] = int(path["pn_largest"])
            self._paths[path_id] = path

        if self._active_path_id not in self._paths:
            self._active_path_id = None
        return True

    def _new_path(self, addr, cwnd, rtt, worker):
        return {
            "addr": addr,
            "cwnd": float(cwnd),
            "rtt": float(rtt),
            "worker": worker,
            "state": "new",
            "token": None,
            "challenge_at": None,
            "deadline": None,
            "challenge_epoch": None,
            "validated_at": None,
            "received": 0.0,
            "sent": 0.0,
            "pn_seen": set(),
            "pn_largest": None,
        }

    def _path_view(self, path_id):
        path = self._paths[path_id]
        return {
            "path_id": path_id,
            "addr": path["addr"],
            "cwnd": path["cwnd"],
            "rtt": path["rtt"],
            "worker": path["worker"],
            "state": path["state"],
        }

    def _accept_active_packet(self, path_id, zero_rtt_id, pn):
        if pn is not None and not self.accept_pn(path_id, pn):
            return {
                "action": "duplicate",
                "path_id": path_id,
                "pn": pn,
                "reason": "duplicate packet number",
            }
        if zero_rtt_id is not None:
            self._zero_rtt_seen.add(zero_rtt_id)
        return {
            "action": "accept",
            "path_id": path_id,
            "pn": pn,
        }

    def _expire_validations(self, now):
        failed = []
        for path_id, path in self._paths.items():
            if path["state"] != "validating":
                continue
            deadline = path.get("deadline")
            if deadline is None or now >= deadline:
                path["state"] = "failed"
                path["token"] = None
                path["deadline"] = None
                failed.append(path_id)
        return failed

    def _reset_pn(self, path):
        path["pn_seen"] = set()
        path["pn_largest"] = None

    def _reset_amplification(self, path):
        path["received"] = 0.0
        path["sent"] = 0.0

    def _prune_pn(self, path):
        largest = path["pn_largest"]
        if largest is None:
            return
        cutoff = largest - self._PN_WINDOW
        if cutoff <= 0:
            return
        path["pn_seen"] = {pn for pn in path["pn_seen"] if pn >= cutoff}

    def _new_candidate_id(self, path_id):
        while True:
            self._candidate_seq += 1
            candidate_id = f"{path_id}~migration-{self._candidate_seq}"
            if candidate_id not in self._paths:
                return candidate_id

    def _active_worker(self):
        path = self._paths.get(self._active_path_id)
        if path is None:
            return "worker-1"
        return path["worker"]

    @staticmethod
    def _normalize_addr(addr):
        if addr is None:
            return None
        if isinstance(addr, str):
            host, separator, port = addr.rpartition(":")
            if separator:
                try:
                    return (host, int(port))
                except ValueError:
                    pass
        if isinstance(addr, (list, tuple)) and len(addr) >= 2:
            return (addr[0], int(addr[1]))
        return addr

    @staticmethod
    def _same_host(left, right):
        if left is None or right is None:
            return False
        if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
            return left[0] == right[0]
        return left == right
