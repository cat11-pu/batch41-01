import pytest

import netpol_selector_eval as np


def pod(ns="ns1", name="p1", labels=None, ip="10.0.0.5", host=False, ports=None):
    return {
        "namespace": ns,
        "name": name,
        "labels": dict(labels or {}),
        "ip": ip,
        "hostNetwork": host,
        "ports": list(ports if ports is not None else
                      [{"name": "http", "port": 8080, "protocol": "TCP"}]),
    }


def peer(ns, labels=None, ip=None):
    if ip is not None:
        return {"ip": ip}
    return {"namespace": ns, "labels": dict(labels or {})}


def rule(peers=None, ports=None, key="from"):
    out = {}
    if peers is not None:
        out[key] = peers
    if ports is not None:
        out["ports"] = ports
    return out


def policy(ns="ns1", name="deny", selector=None, types=("Ingress",),
           ingress=None, egress=None):
    return {
        "namespace": ns,
        "name": name,
        "podSelector": {} if selector is None else selector,
        "policyTypes": list(types),
        "ingress": list(ingress or []),
        "egress": list(egress or []),
    }


def evaluator(*namespaces):
    ev = np.PolicyEvaluator()
    for name, labels in namespaces:
        ev.add_namespace(name, labels)
    return ev


def test_empty_pod_selector_matches_every_pod():
    ev = evaluator(("ns1", {"team": "a"}))
    ev.add_policy(policy(selector={}, ingress=[]))
    target = pod(labels={"app": "web"})
    assert ev.evaluate(target, peer("ns1", {"role": "any"}), "Ingress", 80) is False


def test_default_deny_applies_only_to_selected_pods():
    ev = evaluator(("ns1", {}), ("ns2", {}))
    ev.add_policy(policy(selector={}, ingress=[]))
    assert ev.evaluate(pod(ns="ns1"), peer("ns1", {"app": "x"}), "Ingress", 80) is False
    assert ev.evaluate(pod(ns="ns2", labels={"app": "y"}),
                       peer("ns2", {"app": "x"}), "Ingress", 80) is True


def test_namespace_and_pod_selector_require_both():
    ev = evaluator(("ns1", {}), ("ns2", {"team": "a"}), ("ns3", {"team": "b"}))
    allow = [rule(peers=[{"namespaceSelector": {"team": "a"},
                          "podSelector": {"app": "db"}}])]
    ev.add_policy(policy(ingress=allow))
    target = pod(labels={"app": "api"})
    assert ev.evaluate(target, peer("ns2", {"app": "db"}), "Ingress", 80) is True
    assert ev.evaluate(target, peer("ns2", {"app": "web"}), "Ingress", 80) is False
    assert ev.evaluate(target, peer("ns3", {"app": "db"}), "Ingress", 80) is False


def test_separate_peers_are_ored():
    ev = evaluator(("ns1", {}), ("ns2", {"team": "a"}), ("ns3", {"team": "b"}))
    allow = [rule(peers=[{"namespaceSelector": {"team": "a"}},
                         {"namespaceSelector": {"team": "b"}}])]
    ev.add_policy(policy(ingress=allow))
    assert ev.evaluate(pod(), peer("ns3", {"app": "x"}), "Ingress", 80) is True


def test_ipblock_except_is_subtracted():
    ev = evaluator(("ns1", {}))
    allow = [rule(peers=[{"ipBlock": {"cidr": "10.0.0.0/8",
                                      "except": ["10.1.0.0/16"]}}])]
    ev.add_policy(policy(ingress=allow))
    assert ev.evaluate(pod(), peer("", ip="10.1.2.3"), "Ingress", 80) is False
    assert ev.evaluate(pod(), peer("", ip="10.2.2.2"), "Ingress", 80) is True


def test_named_port_is_resolved_against_target_pod():
    ev = evaluator(("ns1", {}))
    allow = [rule(ports=[{"port": "http"}])]
    ev.add_policy(policy(ingress=allow))
    target = pod(ports=[{"name": "http", "port": 8080, "protocol": "TCP"}])
    assert ev.evaluate(target, peer("ns1", {"app": "x"}), "Ingress", 8080) is True
    assert ev.evaluate(target, peer("ns1", {"app": "x"}), "Ingress", 9090) is False


def test_unknown_named_port_does_not_match():
    ev = evaluator(("ns1", {}))
    allow = [rule(ports=[{"port": "metrics"}])]
    ev.add_policy(policy(ingress=allow))
    target = pod(ports=[{"name": "http", "port": 8080, "protocol": "TCP"}])
    assert ev.evaluate(target, peer("ns1", {}), "Ingress", 8080) is False
    assert ev.evaluate(target, peer("ns1", {}), "Ingress", 9090) is False


