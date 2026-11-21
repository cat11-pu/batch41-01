import pytest

import mptcp_subflow_scheduler as mp


def sf(sid="s1", addr_id=1, local="10.0.0.1", remote="10.0.0.2", rtt=0.02,
       priority=1, backup=False, mp_capable=True, state="established"):
    return {
        "id": sid,
        "addr_id": addr_id,
        "local": local,
        "remote": remote,
        "rtt": rtt,
        "priority": priority,
        "backup": backup,
        "mp_capable": mp_capable,
        "state": state,
    }


def two_paths():
    scheduler = mp.SubflowScheduler()
    scheduler.add_subflow(sf("s1", addr_id=1, remote="10.0.0.2", rtt=0.2))
    scheduler.add_subflow(sf("s2", addr_id=2, remote="10.0.0.3", rtt=0.02))
    return scheduler


def test_primary_is_selected_over_backup():
    scheduler = mp.SubflowScheduler()
    scheduler.add_subflow(sf("s1", addr_id=1, remote="10.0.0.2", backup=False))
    scheduler.add_subflow(sf("s2", addr_id=2, remote="10.0.0.3", backup=True))
    assert scheduler.select() == "s1"


def test_backup_takes_over_after_failure():
    scheduler = mp.SubflowScheduler()
    scheduler.add_subflow(sf("s1", addr_id=1, remote="10.0.0.2", backup=False))
    scheduler.add_subflow(sf("s2", addr_id=2, remote="10.0.0.3", backup=True))
    scheduler.mark_failed("s1")
    assert scheduler.select() == "s2"
    assert scheduler.subflow("s1")["state"] == "failed"


def test_lowest_rtt_subflow_is_preferred():
    scheduler = two_paths()
    assert scheduler.select() == "s2"


def test_plan_weights_by_rtt():
    scheduler = two_paths()
    plan = scheduler.plan(1000)
    assert sum(plan.values()) == 1000
    assert plan["s2"] > plan["s1"]


def test_mapping_uses_data_level_length():
    scheduler = mp.SubflowScheduler()
    mapped = scheduler.map_segment(dsn=1000, length=100,
                                   subflow_id="s1", ssn=5000)
    assert mapped["dsn"] == 1000
    assert mapped["dsn_end"] == 1100
    assert mapped["ssn"] == 5000
    assert mapped["ssn_end"] == 5100
    assert mapped["length"] == 100


def test_failed_subflow_reinjects_unacked_data():
    scheduler = two_paths()
    scheduler.track({"dsn": 100, "length": 10, "subflow": "s1"})
    scheduler.track({"dsn": 200, "length": 10, "subflow": "s2"})
    scheduler.track({"dsn": 300, "length": 10, "subflow": "s1"})
    reinjected = scheduler.mark_failed("s1")
    assert [segment["dsn"] for segment in reinjected] == [100, 300]
    assert [segment["dsn"] for segment in scheduler.pending()] == [100, 300]


def test_handshake_failure_falls_back_to_tcp():
    scheduler = mp.SubflowScheduler()
    failed = scheduler.handshake({"result": "mp_capable_failed"})
    assert failed["mptcp"] is False
    assert failed["fallback"] == "tcp"
    okay = scheduler.handshake({"result": "ok", "mp_capable": True})
    assert okay["mptcp"] is True
    assert okay["fallback"] is None


def test_rm_addr_stops_probing_old_address():
    scheduler = two_paths()
    assert "10.0.0.2" in scheduler.probe_targets()
    assert scheduler.remove_address(1) == 1
    assert "10.0.0.2" not in scheduler.probe_targets()
    assert scheduler.subflow("s1")["state"] == "removed"
    assert scheduler.select() == "s2"


def test_batch_assignment_uses_multiple_subflows():
    scheduler = two_paths()
    assigned = scheduler.batch_assign(10)
    assert len(assigned) == 10
    assert set(assigned) == {"s1", "s2"}
    assert assigned.count("s1") > 0
    assert assigned.count("s2") > 0


def test_dedupes_by_dsn_not_ssn():
    scheduler = mp.SubflowScheduler()
    segments = [
        {"dsn": 100, "ssn": 1, "subflow": "s1"},
        {"dsn": 100, "ssn": 2, "subflow": "s2"},
        {"dsn": 200, "ssn": 2, "subflow": "s2"},
    ]
    kept = scheduler.dedupe(segments)
    assert len(kept) == 2
    assert [segment["dsn"] for segment in kept] == [100, 200]


def test_subflow_without_mp_capable_is_not_ready():
    scheduler = mp.SubflowScheduler()
    added = scheduler.add_subflow(sf("s1", mp_capable=False))
    assert added["ready"] is False
    assert added["reason"] == "mptcp_option_missing"
    assert scheduler.select() is None


def test_simultaneous_open_token_collision():
    scheduler = mp.SubflowScheduler()
    assert scheduler.resolve_open(1, 2) == "initiator"
    assert scheduler.resolve_open(2, 1) == "responder"
    with pytest.raises(mp.MptcpError) as exc:
        scheduler.resolve_open(5, 5)
    assert exc.value.code == "token_collision"


def test_priority_update_is_immediate():
    scheduler = mp.SubflowScheduler()
    scheduler.add_subflow(sf("s1", rtt=0.01, priority=1))
    scheduler.add_subflow(sf("s2", addr_id=2, remote="10.0.0.3",
                             rtt=0.5, priority=1))
    assert scheduler.select() == "s1"
    scheduler.set_priority("s1", 10)
    assert scheduler.select() == "s2"


def test_state_survives_restart():
    scheduler = mp.SubflowScheduler()
    scheduler.add_subflow(sf("s1", addr_id=1, remote="10.0.0.2"))
    scheduler.add_subflow(sf("s2", addr_id=2, remote="10.0.0.3", backup=True))
    scheduler.track({"dsn": 100, "length": 10, "subflow": "s1"})
    scheduler.mark_failed("s1")
    state = scheduler.export_state()
    other = mp.SubflowScheduler()
    other.import_state(state)
    assert other.select() == "s2"
    assert [segment["dsn"] for segment in other.pending()] == [100]
    assert other.subflow("s1")["state"] == "failed"


def test_reinjection_keeps_dsn_order():
    scheduler = mp.SubflowScheduler()
    scheduler.add_subflow(sf("s1", addr_id=1, remote="10.0.0.2"))
    scheduler.add_subflow(sf("s2", addr_id=2, remote="10.0.0.3"))
    scheduler.track({"dsn": 500, "length": 5, "subflow": "s1"})
    scheduler.track({"dsn": 100, "length": 5, "subflow": "s1"})
    reinjected = scheduler.mark_failed("s1")
    assert [segment["dsn"] for segment in reinjected] == [100, 500]
    assert [segment["dsn"] for segment in scheduler.pending()] == [100, 500]
