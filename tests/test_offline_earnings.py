import pytest

import offline_earnings as oe


CONFIG = {"rate": 100, "cap_hours": 8, "mail_ttl": 72, "maintenance": []}


def earnings(config=None, state=None):
    cfg = dict(CONFIG)
    if config:
        cfg.update(config)
    return oe.OfflineEarnings(cfg, state=state)


def test_offline_start_is_last_logout_time():
    e = earnings({"cap_hours": 100})
    e.mark_offline("p1", at=10)
    result = e.settle("p1", now=20)
    assert result["hours"] == 10
    assert result["amount"] == 1000


def test_earnings_cap_is_applied_at_settlement():
    e = earnings()
    e.mark_offline("p1", at=0)
    result = e.settle("p1", now=100)
    assert result["hours"] == 8
    assert result["amount"] == 800


def test_double_card_snapshot_multiplies_payout():
    e = earnings()
    e.mark_offline("p1", at=0, double=True)
    result = e.settle("p1", now=4)
    assert result["amount"] == 800
    assert result["detail"]["multiplier"] == 2.0


def test_maintenance_window_is_deducted():
    e = earnings({"cap_hours": 100, "maintenance": [(2, 4)]})
    e.mark_offline("p1", at=0)
    result = e.settle("p1", now=10)
    assert result["hours"] == 8
    assert result["amount"] == 800


def test_guild_bonus_uses_logout_snapshot_after_kick():
    e = earnings()
    e.mark_offline("p1", at=0, guild="g1", guild_bonus=0.5)
    e.update_online_state("p1", team=["other"], guild=None)
    result = e.settle("p1", now=4)
    assert result["amount"] == 600
    assert result["detail"]["multiplier"] == 1.5


def test_team_uses_logout_snapshot():
    e = earnings()
    e.mark_offline("p1", at=0, team=["a", "b"])
    e.update_online_state("p1", team=["c"], guild=None)
    result = e.settle("p1", now=4)
    assert result["team"] == ["a", "b"]


def test_clock_rollback_is_rejected():
    e = earnings()
    e.mark_offline("p1", at=10)
    with pytest.raises(ValueError):
        e.settle("p1", now=5)


def test_two_devices_claim_only_once():
    e = earnings()
    e.mark_offline("p1", at=0)
    e.settle("p1", now=4)
    first = e.claim("p1", now=4)
    second = e.claim("p1", now=4)
    assert first["amount"] == 400
    assert second["duplicate"] is True
    assert second["amount"] == 0
    assert second["total_credited"] == 400


def test_daily_rollover_keeps_unclaimed_earnings():
    e = earnings()
    e.mark_offline("p1", at=0)
    e.settle("p1", now=10)
    result = e.claim("p1", now=30)
    assert result["amount"] == 800
    assert result["total_credited"] == 800


def test_mail_expiry_boundary():
    e = earnings({"mail_ttl": 72})
    e.mark_offline("p1", at=0)
    e.settle("p1", now=10)
    valid = e.expire("p1", now=82)
    expired = e.expire("p1", now=83)
    assert valid["expires_at"] == 82
    assert valid["expired"] is False
    assert expired["expired"] is True


def test_claim_after_expiry_is_rejected():
    e = earnings({"mail_ttl": 72})
    e.mark_offline("p1", at=0)
    e.settle("p1", now=10)
    with pytest.raises(ValueError):
        e.claim("p1", now=100)


def test_full_bag_does_not_mark_claimed():
    e = earnings()
    e.mark_offline("p1", at=0)
    e.settle("p1", now=4)
    with pytest.raises(ValueError):
        e.claim("p1", now=4, bag_space=0)
    result = e.claim("p1", now=5, bag_space=3)
    assert result["amount"] == 400
    assert result["duplicate"] is False


def test_claim_items_match_credited_amount():
    e = earnings()
    e.mark_offline("p1", at=0)
    e.settle("p1", now=4)
    result = e.claim("p1", now=4)
    assert result["items"] == {"gold": 400}
    assert sum(result["items"].values()) == result["amount"]


def test_claim_before_settlement_is_rejected():
    e = earnings()
    e.mark_offline("p1", at=0)
    with pytest.raises(ValueError):
        e.claim("p1", now=4)


def test_snapshot_keeps_claim_idempotency():
    e = earnings()
    e.mark_offline("p1", at=0)
    e.settle("p1", now=4)
    e.claim("p1", now=4)
    snapshot = e.snapshot()
    restored = earnings(state=snapshot)
    result = restored.claim("p1", now=4)
    assert result["duplicate"] is True
    assert result["amount"] == 0
    assert result["total_credited"] == 400
