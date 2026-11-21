import pytest

import suid_capability_baseline as scb


def entry(path, inode, mode=0o4755, **overrides):
    data = {
        "path": path,
        "inode": inode,
        "mode": mode,
        "content_hash": "hash-%d" % inode,
        "mtime": 100,
        "size": 10,
        "xattrs": {},
        "is_dir": False,
        "overlay": False,
        "missing": False,
    }
    data.update(overrides)
    return data


def make_scanner(**kwargs):
    return scb.SuidCapabilityBaseline(**kwargs)


def test_new_suid_file_after_upgrade_is_found():
    scanner = make_scanner()
    scanner.snapshot([entry("/usr/bin/old", 1, mode=0o755)])
    report = scanner.compare([entry("/usr/bin/new", 2)])
    assert len(report["findings"]) == 1
    assert report["findings"][0]["kind"] == "new_suid"
    assert report["findings"][0]["path"] == "/usr/bin/new"


def test_same_inode_content_change_is_detected_by_hash():
    scanner = make_scanner()
    scanner.snapshot([entry("/usr/bin/a", 1, content_hash="h1")])
    report = scanner.compare([entry("/usr/bin/a", 1, content_hash="h2", mtime=100)])
    assert len(report["findings"]) == 1
    assert report["findings"][0]["kind"] == "replaced"


def test_file_capability_without_suid_bit_is_found():
    scanner = make_scanner()
    scanner.snapshot([])
    report = scanner.compare([
        entry(
            "/usr/bin/cap-tool",
            3,
            mode=0o755,
            xattrs={"security.capability": "cap_net_raw+ep"},
        )
    ])
    assert len(report["findings"]) == 1
    assert report["findings"][0]["kind"] == "new_capability"
    assert report["findings"][0]["caps"] == ["cap_net_raw"]


def test_suid_on_nosuid_mount_is_not_reported():
    scanner = make_scanner()
    scanner.add_mount("/mnt/data", ("nosuid", "rw"))
    scanner.snapshot([])
    report = scanner.compare([entry("/mnt/data/tool", 4, mode=0o4755)])
    assert report["findings"] == []


def test_world_writable_dir_only_reported_on_normal_mount():
    scanner = make_scanner()
    scanner.add_mount("/mnt/data", ("nosuid", "rw"))
    scanner.snapshot([])
    report = scanner.compare([
        entry("/mnt/data/share", 5, mode=0o777, is_dir=True),
        entry("/srv/share", 6, mode=0o777, is_dir=True),
    ])
    assert [f["path"] for f in report["findings"]] == ["/srv/share"]
    assert report["findings"][0]["kind"] == "world_writable_dir"


def test_hardlinks_collapse_to_one_inode():
    scanner = make_scanner()
    result = scanner.snapshot([
        entry("/usr/bin/a", 7),
        entry("/usr/bin/b", 7),
    ])
    assert result["count"] == 1
    assert result["by_inode"][7]["paths"] == ["/usr/bin/a", "/usr/bin/b"]


def test_exemption_on_one_hardlink_covers_the_inode():
    scanner = make_scanner()
    scanner.add_exemption("/usr/bin/a")
    result = scanner.snapshot([
        entry("/usr/bin/a", 7),
        entry("/usr/bin/b", 7),
        entry("/usr/bin/c", 8),
    ])
    assert result["count"] == 1
    assert 7 not in result["by_inode"]
    assert 8 in result["by_inode"]
    assert scanner.compare([
        entry("/usr/bin/a", 7),
        entry("/usr/bin/b", 7),
        entry("/usr/bin/c", 8),
    ])["findings"] == []


def test_container_and_overlay_paths_are_excluded():
    scanner = make_scanner()
    scanner.snapshot([])
    report = scanner.compare([
        entry("/var/lib/docker/overlay2/abc/tool", 9),
        entry("/usr/bin/overlay-tool", 10, overlay=True),
        entry("/usr/bin/real-tool", 11),
    ])
    assert [f["path"] for f in report["findings"]] == ["/usr/bin/real-tool"]


def test_wildcard_exemption_is_rejected():
    scanner = make_scanner()
    with pytest.raises(scb.BaselineError) as caught:
        scanner.add_exemption("/usr/bin/*")
    assert caught.value.reason == "wildcard_exemption"


def test_exact_exemption_does_not_cover_new_sibling():
    scanner = make_scanner()
    scanner.add_exemption("/usr/bin/passwd")
    scanner.snapshot([])
    report = scanner.compare([
        entry("/usr/bin/passwd", 12),
        entry("/usr/bin/backdoor", 13),
    ])
    assert [f["path"] for f in report["findings"]] == ["/usr/bin/backdoor"]


def test_capability_sets_compare_ignoring_order():
    scanner = make_scanner()
    scanner.snapshot([
        entry(
            "/usr/bin/cap-tool",
            14,
            mode=0o755,
            xattrs={"security.capability": "cap_net_raw,cap_net_admin+ep"},
        )
    ])
    report = scanner.compare([
        entry(
            "/usr/bin/cap-tool",
            14,
            mode=0o755,
            xattrs={"security.capability": "cap_net_admin,cap_net_raw+ep"},
        )
    ])
    assert report["findings"] == []


def test_missing_file_is_skipped_not_fatal():
    scanner = make_scanner()
    scanner.snapshot([])
    report = scanner.compare([
        entry("/usr/bin/gone", 15, missing=True),
        entry("/usr/bin/new", 16),
    ])
    assert report["skipped"] == ["/usr/bin/gone"]
    assert [f["path"] for f in report["findings"]] == ["/usr/bin/new"]


def test_audit_row_contains_hash_and_capabilities():
    scanner = make_scanner()
    scanner.snapshot([])
    scanner.compare([
        entry(
            "/usr/bin/new",
            17,
            mode=0o4755,
            content_hash="hash-new",
            xattrs={"security.capability": "cap_net_raw+ep"},
        )
    ])
    row = scanner.audit()[-1]
    assert row["content_hash"] == "hash-new"
    assert row["caps"] == ["cap_net_raw"]
    assert row["path"] == "/usr/bin/new"


def test_replacement_with_new_inode_and_same_mtime_is_found():
    scanner = make_scanner()
    scanner.snapshot([entry("/usr/bin/a", 18, content_hash="same", mtime=100)])
    report = scanner.compare([entry("/usr/bin/a", 19, content_hash="same", mtime=100)])
    assert len(report["findings"]) == 1
    assert report["findings"][0]["kind"] == "replaced"


def test_scanners_are_isolated():
    strict = make_scanner()
    strict.add_mount("/mnt/data", ("nosuid", "rw"))
    normal = make_scanner()
    payload = entry("/mnt/data/tool", 20)
    strict.snapshot([])
    normal.snapshot([])
    assert strict.compare([payload])["findings"] == []
    assert len(normal.compare([payload])["findings"]) == 1
