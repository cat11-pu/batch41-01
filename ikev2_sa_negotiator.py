import base64
import hashlib
import hmac
import ipaddress
import secrets


class IkeError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SaNegotiator:
    NONCE_MIN = 16
    NONCE_MAX = 256
    REPLAY_WINDOW = 64
    DPD_INTERVAL = 1.0
    DPD_TIMEOUT = 5.0
    NAT_T_PORT = 4500

    def __init__(self, supported=None, require_cookie=False):
        if supported is None:
            supported = [
                {"encr": "AES_256_CBC", "integ": "HMAC_SHA2_256",
                 "dh": "MODP_2048", "key_length": 256},
                {"encr": "AES_128_CBC", "integ": "HMAC_SHA2_256",
                 "dh": "MODP_2048", "key_length": 128},
            ]
        self.supported = [dict(item) for item in supported]
        self.require_cookie = require_cookie

        self._sas = {}
        self._replay = {}
        self._issued_cookies = {}
        self._next_ike_id = 1
        self._next_child_id = 1
        self._next_spi = 0x1000
        self._secret = secrets.token_bytes(32)

    def initiate(self, exchange):
        request = {
            "type": "IKE_SA_INIT",
            "spi_i": self._allocate_spi(),
            "nonce": secrets.token_bytes(32),
            "proposals": [dict(item) for item in self.supported],
        }
        result = exchange(dict(request))
        retries = 0
        while result.get("status") in ("cookie", "cookie_challenge", "retry"):
            if retries >= 3:
                break
            retries += 1
            request["cookie"] = result.get("cookie")
            result = exchange(dict(request))
        result = dict(result)
        result["retries"] = retries
        return result

    def handle_init(self, request):
        if "proposals" in request:
            offered = request.get("proposals") or []
        elif "proposal" in request:
            offered = [request["proposal"]]
        else:
            offered = []

        cookie = request.get("cookie")
        if self.require_cookie and not self._cookie_is_valid(cookie, request):
            issued = self._issue_cookie(request)
            return {"status": "cookie", "cookie": issued,
                    "initiator_spi": request.get("spi_i")}

        nonce = request.get("nonce")
        self._validate_nonce(nonce)
        chosen = self.select_proposal(offered)

        spi_i = request.get("spi_i")
        existing = self._find_ike_by_initiator_spi(spi_i)
        if existing is not None:
            return {
                "status": "ok",
                "ike_id": existing["id"],
                "spi_i": spi_i,
                "spi_r": existing["spi_r"],
                "proposal": dict(existing["proposal"]),
                "nonce": existing["nonce_i"],
                "cookie": cookie,
            }

        record = self._new_ike(spi_i=spi_i, proposal=chosen,
                               nonce_i=bytes(nonce))
        self._sas[record["id"]] = record
        return {
            "status": "ok",
            "ike_id": record["id"],
            "spi_i": record["spi_i"],
            "spi_r": record["spi_r"],
            "proposal": dict(record["proposal"]),
            "nonce": record["nonce_i"],
            "cookie": cookie,
        }

    def select_proposal(self, offered):
        for peer in offered:
            if not isinstance(peer, dict):
                continue
            for local in self.supported:
                if self._proposal_matches(local, peer):
                    chosen = dict(peer)
                    chosen.update(local)
                    chosen["key_length"] = self._proposal_key_length(
                        local, peer)
                    return chosen
        raise IkeError("no_proposal_chosen",
                       {"offered": offered, "supported": self.supported})

    def add_tunnel(self, label, local_ts, remote_ts, proposal=None):
        chosen = dict(proposal or self.supported[0])
        chosen["key_length"] = self._proposal_key_length(chosen, chosen)
        record = self._new_ike(proposal=chosen,
                               nonce_i=secrets.token_bytes(32))
        self._sas[record["id"]] = record
        child = self._new_child(record, label, local_ts, remote_ts, chosen)
        record["children"][child["id"]] = child
        return {"ike_id": record["id"], "label": label, "child": child}

    def add_child(self, ike_id, label, local_ts, remote_ts, proposal=None):
        record = self._get_ike(ike_id)
        chosen = dict(proposal or record["proposal"])
        chosen["key_length"] = self._proposal_key_length(chosen, chosen)
        child = self._new_child(record, label, local_ts, remote_ts, chosen)
        record["children"][child["id"]] = child
        return child

    def child(self, ike_id, child_id):
        record = self._sas.get(ike_id)
        if record is None:
            return None
        child = record["children"].get(child_id)
        if child is not None:
            return child if child["ike_id"] == ike_id else None
        for stored in record["children"].values():
            if stored["spi"] == child_id and stored["ike_id"] == ike_id:
                return stored
        return None

    def delete_child(self, ike_id, child_id):
        record = self._get_ike(ike_id)
        child = self.child(ike_id, child_id)
        if child is None:
            raise IkeError("child_not_found",
                           {"ike_id": ike_id, "child_id": child_id})
        record["children"].pop(child["id"], None)
        return {
            "deleted": child["id"],
            "ike_alive": True,
            "ike_id": ike_id,
            "remaining_children": len(record["children"]),
        }

    def resolve_rekey(self, local, remote):
        local_nonce = self._as_bytes(local.get("nonce", b""))
        remote_nonce = self._as_bytes(remote.get("nonce", b""))
        local_wins = local_nonce <= remote_nonce
        winner = "local" if local_wins else "remote"
        losing = remote if local_wins else local
        return {
            "winner": winner,
            "loser": "remote" if local_wins else "local",
            "winner_action": "keep",
            "loser_action": "delete",
            "winning_nonce": local_nonce if local_wins else remote_nonce,
            "losing_nonce": remote_nonce if local_wins else local_nonce,
            "winning_spi": (local if local_wins else remote).get("spi"),
            "losing_spi": losing.get("spi"),
            "delete_spi": losing.get("spi"),
        }

    def narrow_selectors(self, local, remote):
        narrowed = []
        for left in local or []:
            left_net = self._network(left)
            for right in remote or []:
                right_net = self._network(right)
                if left_net.version != right_net.version:
                    continue
                if left_net.subnet_of(right_net):
                    candidate = left_net
                elif right_net.subnet_of(left_net):
                    candidate = right_net
                else:
                    continue
                text = str(candidate)
                if text not in narrowed:
                    narrowed.append(text)
        return narrowed

    def dpd(self, ike_id, now):
        record = self._get_ike(ike_id)
        state = record["dpd"]
        if state["last_probe"] is None:
            state["last_probe"] = now
            state["awaiting_since"] = now
            state["probes"] = 1
            return {"action": "probe", "ike_id": ike_id,
                    "probe": state["probes"]}

        elapsed = now - state["awaiting_since"]
        if elapsed < self.DPD_INTERVAL:
            return {"action": "hold", "ike_id": ike_id,
                    "probe": state["probes"]}
        if elapsed >= self.DPD_TIMEOUT:
            record["state"] = "stale"
            return {"action": "rekey", "ike_id": ike_id,
                    "reason": "dpd_timeout", "probe": state["probes"]}

        state["last_probe"] = now
        state["probes"] += 1
        return {"action": "probe", "ike_id": ike_id,
                "probe": state["probes"]}

    def parse_nat_t(self, packet):
        out = dict(packet)
        payload = self._as_bytes(packet.get("payload", b""))
        ports = {
            packet.get("src_port"), packet.get("source_port"),
            packet.get("sport"), packet.get("dst_port"),
            packet.get("destination_port"), packet.get("dport"),
        }
        nat_t = (self.NAT_T_PORT in ports
                 or packet.get("port") == self.NAT_T_PORT)
        non_esp = False
        if nat_t and len(payload) >= 4 and payload[:4] == b"\x00\x00\x00\x00":
            non_esp = True
            payload = payload[4:]
            if not payload:
                out["keepalive"] = True
        out["payload"] = payload
        out["non_esp"] = non_esp
        out["nat_t"] = nat_t
        if "checksum" in packet:
            out["checksum_ok"] = self._checksum_ok(packet)
        return out

    def replay_check(self, sa_id, seq):
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise IkeError("invalid_sequence", {"sa_id": sa_id, "seq": seq})
        state = self._replay.setdefault(
            sa_id, {"highest": -1, "seen": set()})
        highest = state["highest"]
        seen = state["seen"]
        if seq > highest:
            state["highest"] = seq
            seen.add(seq)
            floor = seq - self.REPLAY_WINDOW + 1
            state["seen"] = {value for value in seen if value >= floor}
            return True
        if seq < highest - self.REPLAY_WINDOW + 1:
            return False
        if seq in seen:
            return False
        seen.add(seq)
        return True

    def rekey(self, sa_id):
        record = self._get_ike(sa_id)
        old_spis = [child["spi"] for child in record["children"].values()]
        record["generation"] += 1
        record["keys"] = self._derive_ike_keys(
            record["nonce_i"], record["nonce_r"],
            record["spi_i"], record["spi_r"], record["proposal"])
        chosen = dict(record["proposal"])
        for child in record["children"].values():
            child["spi"] = self._allocate_spi()
            child["keys"] = self._derive_child_keys(chosen)
            child["generation"] = record["generation"]
        new_spis = [child["spi"] for child in record["children"].values()]
        return {
            "ike_id": sa_id,
            "old_child_spis": old_spis,
            "new_child_spis": new_spis,
            "generation": record["generation"],
            "replay_high": self._replay.get(
                sa_id, {"highest": -1})["highest"],
            "children": list(record["children"].values()),
        }

    def reassemble(self, fragments):
        if isinstance(fragments, dict):
            fragments = [
                dict(value, index=index)
                if isinstance(value, dict) else
                {"index": index, "total": len(fragments), "data": value}
                for index, value in fragments.items()
            ]
        fragments = list(fragments or [])
        if not fragments:
            raise IkeError("no_fragments")
        total = fragments[0].get("total")
        if not isinstance(total, int) or total <= 0:
            raise IkeError("invalid_fragment", {"fragment": fragments[0]})
        pieces = {}
        for fragment in fragments:
            index = fragment.get("index")
            if fragment.get("total") != total:
                raise IkeError("invalid_fragment", {"fragment": fragment})
            if (not isinstance(index, int) or isinstance(index, bool)
                    or index < 0 or index >= total):
                raise IkeError("invalid_fragment", {"fragment": fragment})
            if index in pieces:
                raise IkeError("duplicate_fragment", {"index": index})
            pieces[index] = self._as_bytes(fragment.get("data", b""))
        missing = [index for index in range(total) if index not in pieces]
        if missing:
            raise IkeError("incomplete_fragments", {"missing": missing})
        return b"".join(pieces[index] for index in range(total))

    def export_state(self):
        sas = []
        for record in sorted(self._sas.values(), key=lambda item: item["id"]):
            exported = dict(record)
            exported["children"] = [
                dict(child) for child in record["children"].values()
            ]
            exported["dpd"] = dict(record["dpd"])
            sas.append(self._encode(exported))
        replay = [
            {"sa_id": sa_id, "highest": state["highest"],
             "seen": sorted(state["seen"])}
            for sa_id, state in sorted(self._replay.items(),
                                       key=lambda item: str(item[0]))
        ]
        return {
            "version": 1,
            "sas": sas,
            "replay": replay,
            "next_ike_id": self._next_ike_id,
            "next_child_id": self._next_child_id,
            "next_spi": self._next_spi,
            "supported": [dict(item) for item in self.supported],
            "require_cookie": self.require_cookie,
        }

    def import_state(self, state):
        if not isinstance(state, dict):
            raise IkeError("invalid_state")
        raw_sas = state.get("sas", [])
        self._sas = {}
        self._replay = {}
        self._issued_cookies = {}
        for raw in raw_sas:
            record = self._decode(raw)
            children = {}
            for child in record.get("children", []):
                children[child["id"]] = child
            record["children"] = children
            record.setdefault("dpd", {
                "last_probe": None, "awaiting_since": None, "probes": 0,
            })
            record.setdefault("state", "established")
            record.setdefault("generation", 0)
            self._sas[record["id"]] = record
        for entry in state.get("replay", []):
            self._replay[entry["sa_id"]] = {
                "highest": entry.get("highest", -1),
                "seen": set(entry.get("seen", [])),
            }
        max_ike = max(self._sas, default=0)
        child_ids = [
            child["id"]
            for record in self._sas.values()
            for child in record["children"].values()
        ]
        max_child = max(child_ids, default=0)
        spis = [record["spi_r"] for record in self._sas.values()]
        spis.extend(
            child["spi"]
            for record in self._sas.values()
            for child in record["children"].values()
        )
        spis.extend(self._replay)
        self._next_ike_id = max(
            int(state.get("next_ike_id", 1)), max_ike + 1)
        self._next_child_id = max(
            int(state.get("next_child_id", 1)), max_child + 1)
        numeric_spis = [spi for spi in spis if isinstance(spi, int)]
        self._next_spi = max(int(state.get("next_spi", 0x1000)),
                             max(numeric_spis, default=0) + 1,
                             0x1000)
        return {"restored": len(self._sas)}

    def sa_count(self):
        return len(self._sas)

    def _new_ike(self, spi_i=None, proposal=None, nonce_i=None):
        if proposal is None:
            proposal = dict(self.supported[0])
            proposal["key_length"] = self._proposal_key_length(
                proposal, proposal)
        ike_id = self._allocate_ike_id()
        spi_i = self._allocate_spi() if spi_i is None else spi_i
        spi_r = self._allocate_spi()
        nonce_i = nonce_i or secrets.token_bytes(32)
        nonce_r = secrets.token_bytes(32)
        return {
            "id": ike_id,
            "spi_i": spi_i,
            "spi_r": spi_r,
            "nonce_i": nonce_i,
            "nonce_r": nonce_r,
            "proposal": dict(proposal),
            "keys": self._derive_ike_keys(
                nonce_i, nonce_r, spi_i, spi_r, proposal),
            "children": {},
            "state": "established",
            "generation": 0,
            "dpd": {
                "last_probe": None, "awaiting_since": None, "probes": 0,
            },
        }

    def _new_child(self, record, label, local_ts, remote_ts, proposal):
        child_id = self._allocate_child_id()
        return {
            "id": child_id,
            "ike_id": record["id"],
            "label": label,
            "spi": self._allocate_spi(),
            "keys": self._derive_child_keys(proposal),
            "key_length": self._proposal_key_length(proposal, proposal),
            "local_ts": [str(item) for item in (local_ts or [])],
            "remote_ts": [str(item) for item in (remote_ts or [])],
            "state": "installed",
            "generation": record["generation"],
        }

    def _derive_ike_keys(self, nonce_i, nonce_r, spi_i, spi_r, proposal):
        length = max(16, self._proposal_key_length(proposal, proposal) // 8)
        material = b"".join([
            self._as_bytes(nonce_i), self._as_bytes(nonce_r),
            str(spi_i).encode("ascii"), str(spi_r).encode("ascii"),
            str(proposal.get("encr", "")).encode("ascii"),
            str(proposal.get("integ", "")).encode("ascii"),
            str(proposal.get("dh", "")).encode("ascii"),
        ])
        return hmac.new(self._secret, b"IKE" + material,
                        hashlib.sha256).digest()[:length]

    def _derive_child_keys(self, proposal):
        length = max(16, self._proposal_key_length(proposal, proposal) // 8)
        context = secrets.token_bytes(32)
        return hmac.new(self._secret, b"CHILD" + context,
                        hashlib.sha256).digest()[:length]

    def _get_ike(self, ike_id):
        record = self._sas.get(ike_id)
        if record is None:
            raise IkeError("no_ike_sa", {"ike_id": ike_id})
        return record

    def _find_ike_by_initiator_spi(self, spi_i):
        if spi_i is None:
            return None
        for record in self._sas.values():
            if record["spi_i"] == spi_i:
                return record
        return None

    def _allocate_ike_id(self):
        ike_id = self._next_ike_id
        self._next_ike_id += 1
        return ike_id

    def _allocate_child_id(self):
        child_id = self._next_child_id
        self._next_child_id += 1
        return child_id

    def _allocate_spi(self):
        while self._next_spi in self._all_spis():
            self._next_spi += 1
        spi = self._next_spi
        self._next_spi += 1
        return spi

    def _all_spis(self):
        values = set()
        for record in self._sas.values():
            values.add(record["spi_r"])
            for child in record["children"].values():
                values.add(child["spi"])
        return values

    def _issue_cookie(self, request):
        spi = str(request.get("spi_i", "")).encode("ascii", "ignore")
        timestamp = secrets.token_hex(8).encode("ascii")
        cookie = hmac.new(self._secret, b"COOKIE" + spi + timestamp,
                          hashlib.sha256).hexdigest()
        self._issued_cookies[cookie] = request.get("spi_i")
        return cookie

    def _cookie_is_valid(self, cookie, request):
        if cookie is None:
            return False
        if isinstance(cookie, bytes):
            cookie = cookie.decode("ascii", "ignore")
        expected_spi = self._issued_cookies.get(str(cookie))
        if expected_spi is None:
            return False
        return expected_spi == request.get("spi_i")

    def _validate_nonce(self, nonce):
        if nonce is None:
            raise IkeError("missing_nonce")
        if not isinstance(nonce, (bytes, bytearray, memoryview)):
            raise IkeError("invalid_nonce")
        length = len(nonce)
        if length < self.NONCE_MIN:
            raise IkeError("short_nonce",
                           {"length": length, "minimum": self.NONCE_MIN})
        if length > self.NONCE_MAX:
            raise IkeError("nonce_too_long",
                           {"length": length, "maximum": self.NONCE_MAX})

    def _proposal_matches(self, local, peer):
        for field in ("encr", "integ", "dh"):
            if field not in local:
                continue
            if field not in peer:
                return False
            if self._normalize(field, local[field]) != self._normalize(
                    field, peer[field]):
                return False
        local_length = local.get("key_length")
        peer_length = peer.get("key_length")
        if local_length is not None and peer_length is not None:
            if int(local_length) != int(peer_length):
                return False
        return True

    def _proposal_key_length(self, local, peer):
        value = local.get("key_length")
        if value is None:
            value = peer.get("key_length")
        if value is not None:
            return int(value)
        encr = str(local.get("encr") or peer.get("encr") or "").upper()
        for name, length in (
                ("AES_256", 256), ("AES_192", 192), ("AES_128", 128),
                ("CHACHA20", 256), ("3DES", 192), ("DES", 64)):
            if name in encr:
                return length
        return 256

    def _normalize(self, field, value):
        if field in ("encr", "integ", "dh"):
            return str(value).upper()
        return value

    def _network(self, selector):
        try:
            return ipaddress.ip_network(str(selector), strict=False)
        except ValueError as exc:
            raise IkeError("invalid_selector",
                           {"selector": selector}) from exc

    def _checksum_ok(self, packet):
        checksum = packet.get("checksum")
        expected = packet.get(
            "expected_checksum", packet.get("computed_checksum"))
        if expected is None:
            return checksum not in (None, 0)
        return checksum == expected

    def _as_bytes(self, value):
        if isinstance(value, bytes):
            return value
        if isinstance(value, (bytearray, memoryview)):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        if value is None:
            return b""
        return str(value).encode("utf-8")

    def _encode(self, value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {
                "__bytes__": base64.b64encode(bytes(value)).decode("ascii")
            }
        if isinstance(value, dict):
            return {key: self._encode(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._encode(item) for item in value]
        if isinstance(value, set):
            return {"__set__": [self._encode(item)
                                for item in sorted(value, key=str)]}
        return value

    def _decode(self, value):
        if isinstance(value, dict):
            if set(value) == {"__bytes__"}:
                return base64.b64decode(value["__bytes__"])
            if set(value) == {"__set__"}:
                return set(self._decode(item) for item in value["__set__"])
            return {key: self._decode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._decode(item) for item in value]
        return value
