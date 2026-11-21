import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import copy
import threading


class MptcpError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SubflowScheduler:
    def __init__(self, mode="lowest-rtt"):
        self.mode = mode
        self._flows = {}
        self._locks = {}
        self._struct_lock = threading.RLock()
        self._outstanding = {}
        self._pending = []
        self._mptcp_enabled = True

    def _flow_lock(self, subflow_id):
        lock = self._locks.get(subflow_id)
        if lock is None:
            raise MptcpError("unknown_subflow", subflow_id)
        return lock

    def _eligible(self, flow):
        return (
            flow.get("state") == "established"
            and flow.get("mp_capable", False)
        )

    def _candidates(self):
        flows = [flow for flow in self._flows.values() if self._eligible(flow)]
        if not flows:
            return []
        primaries = [flow for flow in flows if not flow.get("backup")]
        pool = primaries if primaries else flows
        return sorted(pool, key=lambda flow: (flow.get("priority", 1),
                                              flow.get("rtt", 0.0)))

    def add_subflow(self, subflow):
        flow = dict(subflow)
        flow.setdefault("state", "established")
        if not flow.get("mp_capable", False):
            return {"ready": False, "reason": "mptcp_option_missing"}
        subflow_id = flow["id"]
        with self._struct_lock:
            self._flows[subflow_id] = flow
            self._locks[subflow_id] = threading.RLock()
            self._outstanding.setdefault(subflow_id, [])
        return {"ready": True, "subflow": subflow_id}

    def remove_subflow(self, subflow_id):
        with self._struct_lock:
            if subflow_id not in self._flows:
                return 0
            self._reclaim(subflow_id)
            self._flows.pop(subflow_id, None)
            self._locks.pop(subflow_id, None)
            self._outstanding.pop(subflow_id, None)
        return 1

    def remove_address(self, addr_id):
        removed = 0
        with self._struct_lock:
            targets = [sid for sid, flow in self._flows.items()
                       if flow.get("addr_id") == addr_id]
            for subflow_id in targets:
                flow = self._flows.get(subflow_id)
                if flow is not None:
                    flow["state"] = "removed"
                self._reclaim(subflow_id)
                removed += 1
        return removed

    def select(self):
        candidates = self._candidates()
        if not candidates:
            return None
        return candidates[0]["id"]

    def _weights(self, flows):
        weights = {}
        for flow in flows:
            rtt = flow.get("rtt") or 0.0
            weights[flow["id"]] = 1.0 / max(float(rtt), 1e-9)
        return weights

    def plan(self, total):
        flows = self._candidates()
        if not flows or total <= 0:
            return {flow["id"]: 0 for flow in flows}
        weights = self._weights(flows)
        weight_sum = sum(weights.values())
        allocations = {}
        remainders = {}
        for flow in flows:
            share = weights[flow["id"]] / weight_sum * total
            base = int(share)
            allocations[flow["id"]] = base
            remainders[flow["id"]] = share - base
        remaining = total - sum(allocations.values())
        for subflow_id in sorted(remainders, key=lambda sid: remainders[sid],
                                 reverse=True)[:remaining]:
            allocations[subflow_id] += 1
        return allocations

    def map_segment(self, dsn, length, subflow_id, ssn):
        return {
            "dsn": dsn,
            "dsn_end": dsn + length,
            "ssn": ssn,
            "ssn_end": ssn + length,
            "length": length,
            "subflow": subflow_id,
        }

    def track(self, segment):
        tracked = dict(segment)
        subflow_id = tracked["subflow"]
        with self._struct_lock:
            lock = self._flow_lock(subflow_id)
            with lock:
                self._outstanding.setdefault(subflow_id, []).append(tracked)
            self._pending = [
                item for item in self._pending
                if item.get("dsn") != tracked.get("dsn")
            ]
        return tracked

    def _reclaim(self, subflow_id):
        outstanding = self._outstanding.get(subflow_id, [])
        if not outstanding:
            self._outstanding[subflow_id] = []
            return []
        reinjected = [dict(segment) for segment in outstanding]
        reinjected.sort(key=lambda segment: segment.get("dsn", 0))
        known = {item.get("dsn") for item in self._pending}
        for segment in reinjected:
            if segment.get("dsn") not in known:
                self._pending.append(segment)
                known.add(segment.get("dsn"))
        self._pending.sort(key=lambda item: item.get("dsn", 0))
        self._outstanding[subflow_id] = []
        return reinjected

    def mark_failed(self, subflow_id):
        with self._struct_lock:
            flow = self._flows.get(subflow_id)
            if flow is None:
                raise MptcpError("unknown_subflow", subflow_id)
            flow["state"] = "failed"
            return self._reclaim(subflow_id)

    def pending(self):
        with self._struct_lock:
            return [dict(segment) for segment in self._pending]

    def handshake(self, result):
        if result.get("result") != "ok":
            self._mptcp_enabled = False
            return {"mptcp": False, "fallback": "tcp"}
        self._mptcp_enabled = bool(result.get("mp_capable", False))
        return {
            "mptcp": self._mptcp_enabled,
            "fallback": None if self._mptcp_enabled else "tcp",
        }

    def batch_assign(self, count):
        flows = self._candidates()
        if not flows or count <= 0:
            return []
        allocations = self.plan(count)
        queues = {sid: [sid] * amount
                  for sid, amount in allocations.items() if amount > 0}
        assigned = []
        while len(assigned) < count:
            for subflow_id in list(queues):
                if queues[subflow_id]:
                    assigned.append(queues[subflow_id].pop(0))
                    if len(assigned) == count:
                        break
            queues = {sid: queue for sid, queue in queues.items() if queue}
        return assigned

    def dedupe(self, segments):
        seen = set()
        kept = []
        for segment in segments:
            dsn = segment.get("dsn")
            if dsn in seen:
                continue
            seen.add(dsn)
            kept.append(dict(segment))
        return kept

    def resolve_open(self, local_token, remote_token):
        if local_token == remote_token:
            raise MptcpError("token_collision",
                             {"local": local_token, "remote": remote_token})
        if local_token < remote_token:
            return "initiator"
        return "responder"

    def set_priority(self, subflow_id, priority):
        with self._struct_lock:
            flow = self._flows.get(subflow_id)
            if flow is None:
                raise MptcpError("unknown_subflow", subflow_id)
            flow["priority"] = priority
        return priority

    def export_state(self):
        with self._struct_lock:
            return {
                "mode": self.mode,
                "mptcp_enabled": self._mptcp_enabled,
                "subflows": copy.deepcopy(self._flows),
                "outstanding": copy.deepcopy(self._outstanding),
                "pending": copy.deepcopy(self._pending),
            }

    def import_state(self, state):
        with self._struct_lock:
            self.mode = state.get("mode", self.mode)
            self._mptcp_enabled = state.get("mptcp_enabled", True)
            self._flows = copy.deepcopy(state.get("subflows", {}))
            self._outstanding = copy.deepcopy(state.get("outstanding", {}))
            self._pending = copy.deepcopy(state.get("pending", []))
            self._locks = {sid: threading.RLock() for sid in self._flows}
            for sid in self._flows:
                self._outstanding.setdefault(sid, [])
            self._pending.sort(key=lambda item: item.get("dsn", 0))

    def subflow(self, subflow_id):
        flow = self._flows.get(subflow_id)
        return None if flow is None else dict(flow)

    def subflows(self):
        return [dict(flow) for flow in self._flows.values()]

    def probe_targets(self):
        targets = []
        for flow in self._flows.values():
            if not self._eligible(flow):
                continue
            remote = flow.get("remote")
            if remote is not None and remote not in targets:
                targets.append(remote)
        return targets
