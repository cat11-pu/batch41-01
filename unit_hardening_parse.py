"""Hardening directive resolver for systemd-style units (initial skeleton)."""


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

    def resolve(self, unit, files):
        raise NotImplementedError

    def effective(self, unit, key):
        raise NotImplementedError
