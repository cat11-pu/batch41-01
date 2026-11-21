import pytest

import report_review_queue as rrq


CONFIG = {
    "cooldown": 100,
    "appeal_window": 200,
    "evidence_retention": 300,
    "malicious_penalty": 10,
}


def queue(**extra):
    cfg = dict(CONFIG)
    cfg.update(extra)
    return rrq.ReviewQueue(cfg)


def evidence():
    return {"chat": ["c1", "c2"], "battle": ["b1"], "voice": ["v1"]}


def filed(q, reporter="r1", target="t1", match="m1", now=0, server_id=None):
    return q.file(reporter, target, match, evidence(), now=now, server_id=server_id)


def test_same_match_and_target_aggregate_into_one_ticket():
    q = queue()
    first = filed(q)
    second = q.file("r2", "t1", "m1", evidence(), now=0)
    assert first["ticket_id"] == second["ticket_id"]
    assert second["aggregated"] is True
    assert second["reporters"] == ["r1", "r2"]


def test_ten_reporters_on_one_match_create_one_ticket():
    q = queue()
    ticket_id = None
    last = None
    for index in range(10):
        last = q.file("r%d" % index, "t1", "m1", evidence(), now=0)
        if ticket_id is None:
            ticket_id = last["ticket_id"]
        assert last["ticket_id"] == ticket_id
    assert len(last["reporters"]) == 10


def test_evidence_snapshot_keeps_chat_battle_and_voice():
    q = queue()
    ticket = filed(q)
    assert ticket["evidence"]["chat"] == ["c1", "c2"]
    assert ticket["evidence"]["battle"] == ["b1"]
    assert ticket["evidence"]["voice"] == ["v1"]


def test_decision_notifies_every_reporter():
    q = queue()
    ticket = filed(q)
    q.file("r2", "t1", "m1", evidence(), now=0)
    q.assign(ticket["ticket_id"], "rv1")
    result = q.decide(ticket["ticket_id"], "rv1", "guilty",
                      {"type": "mute", "until": 10})
    assert result["notified"] == ["r1", "r2"]


def test_appeal_freezes_punishment_expiry():
    q = queue()
    ticket = filed(q)
    q.assign(ticket["ticket_id"], "rv1")
    q.decide(ticket["ticket_id"], "rv1", "guilty",
             {"type": "ban", "until": 100})
    q.appeal(ticket["ticket_id"], "mistake", now=50)
    state = q.enforce(ticket["ticket_id"], now=150)
    assert state["active"] is True
    assert state["status"] == "frozen"


def test_overturn_revokes_original_punishment():
    q = queue()
    ticket = filed(q)
    q.assign(ticket["ticket_id"], "rv1")
    q.decide(ticket["ticket_id"], "rv1", "guilty",
             {"type": "ban", "until": 100})
    q.appeal(ticket["ticket_id"], "mistake", now=50)
    result = q.decide(ticket["ticket_id"], "rv2", "overturn",
                      idempotency_key="appeal-1", now=60, appeal=True)
    assert result["revoked"] is True
    state = q.enforce(ticket["ticket_id"], now=150)
    assert state["active"] is False
    assert state["status"] == "revoked"


def test_batch_decisions_are_per_case():
    q = queue()
    first = filed(q, reporter="r1", target="t1", match="m1")
    second = filed(q, reporter="r2", target="t2", match="m2")
    results = q.batch_decide([
        {"ticket_id": first["ticket_id"], "reviewer_id": "rv1",
         "verdict": "guilty", "punishment": {"type": "mute", "until": 10},
         "idempotency_key": "b1"},
        {"ticket_id": second["ticket_id"], "reviewer_id": "rv1",
         "verdict": "dismiss", "idempotency_key": "b2"},
    ])
    assert results[0]["punishment"]["type"] == "mute"
    assert results[1]["punishment"] is None
    assert results[0]["notified"] == ["r1"]
    assert results[1]["notified"] == ["r2"]


def test_cooldown_is_keyed_by_reported_target():
    q = queue()
    first = filed(q, reporter="r1", target="t1", match="m1", now=0)
    with pytest.raises(ValueError):
        q.file("r2", "t1", "m2", evidence(), now=50)
    later = q.file("r2", "t1", "m2", evidence(), now=150)
    assert later["ticket_id"] != first["ticket_id"]


def test_malicious_report_deducts_reporter_reputation():
    q = queue()
    ticket = filed(q)
    result = q.decide(ticket["ticket_id"], "rv1", "malicious",
                      idempotency_key="mal-1")
    assert result["punishment"] is None
    assert result["reporter_reputation"] == 90


def test_cross_server_target_resolves_to_global_id():
    q = rrq.ReviewQueue(CONFIG, identities={("s2", "guest"): "acct-9"})
    ticket = q.file("r1", "guest", "m1", evidence(), now=0, server_id="s2")
    assert ticket["target_id"] == "acct-9"


def test_duplicate_decision_key_is_idempotent():
    q = queue()
    ticket = filed(q)
    first = q.decide(ticket["ticket_id"], "rv1", "guilty",
                     {"type": "ban", "until": 100}, idempotency_key="k1")
    second = q.decide(ticket["ticket_id"], "rv2", "guilty",
                      {"type": "ban", "until": 999}, idempotency_key="k1")
    assert second["duplicate"] is True
    assert second["punishment"] == first["punishment"]
    state = q.enforce(ticket["ticket_id"], now=150)
    assert state["active"] is False


def test_other_reviewer_cannot_decide_after_assignment():
    q = queue()
    ticket = filed(q)
    q.assign(ticket["ticket_id"], "rv1")
    with pytest.raises(ValueError):
        q.decide(ticket["ticket_id"], "rv2", "guilty",
                 {"type": "mute", "until": 10})
    result = q.decide(ticket["ticket_id"], "rv1", "guilty",
                      {"type": "mute", "until": 10})
    assert result["verdict"] == "guilty"


def test_retention_shorter_than_appeal_window_is_rejected():
    with pytest.raises(ValueError):
        rrq.ReviewQueue({"appeal_window": 200, "evidence_retention": 100})


def test_appeal_within_retention_keeps_evidence():
    q = queue()
    ticket = filed(q, now=0)
    q.assign(ticket["ticket_id"], "rv1")
    q.decide(ticket["ticket_id"], "rv1", "guilty",
             {"type": "mute", "until": 10}, now=0)
    result = q.appeal(ticket["ticket_id"], "review", now=100)
    assert result["evidence"]["chat"] == ["c1", "c2"]
    assert result["evidence"]["battle"] == ["b1"]


def test_enforcement_expires_without_appeal():
    q = queue()
    ticket = filed(q)
    q.assign(ticket["ticket_id"], "rv1")
    q.decide(ticket["ticket_id"], "rv1", "guilty",
             {"type": "ban", "until": 100})
    state = q.enforce(ticket["ticket_id"], now=150)
    assert state["active"] is False
    assert state["status"] == "expired"
