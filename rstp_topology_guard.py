class TopologyGuard:
    """RSTP root election, port role and topology change guard."""

    def __init__(self, bridge_id='32768.00:11:22:33:44:55', max_age=20, bpdu_guard=True, root_guard=True, tc_limit=10):
        self.bridge_id = bridge_id
        self.bridge_key = self._bridge_key(bridge_id)
        self.max_age = max_age
        self.bpdu_guard = bpdu_guard
        self.root_guard = root_guard
        self.tc_limit = tc_limit

        self.ports = {}
        self.root = bridge_id
        self.root_path_cost = 0
        self._root_port = None
        self._bpdu_by_port = {}

        self._tc_count = 0
        self._tc_times = []
        self._tc_suppressed = 0
        self._tc_flooded = 0
        self._mac_flush_pending = False
        self._role_log = []

        self._cost_table = {
            10: 2000000,
            100: 200000,
            1000: 20000,
            10000: 2000,
            40000: 500,
            100000: 200,
        }

    @staticmethod
    def _bridge_key(bridge_id):
        priority, mac = bridge_id.split('.', 1)
        return (int(priority), mac)

    @staticmethod
    def _port_key(port_id):
        priority, number = port_id.split('.', 1)
        return (int(priority), int(number))

    def add_port(self, name, edge=False, guard=None, root_guard=False):
        if guard is None:
            guard = edge and self.bpdu_guard
        self.ports[name] = {
            "name": name,
            "edge": edge,
            "guard": bool(guard),
            "root_guard": bool(root_guard),
            "state": "Disabled" if guard else "Discarding",
            "role": "Designated",
            "proposal": False,
            "agreement": False,
        }

    def root_id(self):
        return self.root

    def root_port(self):
        return self._root_port

    def port_state(self, name):
        return self.ports[name]["state"]

    def port_role(self, name):
        return self.ports[name]["role"]

    def path_cost(self, speed_mbps):
        if speed_mbps in self._cost_table:
            return self._cost_table[speed_mbps]
        return max(1, int(20000000000 // speed_mbps))

    def _handle_tc(self, now):
        self._tc_times = [moment for moment in self._tc_times if now - moment < 1.0]
        if len(self._tc_times) >= self.tc_limit:
            self._tc_suppressed += 1
            return False
        self._tc_times.append(now)
        self._tc_count += 1
        self._tc_flooded += 1
        self._mac_flush_pending = True
        return True

    def tc_events(self):
        return self._tc_count

    def receive_bpdu(self, bpdu, port, now=0.0):
        entry = self.ports[port]

        if bpdu.get("version") != 2:
            entry["state"] = "Discarding"
            return {"accepted": False, "reason": "unsupported bpdu version"}

        if entry["guard"]:
            entry["state"] = "Disabled"
            return {"accepted": False, "reason": "bpdu guard"}

        if bpdu.get("sender_bridge") == self.bridge_id:
            entry["state"] = "Discarding"
            return {"accepted": False, "reason": "own bpdu loop", "loop": True}

        if bpdu.get("message_age", 0) > bpdu.get("max_age", self.max_age):
            return {"accepted": False, "reason": "message age exceeded"}

        if entry["root_guard"]:
            if self._bridge_key(bpdu["root_id"]) < self.bridge_key:
                entry["state"] = "Discarding"
                return {"accepted": False, "reason": "root guard"}

        if bpdu.get("tc"):
            self._handle_tc(now)

        return {"accepted": self._update_root_and_roles(bpdu, port)}

    def _update_root_and_roles(self, bpdu, port):
        advertised_root = self._bridge_key(bpdu["root_id"])
        advertiser = self._bridge_key(bpdu["sender_bridge"])
        remote_cost = int(bpdu.get("root_path_cost", 0))
        total_cost = remote_cost + self.path_cost(1000)
        remote_port = self._port_key(bpdu.get("port_id", "128.1"))
        new_vector = (advertised_root, total_cost, advertiser, remote_port)

        stored = self._bpdu_by_port.get(port)
        if stored is not None:
            stored_vector = (stored["root"], stored["cost"],
                             stored["advertiser"], stored["port"])
            if new_vector > stored_vector:
                self._recompute_roles()
                return True

        self._bpdu_by_port[port] = {
            "root": advertised_root,
            "root_id": bpdu["root_id"],
            "cost": total_cost,
            "advertiser": advertiser,
            "port": remote_port,
        }
        self._recompute_roles()
        return True

    def _recompute_roles(self):
        candidates = []
        for name, data in self._bpdu_by_port.items():
            if data["root"] < self.bridge_key:
                candidates.append((
                    data["root"],
                    data["cost"],
                    data["advertiser"],
                    data["port"],
                    name,
                ))
        candidates.sort()

        if not candidates:
            self.root = self.bridge_id
            self.root_path_cost = 0
            self._root_port = None
            for name in self.ports:
                self._set_role(name, "Designated")
            return

        winner = candidates[0]
        new_root_port = winner[4]
        self.root = self._bpdu_by_port[new_root_port]["root_id"]
        self.root_path_cost = winner[1]
        self._root_port = new_root_port

        for index, candidate in enumerate(candidates):
            name = candidate[4]
            if name == new_root_port:
                self._set_role(name, "Root")
            else:
                self._set_role(name, "Alternate")

        for name in self.ports:
            port_info = self._bpdu_by_port.get(name)
            if port_info is None or port_info["root"] >= self.bridge_key:
                self._set_role(name, "Designated")

        entry = self.ports[new_root_port]
        if entry["role"] == "Root" and entry["state"] == "Discarding":
            entry["state"] = "Learning"

    def _set_role(self, port, role):
        entry = self.ports[port]
        if entry["role"] != role:
            self._role_log.append((port, entry["role"], role))
            entry["role"] = role

    def process_proposal(self, port, agreement=False):
        entry = self.ports[port]
        entry["proposal"] = True
        entry["agreement"] = bool(agreement)
        if not agreement:
            entry["state"] = "Learning"
            return {"state": "Learning", "forwarding": False, "synced": False}
        entry["state"] = "Forwarding"
        return {"state": "Forwarding", "forwarding": True, "synced": True}

    def advance_port(self, port):
        entry = self.ports[port]
        if entry["state"] == "Disabled":
            return entry["state"]
        if entry["state"] == "Discarding":
            entry["state"] = "Learning"
        elif entry["state"] == "Learning":
            entry["state"] = "Forwarding"
        return entry["state"]

    def snapshot(self):
        return {
            "root_id": self.root,
            "root_port": self._root_port,
            "root_path_cost": self.root_path_cost,
            "ports": {
                name: {"role": data["role"], "state": data["state"]}
                for name, data in self.ports.items()
            },
            "tc_events": self._tc_count,
            "tc_flooded": self._tc_flooded,
            "tc_suppressed": self._tc_suppressed,
            "mac_flush_pending": self._mac_flush_pending,
            "role_changes": list(self._role_log),
        }
