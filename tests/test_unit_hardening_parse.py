import pytest

import unit_hardening_parse as uhp


def _file(name, *pairs):
    return {"name": name, "directives": list(pairs)}


def _codes(result):
    return [issue["code"] for issue in result["issues"]]


def test_dropins_merge_in_numeric_prefix_order():
    files = [
        _file("50-late.conf", ("Service", "ProtectSystem", "strict")),
        _file("main", ("Service", "ProtectSystem", "full")),
        _file("20-mid.conf", ("Service", "ProtectSystem", "yes")),
        _file("10-early.conf", ("Service", "ProtectSystem", "no")),
    ]
    result = uhp.UnitHardeningResolver().resolve("demo.service", files)
    assert result["order"] == ["main", "10-early.conf", "20-mid.conf", "50-late.conf"]
    assert result["values"]["ProtectSystem"] == "strict"
    assert result["sources"]["ProtectSystem"] == "50-late.conf"


def test_each_effective_value_records_its_source_file():
    files = [
        _file("main", ("Service", "NoNewPrivileges", "no")),
        _file("30-nnp.conf", ("Service", "NoNewPrivileges", "yes")),
    ]
    result = uhp.UnitHardeningResolver().resolve("demo.service", files)
    assert result["sources"]["NoNewPrivileges"] == "30-nnp.conf"
    assert result["values"]["NoNewPrivileges"] is True


def test_boolean_spellings_are_parsed_not_treated_as_strings():
    files = [
        _file("main", ("Service", "NoNewPrivileges", "yes")),
        _file("40-off.conf", ("Service", "NoNewPrivileges", "0")),
    ]
    result = uhp.UnitHardeningResolver().resolve("demo.service", files)
    assert result["values"]["NoNewPrivileges"] is False
    assert result["sources"]["NoNewPrivileges"] == "40-off.conf"


def test_empty_list_assignment_clears_previous_paths():
    files = [
        _file("main", ("Service", "ReadWritePaths", "/var/a /var/b")),
        _file("40-clear.conf", ("Service", "ReadWritePaths", "")),
    ]
    result = uhp.UnitHardeningResolver().resolve("demo.service", files)
    assert result["values"]["ReadWritePaths"] == []


def test_empty_boolean_assignment_resets_to_default_false():
    files = [
        _file("main", ("Service", "NoNewPrivileges", "yes")),
        _file("40-reset.conf", ("Service", "NoNewPrivileges", "")),
    ]
    result = uhp.UnitHardeningResolver().resolve("demo.service", files)
    assert result["values"]["NoNewPrivileges"] is False


def test_same_path_in_readonly_and_readwrite_is_reported_as_conflict():
    files = [
        _file(
            "main",
            ("Service", "ReadOnlyPaths", "/srv /etc"),
            ("Service", "ReadWritePaths", "/srv"),
        )
    ]
    result = uhp.UnitHardeningResolver().resolve("demo.service", files)
    assert "readonly_conflict" in _codes(result)


def test_no_new_privileges_with_setuid_program_is_reported():
    resolver = uhp.UnitHardeningResolver(setuid_programs={"/usr/bin/passwd"})
    files = [
        _file(
            "main",
            ("Service", "NoNewPrivileges", "yes"),
            ("Service", "ExecStart", "/usr/bin/passwd root"),
        )
    ]
    result = resolver.resolve("demo.service", files)
    assert "no_new_privileges_conflict" in _codes(result)


def test_private_tmp_namespaces_are_per_instance_and_not_fuzzy_matched():
    resolver = uhp.UnitHardeningResolver()
    files = [_file("main", ("Service", "PrivateTmp", "yes"))]
    first = resolver.resolve("worker@a.service", files)
    second = resolver.resolve("worker@b.service", files)
    typo = resolver.resolve("worker@a_b.service", files)
    assert first["private_tmp_namespace"]
    assert second["private_tmp_namespace"]
    assert first["private_tmp_namespace"] != second["private_tmp_namespace"]
    assert typo["private_tmp_namespace"] not in (
        first["private_tmp_namespace"],
        second["private_tmp_namespace"],
    )
    again = resolver.resolve("worker@a.service", files)
    assert again["private_tmp_namespace"] == first["private_tmp_namespace"]


def test_empty_address_family_restriction_means_deny_all():
    files = [_file("main", ("Service", "RestrictAddressFamilies", ""))]
    result = uhp.UnitHardeningResolver().resolve("demo.service", files)
    assert result["values"]["RestrictAddressFamilies"] == []
    assert "address_family_empty" in _codes(result)


def test_capability_plus_assignment_resets_before_adding():
    files = [
        _file("main", ("Service", "CapabilityBoundingSet", "CAP_CHOWN CAP_DAC_OVERRIDE")),
        _file("40-add.conf", ("Service", "CapabilityBoundingSet", "+CAP_SYS_ADMIN")),
    ]
    result = uhp.UnitHardeningResolver().resolve("demo.service", files)
    assert result["values"]["CapabilityBoundingSet"] == frozenset({"CAP_SYS_ADMIN"})


def test_unknown_syscall_group_is_an_error_not_a_silent_accept():
    files = [_file("main", ("Service", "SystemCallFilter", "@not-a-group"))]
    result = uhp.UnitHardeningResolver().resolve("demo.service", files)
    assert "unsupported_syscall_group" in _codes(result)


def test_directive_name_with_wrong_case_is_flagged():
    files = [_file("main", ("Service", "protectsystem", "strict"))]
    result = uhp.UnitHardeningResolver().resolve("demo.service", files)
    assert "directive_case" in _codes(result)
    assert "ProtectSystem" not in result["values"]


def test_unknown_directive_is_rejected_and_not_effective():
    files = [_file("main", ("Service", "Frobnicate", "1"))]
    result = uhp.UnitHardeningResolver().resolve("demo.service", files)
    unknown = [issue for issue in result["issues"] if issue["code"] == "unknown_directive"]
    assert unknown
    assert unknown[0]["level"] == "error"
    assert "Frobnicate" not in result["values"]


def test_home_readonly_and_bind_path_conflict_is_order_independent():
    first_files = [
        _file("main", ("Service", "ProtectHome", "read-only")),
        _file("10-mount.conf", ("Service", "BindPaths", "/home/alice")),
    ]
    second_files = [
        _file("main", ("Service", "BindPaths", "/home/alice")),
        _file("10-mount.conf", ("Service", "ProtectHome", "read-only")),
    ]
    resolver = uhp.UnitHardeningResolver()
    first = resolver.resolve("demo.service", first_files)
    second = resolver.resolve("demo.service", second_files)
    assert first["values"]["ProtectHome"] == second["values"]["ProtectHome"] == "read-only"
    assert first["values"]["BindPaths"] == second["values"]["BindPaths"] == ["/home/alice"]
    assert "path_conflict" in _codes(first)
    assert "path_conflict" in _codes(second)


def test_equal_prefix_dropins_are_tied_by_filename():
    files = [
        _file("main", ("Service", "ProtectSystem", "full")),
        _file("10-b.conf", ("Service", "ProtectSystem", "strict")),
        _file("10-a.conf", ("Service", "ProtectSystem", "yes")),
    ]
    result = uhp.UnitHardeningResolver().resolve("demo.service", files)
    assert result["order"] == ["main", "10-a.conf", "10-b.conf"]
    assert result["values"]["ProtectSystem"] == "strict"
    assert result["sources"]["ProtectSystem"] == "10-b.conf"
