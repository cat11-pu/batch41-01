import hashlib


class KayGuard:
    """MACsec MKA key agreement and secure association state."""

    MIN_MKA_BODY_LEN = 8
    MAX_PACKET_NUMBER = 0xFFFFFFFF
    KEY_NUMBER_SPACE = 256
    ASSOCIATION_SPACE = 4

    def __init__(self, cak_name='cak-prod', sci='aa:bb:cc:dd:ee:ff:0001', cipher_suite='gcm-aes-128', key_server_priority=0):
        self.cak_name = cak_name
        self.sci = sci
        self.cipher_suite = cipher_suite
        self.key_server_priority = key_server_priority
        self._members = {}
        self._associations = {}
        self._used_key_numbers = set()
        self._last_key_number = -1
        self._key_server = None
        self._link_up = True
        self._link_up_at = None

    @staticmethod
    def _split_sci(sci):
        parts = str(sci).split(':')
        mac = ':'.join(parts[:6]).lower()
        port = int(parts[6], 16) if len(parts) > 6 else 0
        return mac, port

    def join_ca(self, cak_name, sci):
        if cak_name != self.cak_name:
            return {"joined": False, "reason": "cak name mismatch"}
        self._members[sci] = {"sci": sci}
        return {"joined": True, "sci": sci}

    def elect_key_server(self, peers):
        candidates = list(peers)
        best_priority = min(peer["priority"] for peer in candidates)
        top = [peer for peer in candidates if peer["priority"] == best_priority]
        winner = max(top, key=lambda peer: self._split_sci(peer["sci"]))
        self._key_server = winner["sci"]
        return {"key_server": winner["sci"], "senders": [winner["sci"]]}

    def install_sak(self, sak, now=0.0):
        if sak.get("cipher") != self.cipher_suite:
            return {"installed": False, "reason": "cipher suite mismatch"}
        peer_sci = sak.get("sci")
        if peer_sci is not None and self._split_sci(peer_sci) != self._split_sci(self.sci):
            return {"installed": False, "reason": "sci mismatch"}
        key_number = sak.get("key_number")
        if key_number in self._used_key_numbers:
            return {"installed": False, "reason": "key number already used"}
        association = sak.get("an", 0)
        self._used_key_numbers.add(key_number)
        self._last_key_number = key_number
        self._associations[association] = {
            "an": association,
            "key_number": key_number,
            "key": sak.get("key"),
            "cipher": sak.get("cipher"),
            "sci": peer_sci,
            "installed_at": now,
            "lifetime": sak.get("lifetime", 30.0),
            "state": "Up",
            "last_pn": -1,
        }
        return {"installed": True, "an": association, "key_number": key_number}

    def _next_association(self):
        for association in range(self.ASSOCIATION_SPACE):
            if association not in self._associations:
                return association
        return (max(self._associations) + 1) % self.ASSOCIATION_SPACE

    def _next_key_number(self):
        candidate = (self._last_key_number + 1) % self.KEY_NUMBER_SPACE
        for _ in range(self.KEY_NUMBER_SPACE):
            if candidate not in self._used_key_numbers:
                return candidate
            candidate = (candidate + 1) % self.KEY_NUMBER_SPACE
        return None

    def rotate(self, now, lifetime=30.0):
        association = self._next_association()
        key_number = self._next_key_number()
        if key_number is None:
            return {"rotated": False, "reason": "key number space exhausted"}
        sak = {
            "key_number": key_number,
            "an": association,
            "cipher": self.cipher_suite,
            "sci": self.sci,
            "key": "sak-%d" % key_number,
            "lifetime": lifetime,
        }
        result = self.install_sak(sak, now=now)
        if not result["installed"]:
            return {"rotated": False, "reason": result["reason"]}
        return {"rotated": True, "an": association, "key_number": key_number}

    def expire(self, now):
        expired = []
        for association, sa in self._associations.items():
            if sa["state"] == "Expired":
                continue
            if now >= sa["installed_at"] + sa["lifetime"]:
                sa["state"] = "Expired"
                expired.append(association)
        return sorted(expired)

    def sa_state(self, association):
        sa = self._associations.get(association)
        if sa is None:
            return "Missing"
        return sa["state"]

    def _icv(self, sa, pn, payload):
        digest = hashlib.sha256(
            ("%s:%s:%s" % (sa["key"], pn, payload)).encode("utf-8")
        ).hexdigest()
        return digest[:16]

    def encrypt(self, pn, payload, association=0, now=0.0):
        sa = self._associations.get(association)
        if sa is None:
            return {"ok": False, "reason": "missing association"}
        if sa["state"] != "Up":
            return {"ok": False, "reason": "sa not up"}
        if now >= sa["installed_at"] + sa["lifetime"]:
            return {"ok": False, "reason": "sa expired"}
        if pn >= self.MAX_PACKET_NUMBER:
            return {"ok": False, "rekey": True}
        packet = {
            "an": association,
            "pn": pn,
            "payload": payload,
            "sci": self.sci,
            "icv": self._icv(sa, pn, payload),
        }
        return {"ok": True, "packet": packet}

    def decrypt(self, packet, now=0.0):
        sa = self._associations.get(packet.get("an"))
        if sa is None:
            return {"ok": False, "reason": "missing association"}
        if sa["state"] == "Down":
            return {"ok": False, "reason": "renegotiate"}
        if sa["state"] == "Expired" or now >= sa["installed_at"] + sa["lifetime"]:
            return {"ok": False, "reason": "expired"}
        pn = packet.get("pn")
        if packet.get("icv") != self._icv(sa, pn, packet.get("payload")):
            return {"ok": False, "reason": "icv"}
        if pn <= sa["last_pn"]:
            return {"ok": False, "reason": "replay"}
        sa["last_pn"] = pn
        return {"ok": True, "payload": packet.get("payload")}

    def receive_mka(self, packet):
        body = packet.get("body", b"")
        if len(body) < self.MIN_MKA_BODY_LEN:
            return {"ok": False, "reason": "invalid length"}
        cak_name = packet.get("cak_name")
        if cak_name is not None and cak_name != self.cak_name:
            return {"ok": False, "reason": "cak name mismatch"}
        return {"ok": True}

    def link_down(self):
        self._link_up = False
        for sa in self._associations.values():
            if sa["state"] == "Up":
                sa["state"] = "Down"

    def link_up(self, now=0.0):
        self._link_up = True
        self._link_up_at = now

    def snapshot(self):
        return {
            "cak_name": self.cak_name,
            "sci": self.sci,
            "cipher_suite": self.cipher_suite,
            "key_server": self._key_server,
            "used_key_numbers": sorted(self._used_key_numbers),
            "associations": {
                association: {
                    "key_number": sa["key_number"],
                    "state": sa["state"],
                    "last_pn": sa["last_pn"],
                    "installed_at": sa["installed_at"],
                    "lifetime": sa["lifetime"],
                }
                for association, sa in self._associations.items()
            },
        }
