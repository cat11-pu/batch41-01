import re
import threading

import pytest

import pairwise_subject_map as psm


def document(uri="https://idp.example/sector.json", redirects=None):
    return {
        "uri": uri,
        "redirect_uris": redirects or ["https://idp.example/cb"],
    }


def index(**over):
    args = {"sector_ttl": 3600, "salt_grace": 60}
    args.update(over)
    return psm.PairwiseSubjectMap(**args)


def ready(**over):
    idx = index(**over)
    idx.register_sector("sector-a", document(), 0)
    idx.register_sector("sector-b", document(), 0)
    return idx


def test_subject_is_stable_across_time():
    idx = ready()
    first = idx.subject_for("t1", "sector-a", "user-1", now=0)
    later = idx.subject_for("t1", "sector-a", "user-1", now=500)
    assert re.fullmatch(r"[0-9a-f]{32}", first["sub"])
    assert first["sub"] == later["sub"]
    assert later["salt_version"] == 1


def test_different_sectors_produce_different_subjects():
    idx = ready()
    one = idx.subject_for("t1", "sector-a", "user-1", now=0)
    two = idx.subject_for("t1", "sector-b", "user-1", now=0)
    assert one["sub"] != two["sub"]


def test_different_tenants_produce_different_subjects():
    idx = ready()
    one = idx.subject_for("t1", "sector-a", "user-1", now=0)
    two = idx.subject_for("t2", "sector-a", "user-1", now=0)
    assert one["sub"] != two["sub"]


def test_email_case_change_does_not_change_subject():
    idx = ready()
    upper = idx.subject_for("t1", "sector-a", "user-1", email="Alice@Example.COM", now=0)
    lower = idx.subject_for("t1", "sector-a", "user-1", email="alice@example.com", now=1)
    assert upper["sub"] == lower["sub"]


def test_changing_email_keeps_existing_mapping():
    idx = ready()
    first = idx.subject_for("t1", "sector-a", "user-1", email="old@example.com", now=0)
    changed = idx.subject_for("t1", "sector-a", "user-1", email="new@example.com", now=10)
    assert changed["sub"] == first["sub"]
    assert idx.lookup("t1", "sector-a", "user-1")["salt_version"] == first["salt_version"]


def test_public_sub_cannot_replace_missing_local_subject():
    idx = ready()
    with pytest.raises(ValueError):
        idx.subject_for("t1", "sector-a", "", public_sub="public-1", now=0)
    with pytest.raises(ValueError):
        idx.subject_for("t1", "sector-a", None, public_sub="public-1", now=0)
    result = idx.subject_for("t1", "sector-a", "user-1", public_sub="public-1", now=0)
    assert result["sub"] != "public-1"


def test_salt_rotation_keeps_existing_subject():
    idx = ready()
    first = idx.subject_for("t1", "sector-a", "user-1", now=0)
    rotation = idx.rotate_salt("t1", "sector-a", 10)
    again = idx.subject_for("t1", "sector-a", "user-1", now=11)
    assert rotation["version"] == 2
    assert again["sub"] == first["sub"]
    assert again["salt_version"] == 1


def test_salt_rotation_gives_new_subjects_new_version():
    idx = ready()
    idx.subject_for("t1", "sector-a", "user-1", now=0)
    idx.rotate_salt("t1", "sector-a", 10)
    fresh = idx.subject_for("t1", "sector-a", "user-2", now=11)
    assert fresh["salt_version"] == 2
    assert fresh["sub"] != idx.lookup("t1", "sector-a", "user-1")["sub"]


def test_sector_document_must_use_https():
    idx = index()
    with pytest.raises(ValueError):
        idx.register_sector("sector-a", document(uri="http://idp.example/sector.json"), 0)


def test_sector_redirect_uris_must_be_same_origin():
    idx = index()
    with pytest.raises(ValueError):
        idx.register_sector(
            "sector-a", document(redirects=["https://evil.example/cb"]), 0
        )
    with pytest.raises(ValueError):
        idx.register_sector(
            "sector-a", document(redirects=["http://idp.example/cb"]), 0
        )


def test_unregistered_sector_is_rejected():
    idx = index()
    with pytest.raises(ValueError):
        idx.subject_for("t1", "sector-missing", "user-1", now=0)


def test_concurrent_first_use_creates_one_mapping():
    idx = ready()
    results = []
    lock = threading.Lock()

    def worker():
        value = idx.subject_for("t1", "sector-a", "user-1", now=0)
        with lock:
            results.append(value["sub"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8
    assert len(set(results)) == 1
    assert idx.stats()["mappings"] == 1


def test_empty_tenant_or_subject_is_rejected():
    idx = ready()
    with pytest.raises(ValueError):
        idx.subject_for("", "sector-a", "user-1", now=0)
    with pytest.raises(ValueError):
        idx.subject_for("t1", "", "user-1", now=0)
    with pytest.raises(ValueError):
        idx.subject_for("t1", "sector-a", "   ", now=0)


def test_stale_sector_document_is_rejected_until_refreshed():
    idx = index(sector_ttl=10)
    idx.register_sector("sector-a", document(), 0)
    first = idx.subject_for("t1", "sector-a", "user-1", now=5)
    with pytest.raises(ValueError):
        idx.subject_for("t1", "sector-a", "user-1", now=11)
    idx.register_sector("sector-a", document(), 11)
    refreshed = idx.subject_for("t1", "sector-a", "user-1", now=12)
    assert refreshed["sub"] == first["sub"]


def test_audit_records_salt_version_without_email():
    idx = ready()
    idx.subject_for("t1", "sector-a", "user-1", email="Alice@Example.com", now=0)
    idx.rotate_salt("t1", "sector-a", 10)
    entries = idx.audit()
    minted = [entry for entry in entries if entry["type"] == "mint"][-1]
    rotated = [entry for entry in entries if entry["type"] == "rotate"][-1]
    assert minted["salt_version"] == 1
    assert minted["tenant_id"] == "t1"
    assert minted["sector_id"] == "sector-a"
    assert "email" not in minted
    assert rotated["salt_version"] == 2
