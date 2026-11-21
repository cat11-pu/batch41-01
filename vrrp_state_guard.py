import hmac
import ipaddress
import math
import numbers


class VrrpFsm:
    """VRRP master/backup election with preemption and tracking."""

    VALID_STATES = frozenset(("INITIALIZE", "BACKUP", "MASTER"))

    def __init__(
        self,
        vrid,
        priority,
        ip="10.0.0.1",
        advert_interval=1.0,
        preempt=True,
        auth_key="vrrp",
        domain="lan-a",
        track_penalty=10,
    ):
        self.vrid = self._validate_vrid(vrid)
        self.priority = self._validate_priority(priority)
        self.ip = self._validate_ip(ip)
        self.advert_interval = self._validate_interval(advert_interval)
        self.preempt = bool(preempt)
        self.auth_key = auth_key
        self.domain = domain
        self.track_penalty = self._validate_penalty(track_penalty)

        self._ip_value = int(ipaddress.IPv4Address(self.ip))
        self._state = "INITIALIZE"
        self._tracking_ok = True
        self._master_down_deadline = None
        self._preempt_deadline = None
        self._last_advert = None
        self._last_advert_time = None
        self._master_since = None
        self._garp_count = 0
        self._adverts_sent = 0
        self._last_sent_advert = None

    @property
    def election_state(self):
        return self._state

    @property
    def preempt_state(self):
        if not self.preempt:
            return "DISABLED"
        if self._preempt_deadline is not None:
            return "PENDING"
        return "ENABLED"

    @property
    def preemption_state(self):
        return self.preempt_state

    @property
    def tracking_state(self):
        return "UP" if self._tracking_ok else "DOWN"

    @property
    def garp_count(self):
        return self._garp_count

    @property
    def adverts_sent(self):
        return self._adverts_sent

    def current_state(self):
        return self._state

    def effective_priority(self):
        if self._tracking_ok:
            return self.priority
        return max(0, self.priority - self.track_penalty)

    def skew_time(self):
        priority = min(255, max(0, self.effective_priority()))
        return ((256 - priority) / 256.0) * self.advert_interval

    def start(self, now=0.0):
        now = self._validate_time(now)
        self._state = "BACKUP"
        self._preempt_deadline = None
        self._last_advert = None
        self._last_advert_time = None
        self._master_since = None
        self._master_down_deadline = now + (3.0 * self.advert_interval) + self.skew_time()
        return {"action": "start", "state": self._state}

    def _ignore(self, reason):
        return {"action": "ignore", "reason": reason, "state": self._state}

    def receive(self, advert, now):
        now = self._validate_time(now)
        valid, reason = self._validate_advert(advert)
        if not valid:
            return self._ignore(reason)

        priority = advert["priority"]
        interval = float(advert["interval"])
        remote_ip = str(ipaddress.IPv4Address(advert["ip"]))
        remote_ip_value = int(ipaddress.IPv4Address(remote_ip))

        if remote_ip_value == self._ip_value:
            return self._ignore("self advert")

        if self._state == "INITIALIZE":
            self.start(now)

        snapshot = {
            "vrid": self.vrid,
            "priority": priority,
            "ip": remote_ip,
            "interval": interval,
            "domain": advert["domain"],
        }
        self._last_advert = snapshot
        self._last_advert_time = now

        if self._state == "MASTER":
            if priority == 0:
                return {"action": "ignore", "reason": "master release advert", "state": self._state}
            if self.preempt and self._advert_beats_local(priority, remote_ip_value):
                self._state = "BACKUP"
                self._master_since = None
                self._preempt_deadline = None
                self._master_down_deadline = now + self._master_down_interval(interval)
                return {"action": "step_down", "reason": "better master advert", "state": self._state}
            return {"action": "stay_master", "state": self._state}

        if priority == 0:
            self._preempt_deadline = None
            self._master_down_deadline = now + self.skew_time()
            return {"action": "master_release", "state": self._state}

        self._master_down_deadline = now + self._master_down_interval(interval)

        if self._advert_beats_local(priority, remote_ip_value):
            self._preempt_deadline = None
            return {"action": "stay_backup", "state": self._state}

        if priority == self.effective_priority() and remote_ip_value == self._ip_value:
            self._preempt_deadline = None
            return {"action": "stay_backup", "state": self._state}

        if self.preempt and self.effective_priority() > 0:
            self._preempt_deadline = now + self.skew_time()
            return {"action": "preempt_pending", "state": self._state}

        self._preempt_deadline = None
        return {"action": "stay_backup", "state": self._state}

    def tick(self, now):
        now = self._validate_time(now)

        if self._state == "INITIALIZE":
            self.start(now)

        if self._state == "MASTER":
            return {"action": "stay_master", "state": self._state}

        if self._preempt_deadline is not None:
            if not self._preempt_should_fire():
                self._preempt_deadline = None
            elif now >= self._preempt_deadline:
                self._become_master(now)
                return {"action": "preempt", "state": self._state}

        if self._master_down_deadline is not None and now >= self._master_down_deadline:
            self._preempt_deadline = None
            if self.effective_priority() <= 0:
                return {"action": "cannot_become_master", "state": self._state}
            self._become_master(now)
            return {"action": "master_down_expired", "state": self._state}

        return {"action": "stay_backup", "state": self._state}

    def set_tracking(self, ok):
        ok = bool(ok)
        if ok == self._tracking_ok:
            return self.effective_priority()

        self._tracking_ok = ok
        if self._state == "BACKUP":
            if not ok and self._preempt_deadline is not None and not self._preempt_should_fire():
                self._preempt_deadline = None
            elif ok:
                self._rearm_preemption()
        return self.effective_priority()

    def virtual_mac(self):
        return "00:00:5e:00:01:%02x" % self.vrid

    def send_advert(self):
        packet = {
            "vrid": self.vrid,
            "priority": self.effective_priority(),
            "ip": self.ip,
            "interval": self.advert_interval,
            "auth": self.auth_key,
            "ttl": 255,
            "checksum": 1,
            "domain": self.domain,
        }
        self._adverts_sent += 1
        self._last_sent_advert = dict(packet)
        return packet

    def resolve_split_brain(self, peers):
        candidates = [(self.effective_priority(), self._ip_value, self.ip)]

        if peers is not None:
            for peer in peers:
                candidate = self._peer_candidate(peer)
                if candidate is not None:
                    candidates.append(candidate)

        winner = max(candidates, key=lambda item: (item[0], item[1]))
        winner_ip = winner[2]
        winner_value = winner[1]
        was_master = self._state == "MASTER"

        if winner_value == self._ip_value:
            self._state = "MASTER"
            self._preempt_deadline = None
            self._master_down_deadline = None
            if not was_master:
                self._garp_count += 1
        else:
            self._state = "BACKUP"
            self._master_since = None
            self._preempt_deadline = None

        return {
            "master": winner_ip,
            "garp_senders": [winner_ip],
            "state": self._state,
        }

    def _become_master(self, now):
        self._state = "MASTER"
        self._master_since = now
        self._master_down_deadline = None
        self._preempt_deadline = None
        self._garp_count += 1

    def _master_down_interval(self, advertised_interval):
        return (3.0 * advertised_interval) + self.skew_time()

    def _advert_beats_local(self, priority, remote_ip_value):
        local_priority = self.effective_priority()
        if priority != local_priority:
            return priority > local_priority
        return remote_ip_value > self._ip_value

    def _preempt_should_fire(self):
        if not self.preempt or self.effective_priority() <= 0:
            return False
        advert = self._last_advert
        if advert is None:
            return True
        remote_ip_value = int(ipaddress.IPv4Address(advert["ip"]))
        return not self._advert_beats_local(advert["priority"], remote_ip_value)

    def _rearm_preemption(self):
        if not self.preempt or self._last_advert is None or not self._preempt_should_fire():
            return
        base_time = self._last_advert_time if self._last_advert_time is not None else 0.0
        self._preempt_deadline = base_time + self.skew_time()

    def _peer_candidate(self, peer):
        if not isinstance(peer, dict):
            return None
        if "vrid" in peer and peer["vrid"] != self.vrid:
            return None
        if peer.get("domain", self.domain) != self.domain:
            return None
        try:
            priority = self._validate_priority(peer["priority"])
            address = ipaddress.IPv4Address(peer["ip"])
        except (KeyError, TypeError, ValueError):
            return None
        return (priority, int(address), str(address))

    def _validate_advert(self, advert):
        if not isinstance(advert, dict):
            return False, "malformed advert"
        if advert.get("vrid") != self.vrid:
            return False, "vrid mismatch"
        if advert.get("domain") != self.domain:
            return False, "wrong domain"
        if advert.get("ttl") != 255:
            return False, "bad ttl"
        if not self._checksum_ok(advert.get("checksum")):
            return False, "bad checksum"
        if "auth" not in advert or not self._auth_ok(advert.get("auth")):
            return False, "bad auth"
        try:
            self._validate_priority(advert.get("priority"))
            self._validate_interval(advert.get("interval"))
            ipaddress.IPv4Address(advert.get("ip"))
        except (TypeError, ValueError):
            return False, "malformed advert"
        return True, None

    def _auth_ok(self, auth):
        try:
            return hmac.compare_digest(str(auth), str(self.auth_key))
        except TypeError:
            return False

    @staticmethod
    def _checksum_ok(checksum):
        return isinstance(checksum, int) and not isinstance(checksum, bool) and checksum == 1

    @staticmethod
    def _validate_vrid(vrid):
        if not isinstance(vrid, int) or isinstance(vrid, bool) or not 1 <= vrid <= 255:
            raise ValueError("vrid must be an integer from 1 to 255")
        return vrid

    @staticmethod
    def _validate_priority(priority):
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 255:
            raise ValueError("priority must be an integer from 0 to 255")
        return priority

    @staticmethod
    def _validate_interval(interval):
        if (
            not isinstance(interval, numbers.Real)
            or isinstance(interval, bool)
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("advert_interval must be a positive finite number")
        return float(interval)

    @staticmethod
    def _validate_penalty(penalty):
        if (
            not isinstance(penalty, numbers.Real)
            or isinstance(penalty, bool)
            or not math.isfinite(penalty)
            or penalty < 0
        ):
            raise ValueError("track_penalty must be a non-negative finite number")
        return int(penalty)

    @staticmethod
    def _validate_time(now):
        if not isinstance(now, numbers.Real) or isinstance(now, bool) or not math.isfinite(now):
            raise ValueError("now must be a finite number")
        return float(now)

    @staticmethod
    def _validate_ip(ip):
        try:
            return str(ipaddress.IPv4Address(ip))
        except (TypeError, ValueError) as exc:
            raise ValueError("ip must be a valid IPv4 address") from exc