def test_end_port_range_is_matched():
    ev = evaluator(("ns1", {}))
    allow = [rule(ports=[{"port": 8000, "endPort": 8010}])]
    ev.add_policy(policy(ingress=allow))
    assert ev.evaluate(pod(), peer("ns1", {}), "Ingress", 8005) is True
    assert ev.evaluate(pod(), peer("ns1", {}), "Ingress", 8015) is False


def test_ingress_and_egress_are_independent():
    ev = evaluator(("ns1", {"team": "a"}))
    ingress = [rule(peers=[{"podSelector": {"app": "web"}}])]
    egress = [rule(peers=[{"podSelector": {"app": "db"}}], key="to")]
    ev.add_policy(policy(types=("Ingress", "Egress"),
                         ingress=ingress, egress=egress))
    target = pod(labels={"app": "api"})
    web = peer("ns1", {"app": "web"})
    db = peer("ns1", {"app": "db"})
    assert ev.evaluate(target, web, "Ingress", 80) is True
    assert ev.evaluate(target, web, "Egress", 80) is False
    assert ev.evaluate(target, db, "Egress", 80) is True


def test_multiple_policies_are_unioned():
    ev = evaluator(("ns1", {}))
    ev.add_policy(policy(name="deny"))
    ev.add_policy(policy(name="allow-web",
                         ingress=[rule(peers=[{"podSelector": {"app": "web"}}])]))
    target = pod(labels={"app": "api"})
    assert ev.evaluate(target, peer("ns1", {"app": "web"}), "Ingress", 80) is True
    assert ev.evaluate(target, peer("ns1", {"app": "other"}), "Ingress", 80) is False


def test_policy_update_replaces_old_rules():
    ev = evaluator(("ns1", {}))
    ev.add_policy(policy(name="allow",
                         ingress=[rule(peers=[{"podSelector": {"app": "web"}}])]))
    target = pod(labels={"app": "api"})
    assert ev.evaluate(target, peer("ns1", {"app": "web"}), "Ingress", 80) is True
    before = ev.version()
    ev.update_policy(policy(name="allow",
                            ingress=[rule(peers=[{"podSelector": {"app": "db"}}])]))
    assert ev.version() > before
    assert ev.evaluate(target, peer("ns1", {"app": "web"}), "Ingress", 80) is False
    assert ev.evaluate(target, peer("ns1", {"app": "db"}), "Ingress", 80) is True


def test_delete_namespace_removes_its_policies():
    ev = evaluator(("ns1", {}), ("ns2", {}))
    ev.add_policy(policy(ns="ns1", name="a"))
    ev.add_policy(policy(ns="ns1", name="b"))
    ev.add_policy(policy(ns="ns2", name="c"))
    assert ev.delete_namespace("ns1") == 2
    assert ev.stats()["policies"] == 1
    assert ev.evaluate(pod(ns="ns1"), peer("ns1", {}), "Ingress", 80) is True


def test_dns_egress_rule_allows_port_53():
    ev = evaluator(("ns1", {}))
    dns_rule = rule(ports=[{"port": 53, "protocol": "UDP"}], key="to")
    ev.add_policy(policy(types=("Egress",), egress=[dns_rule]))
    target = pod(labels={"app": "api"})
    dns = pod(ns="kube-system", name="dns", labels={"k8s-app": "dns"},
              ip="10.96.0.10")
    assert ev.evaluate(target, dns, "Egress", {"port": 53, "protocol": "UDP"}) is True
    assert ev.evaluate(target, dns, "Egress", {"port": 443, "protocol": "TCP"}) is False


def test_host_network_pod_is_not_exempt():
    ev = evaluator(("ns1", {}))
    ev.add_policy(policy(selector={"app": "edge"}, ingress=[]))
    edge = pod(labels={"app": "edge"}, host=True)
    other = pod(labels={"app": "web"}, host=True)
    assert ev.evaluate(edge, peer("ns1", {"app": "any"}), "Ingress", 80) is False
    assert ev.evaluate(other, peer("ns1", {"app": "any"}), "Ingress", 80) is True


def test_index_avoids_scaling_with_policy_count():
    ev = np.PolicyEvaluator()
    for i in range(500):
        ev.add_namespace("ns%d" % i, {})
        ev.add_policy(policy(ns="ns%d" % i, name="deny"))
    assert ev.stats()["policies"] == 500
    assert ev.evaluate(pod(ns="ns0"), peer("ns0", {}), "Ingress", 80) is False
    checked = ev.stats()["last_rules_checked"]
    assert 1 <= checked < 50
