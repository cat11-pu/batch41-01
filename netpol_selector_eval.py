import copy
import ipaddress
import threading


class PolicyError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _labels_match(selector, labels):
    return all(labels.get(key) == value for key, value in selector.items())


class PolicyEvaluator:
    def __init__(self):
        self._namespaces = {}
        self._policies = {}
        self._ns_index = {}
        self._version = 0
        self._last_rules_checked = 0
        self._lock = threading.RLock()

    def add_namespace(self, name, labels):
        with self._lock:
            if name in self._namespaces:
                raise PolicyError("namespace_exists", name)
            self._namespaces[name] = dict(labels)
            self._ns_index.setdefault(name, {})
            self._version += 1

    def add_policy(self, policy):
        namespace = policy["namespace"]
        name = policy["name"]
        with self._lock:
            bucket = self._policies.setdefault(namespace, {})
            if name in bucket:
                raise PolicyError("policy_exists", "%s/%s" % (namespace, name))
            stored = copy.deepcopy(policy)
            bucket[name] = stored
            self._ns_index.setdefault(namespace, {})[name] = stored
            self._version += 1

    def update_policy(self, policy):
        namespace = policy["namespace"]
        name = policy["name"]
        with self._lock:
            bucket = self._policies.setdefault(namespace, {})
            if name not in bucket:
                raise PolicyError("policy_not_found", "%s/%s" % (namespace, name))
            stored = copy.deepcopy(policy)
            bucket[name] = stored
            self._ns_index.setdefault(namespace, {})[name] = stored
            self._version += 1

    def delete_policy(self, namespace, name):
        with self._lock:
            bucket = self._policies.get(namespace)
            if not bucket or name not in bucket:
                return False
            del bucket[name]
            index_bucket = self._ns_index.get(namespace)
            if index_bucket:
                index_bucket.pop(name, None)
            self._version += 1
            return True

    def delete_namespace(self, namespace):
        with self._lock:
            if namespace not in self._namespaces and namespace not in self._ns_index:
                return 0
            bucket = self._policies.pop(namespace, {})
            self._ns_index.pop(namespace, None)
            self._namespaces.pop(namespace, None)
            self._version += 1
            return len(bucket)

    def version(self):
        with self._lock:
            return self._version

    def stats(self):
        with self._lock:
            return {
                "namespaces": len(self._namespaces),
                "policies": sum(len(bucket) for bucket in self._policies.values()),
                "last_rules_checked": self._last_rules_checked,
                "version": self._version,
            }

    def evaluate(self, pod, peer, direction, port=None):
        with self._lock:
            namespace = pod["namespace"]
            target_labels = pod.get("labels", {})
            selected = [
                stored
                for stored in self._ns_index.get(namespace, {}).values()
                if direction in stored.get("policyTypes", [])
                and _labels_match(stored.get("podSelector", {}), target_labels)
            ]

            if not selected:
                self._last_rules_checked = 0
                return True

            rules_key = "ingress" if direction == "Ingress" else "egress"
            rules_checked = len(selected)
            for stored in selected:
                for rule in stored.get(rules_key, []):
                    rules_checked += 1
                    if self._rule_allows(rule, pod, peer, port):
                        self._last_rules_checked = rules_checked
                        return True

            self._last_rules_checked = rules_checked
            return False

    def _rule_allows(self, rule, pod, peer, port):
        if not self._ports_match(rule.get("ports"), pod, port):
            return False
        peers = rule.get("from", rule.get("to"))
        if peers is None:
            return True
        return any(self._peer_matches(candidate, peer) for candidate in peers)

    def _peer_matches(self, candidate, peer):
        ip_block = candidate.get("ipBlock")
        if ip_block is not None:
            peer_ip = peer.get("ip")
            if peer_ip is None:
                return False
            address = ipaddress.ip_address(peer_ip)
            if any(
                address in ipaddress.ip_network(excepted)
                for excepted in ip_block.get("except", [])
            ):
                return False
            return address in ipaddress.ip_network(ip_block["cidr"])

        if peer.get("ip") is not None:
            return False

        pod_selector = candidate.get("podSelector")
        namespace_selector = candidate.get("namespaceSelector")

        if namespace_selector is None and pod_selector is None:
            return False

        if namespace_selector is not None:
            namespace_labels = self._namespaces.get(peer.get("namespace", ""), {})
            if not _labels_match(namespace_selector, namespace_labels):
                return False

        if pod_selector is not None:
            if not _labels_match(pod_selector, peer.get("labels", {})):
                return False
        elif namespace_selector is None:
            return False

        return True

    def _ports_match(self, port_rules, pod, port):
        if not port_rules:
            return True
        if port is None:
            return False

        if isinstance(port, dict):
            requested_port = port["port"]
            requested_protocol = port.get("protocol", "TCP")
        else:
            requested_port = port
            requested_protocol = "TCP"

        for port_rule in port_rules:
            protocol = port_rule.get("protocol", "TCP")
            if protocol != requested_protocol:
                continue

            rule_port = port_rule["port"]
            if isinstance(rule_port, str):
                for container_port in pod.get("ports", []):
                    if (
                        container_port.get("name") == rule_port
                        and container_port.get("protocol", "TCP") == protocol
                        and container_port.get("port") == requested_port
                    ):
                        return True
                continue

            end_port = port_rule.get("endPort")
            if end_port is None:
                if rule_port == requested_port:
                    return True
            elif rule_port <= requested_port <= end_port:
                return True

        return False
