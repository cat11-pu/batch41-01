class BundleGuard:
    """LACP (802.1AX / 802.3ad) bundle guard.

    Tracks member ports, validates received LACPDUs and enforces the
    standard partner/timeout/synchronization rules so that a single
    misbehaving member can never disturb forwarding on the whole group.
    """

    SHORT_TIMEOUT_SECS = 3
    LONG_TIMEOUT_SECS = 90

    # A well-formed LACPDU carries the mandatory TLVs and is padded to the
    # minimum Ethernet frame size; 128 is the largest legal TLV payload.
    MIN_TLV_LENGTH = 60
    MAX_TLV_LENGTH = 128

    DYNAMIC_MODES = ("active", "passive")

    def __init__(self, group_id="bundle-1", system_id="00:11:22:33:44:55",
                 system_priority=32768, mode="active", timeout="short",
                 min_links=1, cooldown=5, required_speed=None):
        self.group_id = group_id
        self.system_id = system_id
        self.system_priority = system_priority
        self.mode = mode
        self.timeout = timeout
        self.min_links = min_links
        self.cooldown = cooldown
        self.required_speed = required_speed
        self.members = {}
        self.mac_table_db = {}
        if self.timeout not in ("short", "long"):
            raise ValueError("invalid timeout: %r" % self.timeout)
        if self.mode not in self.DYNAMIC_MODES + ("static",):
            raise ValueError("invalid mode: %r" % self.mode)

    def add_member(self, port, speed=None, mode=None):
        member_mode = mode or self.mode
        if member_mode == "static" and self.mode in self.DYNAMIC_MODES:
            raise ValueError("static members cannot be mixed with LACP")
        if member_mode in self.DYNAMIC_MODES and self.mode == "static":
            raise ValueError("LACP members cannot be mixed with static")
        if port in self.members:
            raise ValueError("duplicate member port: %r" % port)
        self.members[port] = {
            "port": port,
            "speed": speed,
            "mode": member_mode,
            "last_lacpdu": None,
            "last_recv": None,
            "cooldown_until": 0,
            "flap_count": 0,
            "sync_reason": None,
        }

    def recv_lacpdu(self, port, lacpdu, now):
        member = self._require_member(port)

        tlv_length = lacpdu.get("tlv_length")
        if (
            not isinstance(tlv_length, int)
            or isinstance(tlv_length, bool)
            or not self.MIN_TLV_LENGTH <= tlv_length <= self.MAX_TLV_LENGTH
        ):
            raise ValueError("invalid LACPDU TLV length: %r" % tlv_length)

        source_mac = self._normalize_mac(lacpdu.get("source_mac"))
        actor_system = self._normalize_mac(lacpdu["actor"]["system_id"])
        if source_mac != actor_system:
            raise ValueError("LACPDU source MAC does not match actor system")

        reason = self._synchronization_reason(lacpdu)

        member["last_lacpdu"] = lacpdu
        member["last_recv"] = now
        member["sync_reason"] = reason

        synchronized = reason is None
        return {
            "port": port,
            "synchronized": synchronized,
            "reason": reason,
        }

    def member_state(self, port, now):
        member = self._require_member(port)

        if now < member["cooldown_until"]:
            return self._state(member, now, eligible=False, reason="cooldown")

        reason = self._current_reason(member, now)
        return self._state(member, now, eligible=reason is None, reason=reason)

    def select(self, now):
        selected = []
        for port, member in self.members.items():
            if self._is_selected(member, now):
                selected.append(port)
        return selected

    def aggregate_status(self, now):
        selected = self.select(now)
        if len(selected) < self.min_links:
            return {
                "up": False,
                "reason": "min_links",
                "selected": selected,
                "active_count": len(selected),
                "min_links": self.min_links,
            }
        return {
            "up": True,
            "reason": None,
            "selected": selected,
            "active_count": len(selected),
            "min_links": self.min_links,
        }

    def flap(self, port, now):
        member = self._require_member(port)
        member["flap_count"] += 1
        member["cooldown_until"] = now + self.cooldown
        member["last_lacpdu"] = None
        member["last_recv"] = None
        member["sync_reason"] = None

    def restart(self, now):
        for member in self.members.values():
            member["last_lacpdu"] = None
            member["last_recv"] = None
            member["cooldown_until"] = 0
            member["sync_reason"] = None
        self.mac_table_db = {}

    def learn_mac(self, mac, port, now=0):
        self._require_member(port)
        if port not in self.select(now):
            return
        self.mac_table_db[self._normalize_mac(mac)] = port

    def mac_table(self):
        synced = {}
        selected = [
            port
            for port, member in self.members.items()
            if member["last_recv"] is not None
            and self._is_selected(member, member["last_recv"])
        ]
        for mac, port in self.mac_table_db.items():
            if port in self.members:
                synced[mac] = list(selected)
        return synced

    def snapshot(self):
        return {
            "group_id": self.group_id,
            "system_id": self.system_id,
            "system_priority": self.system_priority,
            "mode": self.mode,
            "timeout": self.timeout,
            "min_links": self.min_links,
            "cooldown": self.cooldown,
            "required_speed": self.required_speed,
            "members": sorted(self.members),
        }

    def _synchronization_reason(self, lacpdu):
        actor = lacpdu["actor"]
        partner = lacpdu["partner"]
        actor_state = actor["state"]

        if self._normalize_mac(partner["system_id"]) != self._normalize_mac(
            self.system_id
        ):
            return "partner_system_mismatch"
        if partner["system_priority"] != self.system_priority:
            return "partner_priority_mismatch"

        advertised_timeout = actor_state.get("timeout")
        if advertised_timeout not in ("short", "long"):
            return "timeout_mismatch"
        if advertised_timeout != self.timeout:
            return "timeout_mismatch"

        if actor_state.get("defaulted"):
            return "defaulted"
        if not actor_state.get("synchronization"):
            return "not_synchronized"
        if not actor_state.get("aggregation"):
            return "individual"
        if not actor_state.get("collecting"):
            return "not_collecting"
        if not actor_state.get("distributing"):
            return "not_distributing"
        return None

    def _current_reason(self, member, now):
        lacpdu = member["last_lacpdu"]
        if lacpdu is None:
            return "no_lacpdu"

        advertised_timeout = lacpdu["actor"]["state"].get("timeout")
        if advertised_timeout == "long":
            timeout_secs = self.LONG_TIMEOUT_SECS
        else:
            timeout_secs = self.SHORT_TIMEOUT_SECS
        if now - member["last_recv"] > timeout_secs:
            return "expired"

        speed = member["speed"]
        if (
            self.required_speed is not None
            and speed is not None
            and speed != self.required_speed
        ):
            return "speed_mismatch"

        return member["sync_reason"]

    def _is_selected(self, member, now):
        if now < member["cooldown_until"]:
            return False
        return self._current_reason(member, now) is None

    def _state(self, member, now, eligible, reason):
        lacpdu = member["last_lacpdu"]
        actor_state = lacpdu["actor"]["state"] if lacpdu else {}
        return {
            "port": member["port"],
            "eligible": eligible,
            "synchronized": eligible,
            "reason": reason,
            "expired": reason == "expired",
            "collecting": bool(actor_state.get("collecting")),
            "distributing": bool(actor_state.get("distributing")),
            "speed": member["speed"],
            "mode": member["mode"],
        }

    def _require_member(self, port):
        try:
            return self.members[port]
        except KeyError:
            raise ValueError("unknown member port: %r" % port)

    @staticmethod
    def _normalize_mac(mac):
        if not isinstance(mac, str):
            raise ValueError("invalid MAC address: %r" % mac)
        return mac.strip().lower()
