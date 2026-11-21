import base64
import copy
import hashlib
import hmac
import ipaddress
import json
import os
import re
import tempfile
import threading

try:
    import fcntl
except ImportError:
    fcntl = None


_MAC1_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_STATE_VERSION = 1


class HandshakeGuard:
    def __init__(
        self,
        psk=None,
        replay_window=2000,
        max_handshake_age=120,
        keepalive_interval=10,
    ):
        self.psk = psk
        self.replay_window = int(replay_window)
        self.max_handshake_age = max_handshake_age
        self.keepalive_interval = keepalive_interval

        self.seen_inits = set()
        self.last_init_ts = {}
        self.index_owners = {}
        self.pending = {}
        self.sessions = {}
        self.keys = {}
        self.allowed = {}
        self.keepalives = {}
        self._key_seq = 0
        self._lock = threading.RLock()

    def _psk_bytes(self):
        if self.psk is None:
            return b""
        if isinstance(self.psk, bytes):
            return self.psk
        return str(self.psk).encode("utf-8")

    def _derived_key(self, peer_pub, index, timestamp):
        salt = b"wireguard-handshake-guard-v1"
        prk = hmac.new(salt, self._psk_bytes(), hashlib.sha256).digest()
        info = f"{peer_pub}\0{index}\0{timestamp}".encode("utf-8")
        return hmac.new(prk, info, hashlib.sha256).hexdigest()

    def _new_key_id(self, peer, now):
        self._key_seq += 1
        material = f"rotate\0{peer}\0{now}\0{self._key_seq}".encode("utf-8")
        key = self._psk_bytes() or b"wireguard-no-psk"
        return hmac.new(key, material, hashlib.sha256).hexdigest()[:32]

    def initiate(self, index, peer_pub, timestamp, now):
        with self._lock:
            owner = self.index_owners.get(index)
            if owner is not None and owner != peer_pub:
                raise ValueError("index is owned by a different peer")

            if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
                return {"ok": False, "index": index, "reason": "invalid_timestamp"}

            previous = self.last_init_ts.get(peer_pub)
            if previous is not None and timestamp <= previous:
                return {"ok": False, "index": index, "reason": "replay"}

            age = now - timestamp
            if age > self.max_handshake_age:
                return {"ok": False, "index": index, "reason": "stale_timestamp"}
            if timestamp - now > self.max_handshake_age:
                return {"ok": False, "index": index, "reason": "stale_timestamp"}

            if index in self.sessions or index in self.pending:
                return {"ok": False, "index": index, "reason": "duplicate_index"}

            self.index_owners[index] = peer_pub
            self.pending[index] = {
                "index": index,
                "peer": peer_pub,
                "timestamp": timestamp,
                "created_at": now,
            }
            self.seen_inits.add((peer_pub, timestamp))
            self.last_init_ts[peer_pub] = timestamp
            return {"ok": True, "index": index, "reason": None}

    def cookie_reply(self, client_ip, mac1, valid=True):
        with self._lock:
            if not valid:
                return {"ok": False, "cookie": None, "reason": "invalid_mac1"}

            try:
                ipaddress.ip_address(client_ip)
            except (TypeError, ValueError):
                return {"ok": False, "cookie": None, "reason": "invalid_source"}

            if isinstance(mac1, (bytes, bytearray)):
                raw_mac1 = bytes(mac1)
                if len(raw_mac1) == 16:
                    mac1 = raw_mac1.hex()
                else:
                    try:
                        mac1 = raw_mac1.decode("ascii")
                    except UnicodeDecodeError:
                        return {"ok": False, "cookie": None, "reason": "invalid_mac1"}
            if not isinstance(mac1, str) or not _MAC1_RE.fullmatch(mac1):
                return {"ok": False, "cookie": None, "reason": "invalid_mac1"}
            if int(mac1, 16) == 0:
                return {"ok": False, "cookie": None, "reason": "invalid_mac1"}

            message = f"{client_ip}\0{mac1.lower()}".encode("utf-8")
            key = self._psk_bytes() or b"wireguard-cookie"
            cookie = hmac.new(key, b"cookie\0" + message, hashlib.sha256).hexdigest()
            return {"ok": True, "cookie": cookie, "reason": None}

    def respond(self, index, peer_pub, timestamp, now):
        with self._lock:
            owner = self.index_owners.get(index)
            if owner is not None and owner != peer_pub:
                raise ValueError("index is owned by a different peer")

            if index in self.sessions:
                return {
                    "ok": False,
                    "index": index,
                    "reason": "duplicate_index",
                    "key_material": None,
                }

            pending = self.pending.get(index)
            if pending is None or pending["peer"] != peer_pub:
                return {
                    "ok": False,
                    "index": index,
                    "reason": "unknown_index",
                    "key_material": None,
                }
            if pending["timestamp"] != timestamp:
                return {
                    "ok": False,
                    "index": index,
                    "reason": "timestamp_mismatch",
                    "key_material": None,
                }
            if now - pending["created_at"] > self.max_handshake_age:
                self.pending.pop(index, None)
                return {
                    "ok": False,
                    "index": index,
                    "reason": "stale_timestamp",
                    "key_material": None,
                }

            key_material = self._derived_key(peer_pub, index, timestamp)
            key_id = hashlib.sha256(
                f"session-key\0{key_material}".encode("utf-8")
            ).hexdigest()[:32]
            self.sessions[index] = {
                "index": index,
                "peer": peer_pub,
                "timestamp": timestamp,
                "established_at": now,
                "last_seen": now,
                "key_material": key_material,
                "key_id": key_id,
                "max_counter": -1,
                "bitmap": 0,
                "replay_window": self.replay_window,
            }
            self.pending.pop(index, None)
            return {
                "ok": True,
                "index": index,
                "reason": None,
                "key_material": key_material,
                "key_id": key_id,
            }

    @staticmethod
    def _accept_counter(session, counter):
        if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0:
            return False

        window = int(session.get("replay_window", 2000))
        window = max(window, 1)
        max_counter = int(session.get("max_counter", -1))
        bitmap = int(session.get("bitmap", 0))

        if counter > max_counter:
            shift = counter - max_counter
            if shift > window:
                bitmap = 1
            else:
                bitmap = ((bitmap << shift) | 1) & ((1 << (window + 1)) - 1)
            max_counter = counter
        else:
            difference = max_counter - counter
            if difference > window:
                return False
            bit = 1 << difference
            if bitmap & bit:
                return False
            bitmap |= bit

        session["max_counter"] = max_counter
        session["bitmap"] = bitmap
        return True

    def transport(self, receiver_index, counter, now):
        with self._lock:
            session = self.sessions.get(receiver_index)
            if session is None:
                return False
            if not self._accept_counter(session, counter):
                return False
            session["last_seen"] = now
            return True

    def allow_ip(self, peer, cidr):
        with self._lock:
            network = ipaddress.ip_network(cidr, strict=False)
            family = "ipv4" if network.version == 4 else "ipv6"
            entries = self.allowed.setdefault(peer, {"ipv4": [], "ipv6": []})
            text = str(network)
            for entry in entries[family]:
                if entry["network"] == network:
                    return entry["cidr"]
            entries[family].append({"cidr": text, "network": network})
            return text

    def select_allowed_ip(self, peer, ip):
        with self._lock:
            entries = self.allowed.get(peer)
            if not entries:
                return None
            try:
                address = ipaddress.ip_address(ip)
            except (TypeError, ValueError):
                return None

            family = "ipv4" if address.version == 4 else "ipv6"
            best = None
            best_prefix = -1
            for entry in entries.get(family, []):
                network = entry["network"]
                if address in network and network.prefixlen > best_prefix:
                    best = entry["cidr"]
                    best_prefix = network.prefixlen
            return best

    def rotate_key(self, peer, now, grace=60):
        with self._lock:
            key_id = self._new_key_id(peer, now)
            record = {
                "key_id": key_id,
                "created_at": now,
                "expires_at": now + grace,
                "key_material": self._derived_key(
                    f"rotate:{peer}:{self._key_seq}", key_id, now
                ),
            }
            self.keys.setdefault(peer, []).append(record)
            return {
                "ok": True,
                "key_id": key_id,
                "expires_at": now + grace,
                "key_material": record["key_material"],
                "reason": None,
            }

    def decrypt(self, peer, key_id, now):
        with self._lock:
            records = self.keys.get(peer, [])
            living = []
            usable = False
            for record in records:
                if now <= record["expires_at"]:
                    living.append(record)
                    if record["key_id"] == key_id:
                        usable = True
            if peer in self.keys:
                self.keys[peer] = living
            return usable

    def keepalive(self, peer, now):
        with self._lock:
            previous = self.keepalives.get(peer)
            interval = self.keepalive_interval
            if previous is not None and now - previous < interval:
                return {
                    "ok": False,
                    "reason": "rate_limited",
                    "retry_after": previous + interval - now,
                }
            self.keepalives[peer] = now
            return {"ok": True, "reason": None}

    def validate_message(self, kind, length):
        if not isinstance(length, int) or isinstance(length, bool):
            raise ValueError("invalid message length")

        fixed_lengths = {
            "initiation": 148,
            "response": 92,
            "cookie": 64,
        }
        if kind in fixed_lengths:
            if length != fixed_lengths[kind]:
                raise ValueError(f"invalid {kind} length")
            return True
        if kind == "transport":
            if length < 32 or length % 16 != 0:
                raise ValueError("invalid transport length")
            return True
        raise ValueError("unknown message kind")

    def _allowed_state(self):
        state = {}
        for peer, families in self.allowed.items():
            state[peer] = {
                "ipv4": [entry["cidr"] for entry in families.get("ipv4", [])],
                "ipv6": [entry["cidr"] for entry in families.get("ipv6", [])],
            }
        return state

    def _to_state(self):
        psk = None
        psk_is_bytes = isinstance(self.psk, bytes)
        if self.psk is not None:
            psk = base64.b64encode(self._psk_bytes()).decode("ascii")
        return {
            "version": _STATE_VERSION,
            "config": {
                "psk": psk,
                "psk_is_bytes": psk_is_bytes,
                "replay_window": self.replay_window,
                "max_handshake_age": self.max_handshake_age,
                "keepalive_interval": self.keepalive_interval,
            },
            "seen_inits": [list(item) for item in sorted(self.seen_inits)],
            "last_init_ts": dict(self.last_init_ts),
            "index_owners": {
                str(index): peer for index, peer in self.index_owners.items()
            },
            "pending": {
                str(index): copy.deepcopy(pending)
                for index, pending in self.pending.items()
            },
            "sessions": {
                str(index): copy.deepcopy(session)
                for index, session in self.sessions.items()
            },
            "keys": copy.deepcopy(self.keys),
            "allowed": self._allowed_state(),
            "keepalives": dict(self.keepalives),
            "key_seq": self._key_seq,
        }

    @staticmethod
    def _merge_replay(first, second):
        first_max = int(first.get("max_counter", -1))
        second_max = int(second.get("max_counter", -1))
        first_bits = int(first.get("bitmap", 0))
        second_bits = int(second.get("bitmap", 0))
        window = max(
            int(first.get("replay_window", 2000)),
            int(second.get("replay_window", 2000)),
            1,
        )

        if first_max >= second_max:
            shift = first_max - second_max
            if shift > window:
                second_bits = 0
            else:
                second_bits <<= shift
            merged_max, merged_bits = first_max, first_bits | second_bits
        else:
            shift = second_max - first_max
            if shift > window:
                first_bits = 0
            else:
                first_bits <<= shift
            merged_max, merged_bits = second_max, second_bits | first_bits

        merged = dict(second)
        merged["max_counter"] = merged_max
        merged["bitmap"] = merged_bits & ((1 << (window + 1)) - 1)
        merged["replay_window"] = window
        return merged

    @classmethod
    def _merge_states(cls, old, new):
        if not old:
            return new

        merged = copy.deepcopy(new)

        sessions = copy.deepcopy(old.get("sessions", {}))
        for index, session in new.get("sessions", {}).items():
            previous = sessions.get(index)
            if previous is None:
                sessions[index] = copy.deepcopy(session)
            elif (
                previous.get("key_id") == session.get("key_id")
                and previous.get("key_material") == session.get("key_material")
            ):
                sessions[index] = cls._merge_replay(previous, session)
            else:
                sessions[index] = copy.deepcopy(session)
        merged["sessions"] = sessions

        pending = copy.deepcopy(old.get("pending", {}))
        pending.update(copy.deepcopy(new.get("pending", {})))
        for index in sessions:
            pending.pop(index, None)
        merged["pending"] = pending

        owners = dict(old.get("index_owners", {}))
        owners.update(new.get("index_owners", {}))
        merged["index_owners"] = owners

        last_init_ts = dict(old.get("last_init_ts", {}))
        for peer, timestamp in new.get("last_init_ts", {}).items():
            if peer not in last_init_ts or timestamp > last_init_ts[peer]:
                last_init_ts[peer] = timestamp
        merged["last_init_ts"] = last_init_ts

        seen = {
            tuple(item) for item in old.get("seen_inits", [])
        } | {
            tuple(item) for item in new.get("seen_inits", [])
        }
        merged["seen_inits"] = [list(item) for item in sorted(seen)]

        keys = copy.deepcopy(old.get("keys", {}))
        for peer, records in new.get("keys", {}).items():
            by_id = {record["key_id"]: record for record in keys.get(peer, [])}
            for record in records:
                previous = by_id.get(record["key_id"])
                if (
                    previous is None
                    or record.get("expires_at", 0) >= previous.get("expires_at", 0)
                ):
                    by_id[record["key_id"]] = copy.deepcopy(record)
            keys[peer] = list(by_id.values())
        merged["keys"] = keys

        allowed = copy.deepcopy(old.get("allowed", {}))
        for peer, families in new.get("allowed", {}).items():
            target = allowed.setdefault(peer, {"ipv4": [], "ipv6": []})
            for family in ("ipv4", "ipv6"):
                for cidr in families.get(family, []):
                    if cidr not in target.setdefault(family, []):
                        target[family].append(cidr)
        merged["allowed"] = allowed

        keepalives = dict(old.get("keepalives", {}))
        for peer, timestamp in new.get("keepalives", {}).items():
            if peer not in keepalives or timestamp > keepalives[peer]:
                keepalives[peer] = timestamp
        merged["keepalives"] = keepalives
        merged["key_seq"] = max(
            int(old.get("key_seq", 0)), int(new.get("key_seq", 0))
        )
        return merged

    @staticmethod
    def _read_state(path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def flush(self, path):
        path = os.fspath(path)
        with self._lock:
            state = self._to_state()
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)

            lock_path = path + ".lock"
            with open(lock_path, "a+", encoding="utf-8") as lock_handle:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                try:
                    if os.path.exists(path):
                        try:
                            state = self._merge_states(
                                self._read_state(path), state
                            )
                        except (OSError, ValueError, TypeError):
                            pass

                    descriptor, temporary = tempfile.mkstemp(
                        prefix="." + os.path.basename(path) + ".",
                        suffix=".tmp",
                        dir=directory,
                    )
                    try:
                        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                            json.dump(
                                state,
                                handle,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary, path)
                    except BaseException:
                        try:
                            os.unlink(temporary)
                        except OSError:
                            pass
                        raise
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @classmethod
    def load(cls, path):
        path = os.fspath(path)
        lock_path = path + ".lock"
        with open(lock_path, "a+", encoding="utf-8") as lock_handle:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

        config = state.get("config", {})
        encoded_psk = config.get("psk")
        psk = None
        if encoded_psk is not None:
            decoded = base64.b64decode(encoded_psk)
            psk = decoded if config.get("psk_is_bytes") else decoded.decode("utf-8")

        store = cls(
            psk=psk,
            replay_window=config.get("replay_window", 2000),
            max_handshake_age=config.get("max_handshake_age", 120),
            keepalive_interval=config.get("keepalive_interval", 10),
        )
        store.seen_inits = {
            tuple(item) for item in state.get("seen_inits", [])
        }
        store.last_init_ts = dict(state.get("last_init_ts", {}))
        store.index_owners = {
            int(index): peer
            for index, peer in state.get("index_owners", {}).items()
        }
        store.pending = {
            int(index): pending
            for index, pending in state.get("pending", {}).items()
        }
        store.sessions = {
            int(index): session
            for index, session in state.get("sessions", {}).items()
        }
        store.keys = copy.deepcopy(state.get("keys", {}))
        store.keepalives = dict(state.get("keepalives", {}))
        store._key_seq = int(state.get("key_seq", 0))

        allowed = state.get("allowed", {})
        for peer, families in allowed.items():
            for family in ("ipv4", "ipv6"):
                for cidr in families.get(family, []):
                    store.allow_ip(peer, cidr)
        return store

    def snapshot(self):
        with self._lock:
            result = {
                "sessions": len(self.sessions),
                "indexes": sorted(self.sessions.keys()),
                "session_table": copy.deepcopy(self.sessions),
                "pending": copy.deepcopy(self.pending),
                "index_owners": copy.deepcopy(self.index_owners),
                "keys": copy.deepcopy(self.keys),
                "allowed": self._allowed_state(),
                "keepalives": copy.deepcopy(self.keepalives),
            }
            return copy.deepcopy(result)
