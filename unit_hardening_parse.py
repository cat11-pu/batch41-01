"""Hardening directive resolver for systemd-style units."""

import hashlib
import re
import shlex

_BOOL_TRUE = {"yes", "true", "1", "on"}
_BOOL_FALSE = {"no", "false", "0", "off"}

_BOOL_KEYS = {"NoNewPrivileges", "PrivateTmp"}
_LIST_KEYS = {
    "ReadOnlyPaths",
    "ReadWritePaths",
    "BindPaths",
    "RestrictAddressFamilies",
    "SystemCallFilter",
}
_SET_KEYS = {"CapabilityBoundingSet"}
_SCALAR_KEYS = {"ProtectSystem", "ProtectHome", "ExecStart"}

_KNOWN_KEYS = _BOOL_KEYS | _LIST_KEYS | _SET_KEYS | _SCALAR_KEYS
_KNOWN_KEYS_LOWER = {key.lower(): key for key in _KNOWN_KEYS}

_SCALAR_DEFAULTS = {"ProtectSystem": "no", "ProtectHome": "no", "ExecStart": ""}

_KNOWN_SYSCALL_GROUPS = {
    "@aio",
    "@basic-io",
    "@chown",
    "@clock",
    "@cpu-emulation",
    "@debug",
    "@default",
    "@file-system",
    "@io-event",
    "@ipc",
    "@keyring",
    "@memlock",
    "@memory",
    "@module",
    "@mount",
    "@network-io",
    "@obsolete",
    "@pkey",
    "@privileged",
    "@process",
    "@raw-io",
    "@reboot",
    "@resources",
    "@setuid",
    "@signal",
    "@swap",
    "@sync",
    "@system-service",
    "@timer",
}

_PROTECT_HOME_VALUES = {"yes", "read-only", "tmpfs"}
_PROTECT_HOME_PREFIXES = ("/home", "/root", "/run/user")


def _dropin_sort_key(name):
    match = re.match(r"(\d+)", name)
    prefix = int(match.group(1)) if match else -1
    return (prefix, name)


def _parse_bool(raw):
    text = raw.strip().lower()
    if text in _BOOL_TRUE:
        return True, True
    if text in _BOOL_FALSE:
        return False, True
    return False, False


def _merge_capability_set(value):
    """Every assignment resets the set first, then applies +/- tokens."""
    result = set()
    for token in value.split():
        if token.startswith(("+",)):
            token = token[1:]
            if token:
                result.add(token)
        elif token.startswith(("-", "~")):
            result.discard(token[1:])
        elif token:
            result.add(token)
    return frozenset(result)


