import pytest

import process_lineage_gate as plg


def _ev(
    pid,
    ppid,
    start,
    name="proc",
    exe=None,
    exe_hash=None,
    namespace="host",
    cmdline=None,
    session=1,
    seq=0,
    ts=None,
    kind="exec",
):
    return {
        "pid": pid,
        "ppid": ppid,
        "start": float(start),
        "name": name,
        "exe": exe,
        "exe_hash": exe_hash,
        "namespace": namespace,
        "cmdline": list(cmdline or [name]),
        "session": session,
        "seq": seq,
        "ts": float(start if ts is None else ts),
        "kind": kind,
    }


def test_ppid_reuse_links_to_the_parent_alive_at_child_start():
    gate = plg.ProcessLineageGate()
    gate.ingest(
        [
            _ev(100, 1, 1, name="old-parent"),
            _ev(100, 1, 10, name="new-parent", seq=1),
            _ev(200, 100, 11, name="late-child", seq=2),
            _ev(300, 100, 5, name="early-child", seq=3),
        ]
    )
    late = gate.lineage(200, 11)["chain"]
    early = gate.lineage(300, 5)["chain"]
    assert late[1]["pid"] == 100
    assert late[1]["start"] == 10.0
    assert early[1]["start"] == 1.0


def test_lineage_never_crosses_namespace_boundaries():
    gate = plg.ProcessLineageGate()
    gate.ingest(
        [
            _ev(100, 1, 1, name="host-100", namespace="host"),
            _ev(100, 1, 5, name="ctr-100", namespace="ctr", seq=1),
            _ev(200, 100, 6, name="ctr-child", namespace="ctr", seq=2),
        ]
    )
    chain = gate.lineage(200, 6, namespace="ctr")["chain"]
    assert chain[1]["name"] == "ctr-100"
    assert chain[1]["namespace"] == "ctr"


def test_whitelist_requires_same_name_and_exact_executable():
    whitelist = [
        {"name": "java", "exe": "/usr/bin/java", "cmdline": ["java", "-jar", "app.jar"]}
    ]
    gate = plg.ProcessLineageGate(whitelist=whitelist)
    gate.ingest(
        [
            _ev(10, 1, 1, name="java", exe="/usr/bin/java",
                cmdline=["java", "-jar", "app.jar"]),
            _ev(20, 1, 2, name="java", exe="/tmp/java",
                cmdline=["java", "-jar", "app.jar"], seq=1),
        ]
    )
    assert gate.verdict(10, 1)["trusted"] is True
    verdict = gate.verdict(20, 2)
    assert verdict["trusted"] is False
    assert verdict["reason"] == "not_whitelisted"


def test_short_lived_process_is_kept_for_lineage():
    gate = plg.ProcessLineageGate()
    gate.ingest(
        [
            _ev(10, 1, 2, name="short"),
            _ev(10, 1, 2, name="short", seq=1, ts=3, kind="exit"),
            _ev(20, 10, 4, name="child", seq=2),
        ]
    )
    chain = gate.lineage(20, 4)["chain"]
    assert [item["name"] for item in chain] == ["child", "short"]


def test_missing_executable_falls_back_to_hash_identity():
    gate = plg.ProcessLineageGate()
    gate.ingest([_ev(10, 1, 1, name="dropper", exe=None, exe_hash="sha256:abc")])
    chain = gate.lineage(10, 1)["chain"]
    assert chain[0]["exe"] is None
    assert chain[0]["exe_hash"] == "sha256:abc"


def test_setsid_does_not_break_the_parent_chain():
    gate = plg.ProcessLineageGate()
    gate.ingest(
        [
            _ev(10, 1, 1, name="parent", session=1),
            _ev(20, 10, 2, name="escaped", session=99, seq=1),
        ]
    )
    chain = gate.lineage(20, 2)["chain"]
    assert [item["pid"] for item in chain] == [20, 10]


def test_child_event_before_parent_is_resolved_after_ingest():
    gate = plg.ProcessLineageGate()
    gate.ingest(
        [
            _ev(20, 10, 2, name="child", seq=1),
            _ev(10, 1, 1, name="parent"),
        ]
    )
    chain = gate.lineage(20, 2)["chain"]
    assert [item["name"] for item in chain] == ["child", "parent"]


def test_process_name_with_spaces_and_parentheses_is_preserved():
    gate = plg.ProcessLineageGate()
    gate.ingest([_ev(10, 1, 1, name="my proc (deleted)")])
    assert gate.lineage(10, 1)["chain"][0]["name"] == "my proc (deleted)"


def test_whitelist_cmdline_is_exact_not_a_prefix():
    whitelist = [
        {"name": "java", "exe": "/usr/bin/java", "cmdline": ["java", "-jar", "app.jar"]}
    ]
    gate = plg.ProcessLineageGate(whitelist=whitelist)
    gate.ingest(
        [
            _ev(10, 1, 1, name="java", exe="/usr/bin/java",
                cmdline=["java", "-jar", "app.jar"]),
            _ev(20, 1, 2, name="java", exe="/usr/bin/java",
                cmdline=["java", "-jar", "app.jar", "--evil"], seq=1),
        ]
    )
    assert gate.verdict(10, 1)["trusted"] is True
    assert gate.verdict(20, 2)["trusted"] is False


def test_depth_limit_truncates_the_chain_and_says_so():
    gate = plg.ProcessLineageGate(max_depth=2)
    gate.ingest(
        [
            _ev(10, 1, 1, name="p1"),
            _ev(20, 10, 2, name="p2", seq=1),
            _ev(30, 20, 3, name="p3", seq=2),
            _ev(40, 30, 4, name="p4", seq=3),
        ]
    )
    result = gate.lineage(40, 4)
    assert result["depth"] == 2
    assert len(result["chain"]) == 2
    assert result["truncated"] is True


def test_empty_ingest_and_unknown_process_are_safe():
    gate = plg.ProcessLineageGate()
    assert gate.ingest([]) == 0
    result = gate.lineage(999, 1)
    assert result["missing"] is True
    assert result["chain"] == []


def test_duplicate_events_are_stored_once():
    gate = plg.ProcessLineageGate()
    event = _ev(10, 1, 1, name="once", seq=5)
    assert gate.ingest([event]) == 1
    assert gate.ingest([dict(event)]) == 0
    assert gate.stats()["events"] == 1


def test_parent_starting_after_child_is_not_linked():
    gate = plg.ProcessLineageGate()
    gate.ingest(
        [
            _ev(10, 1, 20, name="parent"),
            _ev(20, 10, 10, name="child", seq=1),
        ]
    )
    chain = gate.lineage(20, 10)["chain"]
    assert [item["pid"] for item in chain] == [20]


def test_retention_expiry_hides_old_processes():
    gate = plg.ProcessLineageGate(retention=5.0)
    gate.ingest([_ev(10, 1, 0, name="old")])
    fresh = gate.lineage(10, 0, now=4.0)
    stale = gate.lineage(10, 0, now=100.0)
    assert fresh["expired"] is False
    assert [item["pid"] for item in fresh["chain"]] == [10]
    assert stale["expired"] is True
    assert stale["chain"] == []


def test_gates_do_not_share_ingested_state():
    first = plg.ProcessLineageGate()
    second = plg.ProcessLineageGate()
    first.ingest([_ev(10, 1, 1, name="only-first")])
    assert first.lineage(10, 1)["missing"] is False
    assert second.lineage(10, 1)["missing"] is True
