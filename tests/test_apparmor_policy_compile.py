import pytest

import apparmor_policy_compile as apc


def _file(action, path):
    return {"kind": "file", "action": action, "path": path}


def _file_rules(result):
    return [rule for rule in result["rules"] if rule["kind"] == "file"]


def test_same_named_profiles_merge_instead_of_replacing():
    compiler = apc.ProfileMergeCompiler()
    compiler.add_profile("web", [_file("allow", "/var/www")], source="a.conf")
    compiler.add_profile("web", [_file("allow", "/srv/www")], source="b.conf")
    result = compiler.compile("web")
    assert [rule["path"] for rule in _file_rules(result)] == [
        "/var/www",
        "/srv/www",
    ]


def test_deny_entries_from_first_profile_survive_the_merge():
    compiler = apc.ProfileMergeCompiler()
    compiler.add_profile(
        "web",
        [_file("allow", "/var/www"), _file("deny", "/var/www/secret")],
        source="a.conf",
    )
    compiler.add_profile("web", [_file("allow", "/srv/www")], source="b.conf")
    result = compiler.compile("web")
    pairs = {(rule["path"], rule["action"]) for rule in _file_rules(result)}
    assert ("/var/www/secret", "deny") in pairs
    assert compiler.decision("web", "/var/www/secret") == "deny"


def test_deny_wins_over_allow_in_either_source_order():
    compiler = apc.ProfileMergeCompiler()
    compiler.add_profile(
        "deny-first", [_file("deny", "/etc/shadow"), _file("allow", "/etc/shadow")]
    )
    compiler.add_profile(
        "allow-first", [_file("allow", "/etc/shadow"), _file("deny", "/etc/shadow")]
    )
    assert compiler.decision("deny-first", "/etc/shadow") == "deny"
    assert compiler.decision("allow-first", "/etc/shadow") == "deny"


def test_wildcard_does_not_match_a_parent_directory_traversal():
    compiler = apc.ProfileMergeCompiler()
    assert compiler.path_matches("/etc/*", "/etc/../secret") is False
    assert compiler.path_matches("/etc/*", "/etc/passwd") is True


def test_subprofiles_with_the_same_name_merge_their_subrules():
    compiler = apc.ProfileMergeCompiler()
    compiler.add_profile(
        "main",
        [{"kind": "subprofile", "name": "child", "rules": [_file("allow", "/a")]}],
        source="a.conf",
    )
    compiler.add_profile(
        "main",
        [{"kind": "subprofile", "name": "child", "rules": [_file("allow", "/b")]}],
        source="b.conf",
    )
    result = compiler.compile("main")
    assert [rule["path"] for rule in result["subprofiles"]["child"]] == ["/a", "/b"]


def test_variable_value_with_spaces_stays_a_single_token():
    compiler = apc.ProfileMergeCompiler({"@{HOME}": "/home/My User"})
    compiler.add_profile("p", [_file("allow", "@{HOME}/Docs/*")])
    rules = _file_rules(compiler.compile("p"))
    assert len(rules) == 1
    assert rules[0]["path"] == "/home/My User/Docs/*"


def test_variable_change_is_reflected_on_the_next_compile():
    compiler = apc.ProfileMergeCompiler({"@{DATA}": "/srv/data"})
    compiler.add_profile("p", [_file("allow", "@{DATA}/*")])
    assert _file_rules(compiler.compile("p"))[0]["path"] == "/srv/data/*"
    compiler.set_variable("@{DATA}", "/opt/data")
    assert _file_rules(compiler.compile("p"))[0]["path"] == "/opt/data/*"


def test_trailing_slash_rule_matches_both_file_and_directory():
    compiler = apc.ProfileMergeCompiler()
    compiler.add_profile("p", [_file("allow", "/var/log/")])
    rule = _file_rules(compiler.compile("p"))[0]
    assert rule["match_file"] is True
    assert rule["match_dir"] is True
    assert compiler.path_matches("/var/log/", "/var/log") is True
    assert compiler.path_matches("/var/log/", "/var/log/app.log") is True


def test_network_rule_without_port_gets_explicit_deny_semantics():
    compiler = apc.ProfileMergeCompiler()
    compiler.add_profile("p", [{"kind": "network", "action": "allow", "family": "inet"}])
    result = compiler.compile("p")
    network = [rule for rule in result["rules"] if rule["kind"] == "network"][0]
    assert network["port"] == 0
    assert network["protocol"] == "tcp"
    assert "network_port_unspecified" in [warning["code"] for warning in result["warnings"]]


def test_network_rule_without_family_is_an_error():
    compiler = apc.ProfileMergeCompiler()
    compiler.add_profile(
        "p", [{"kind": "network", "action": "allow", "protocol": "tcp", "port": 443}]
    )
    result = compiler.compile("p")
    assert "network_family_missing" in [error["code"] for error in result["errors"]]
    assert [rule for rule in result["rules"] if rule["kind"] == "network"] == []


def test_jump_to_a_missing_subprofile_is_an_error():
    compiler = apc.ProfileMergeCompiler()
    compiler.add_profile("p", [{"kind": "jump", "target": "missing"}])
    result = compiler.compile("p")
    assert "missing_jump_target" in [error["code"] for error in result["errors"]]


def test_unknown_rule_kind_is_an_error_and_is_not_compiled():
    compiler = apc.ProfileMergeCompiler()
    compiler.add_profile("p", [{"kind": "frobnicate", "action": "allow"}])
    result = compiler.compile("p")
    assert "unknown_rule" in [error["code"] for error in result["errors"]]
    assert [rule for rule in result["rules"] if rule["kind"] == "frobnicate"] == []


def test_compile_cache_is_invalidated_by_new_rules():
    compiler = apc.ProfileMergeCompiler()
    compiler.add_profile("p", [_file("allow", "/a")])
    assert len(compiler.compile("p")["rules"]) == 1
    compiler.add_profile("p", [_file("allow", "/b")])
    result = compiler.compile("p")
    assert len(result["rules"]) == 2
    assert compiler.decision("p", "/b") == "allow"


def test_every_compiled_rule_keeps_its_source_file():
    compiler = apc.ProfileMergeCompiler()
    compiler.add_profile("p", [_file("allow", "/a")], source="a.conf")
    compiler.add_profile("p", [_file("allow", "/b")], source="b.conf")
    result = compiler.compile("p")
    assert [rule["source"] for rule in result["rules"]] == ["a.conf", "b.conf"]
    assert result["sources"]["/a"] == "a.conf"
    assert result["sources"]["/b"] == "b.conf"


def test_compiler_instances_do_not_share_profiles_or_variables():
    first = apc.ProfileMergeCompiler({"@{ROOT}": "/one"})
    second = apc.ProfileMergeCompiler({"@{ROOT}": "/two"})
    first.add_profile("p", [_file("allow", "@{ROOT}/*")])
    second.add_profile("p", [_file("deny", "@{ROOT}/*")])
    first_result = first.compile("p")
    second_result = second.compile("p")
    assert _file_rules(first_result)[0]["path"] == "/one/*"
    assert _file_rules(first_result)[0]["action"] == "allow"
    assert _file_rules(second_result)[0]["path"] == "/two/*"
    assert _file_rules(second_result)[0]["action"] == "deny"