class UnitHardeningResolver:
    """Resolve a base unit plus ordered drop-ins.

    ``files`` is a list of ``{"name": str, "directives": [(section, key, value)]}``.
    The file named ``main`` is the base unit; the others are drop-ins ordered by
    their numeric filename prefix and then by filename.  Later files win for
    scalar values; list values are replaced on assignment and ``""`` clears a
    list or resets a boolean to its default (False).

    Recognised case-sensitive directives: ProtectSystem, ProtectHome,
    ReadOnlyPaths, ReadWritePaths, BindPaths, NoNewPrivileges, PrivateTmp,
    RestrictAddressFamilies, CapabilityBoundingSet, SystemCallFilter, ExecStart.

    The resolved dict has keys ``unit``, ``order``, ``values``, ``sources``,
    ``issues`` and ``private_tmp_namespace``.  Issues are
    ``{"level", "code", "key", "source", "detail"}`` records.
    """

    def __init__(self, strict=True, setuid_programs=()):
        self.strict = strict
        self.setuid_programs = set(setuid_programs)
        self._resolved = {}

    def _issue(self, issues, level, code, key, source, detail):
        if level == "error" and not self.strict:
            level = "warning"
        issues.append(
            {
                "level": level,
                "code": code,
                "key": key,
                "source": source,
                "detail": detail,
            }
        )

    def resolve(self, unit, files):
        mains = [f for f in files if f["name"] == "main"]
        dropins = sorted(
            (f for f in files if f["name"] != "main"),
            key=lambda f: _dropin_sort_key(f["name"]),
        )
        ordered = mains + dropins
        order = [f["name"] for f in ordered]

        values = {}
        sources = {}
        issues = []

        for file_entry in ordered:
            name = file_entry["name"]
            for _section, key, raw_value in file_entry["directives"]:
                value = raw_value if raw_value is not None else ""
                if key not in _KNOWN_KEYS:
                    canonical = _KNOWN_KEYS_LOWER.get(key.lower())
                    if canonical is not None:
                        self._issue(
                            issues,
                            "error",
                            "directive_case",
                            key,
                            name,
                            "directive names are case-sensitive; "
                            "did you mean '%s'?" % canonical,
                        )
                    else:
                        self._issue(
                            issues,
                            "error",
                            "unknown_directive",
                            key,
                            name,
                            "unknown directive '%s' is not applied" % key,
                        )
                    continue

                if key in _BOOL_KEYS:
                    if value.strip() == "":
                        values[key] = False
                    else:
                        parsed, ok = _parse_bool(value)
                        if not ok:
                            self._issue(
                                issues,
                                "warning",
                                "invalid_boolean",
                                key,
                                name,
                                "unrecognised boolean value %r; treated as false"
                                % value,
                            )
                        values[key] = parsed
                elif key in _SET_KEYS:
                    values[key] = _merge_capability_set(value)
                elif key in _LIST_KEYS:
                    items = value.split()
                    if key == "RestrictAddressFamilies" and not items:
                        self._issue(
                            issues,
                            "warning",
                            "address_family_empty",
                            key,
                            name,
                            "empty RestrictAddressFamilies denies all "
                            "address families",
                        )
                    if key == "SystemCallFilter":
                        for token in items:
                            group = token.lstrip("~-")
                            if (
                                group.startswith("@")
                                and group not in _KNOWN_SYSCALL_GROUPS
                            ):
                                self._issue(
                                    issues,
                                    "error",
                                    "unsupported_syscall_group",
                                    key,
                                    name,
                                    "syscall group '%s' is not supported"
                                    % group,
                                )
                    values[key] = items
                else:
                    values[key] = (
                        value if value.strip() else _SCALAR_DEFAULTS.get(key, "")
                    )
                sources[key] = name

        self._check_conflicts(values, sources, issues)

        if values.get("PrivateTmp"):
            digest = hashlib.sha256(unit.encode("utf-8")).hexdigest()[:16]
            namespace = "systemd-private-%s-%s" % (digest, unit)
        else:
            namespace = None

        result = {
            "unit": unit,
            "order": order,
            "values": values,
            "sources": sources,
            "issues": issues,
            "private_tmp_namespace": namespace,
        }
        self._resolved[unit] = result
        return result

    def _check_conflicts(self, values, sources, issues):
        readonly = set(values.get("ReadOnlyPaths", []))
        readwrite = set(values.get("ReadWritePaths", []))
        for path in sorted(readonly & readwrite):
            self._issue(
                issues,
                "error",
                "readonly_conflict",
                "ReadWritePaths",
                sources.get("ReadWritePaths"),
                "path to be both read-only and read-write: %s" % path,
            )

        protect_home = values.get("ProtectHome")
        if protect_home in _PROTECT_HOME_VALUES:
            for path in values.get("BindPaths", []):
                if any(
                    path == prefix or path.startswith(prefix + "/")
                    for prefix in _PROTECT_HOME_PREFIXES
                ):
                    self._issue(
                        issues,
                        "warning",
                        "path_conflict",
                        "BindPaths",
                        sources.get("BindPaths"),
                        "BindPaths '%s' is hidden by ProtectHome=%s"
                        % (path, protect_home),
                    )

        if values.get("NoNewPrivileges") and self.setuid_programs:
            exec_start = values.get("ExecStart", "")
            try:
                argv = shlex.split(exec_start)
            except ValueError:
                argv = exec_start.split()
            program = argv[0] if argv else ""
            if program in self.setuid_programs:
                self._issue(
                    issues,
                    "error",
                    "no_new_privileges_conflict",
                    "NoNewPrivileges",
                    sources.get("NoNewPrivileges"),
                    "NoNewPrivileges=yes neutralises setuid program '%s'"
                    % program,
                )

    def effective(self, unit, key):
        result = self._resolved.get(unit)
        if result is None:
            return None
        return result["values"].get(key)
