"""Baseline for setuid/setgid and file-capability binaries.

Delivered as a failing shell: every public operation raises
``NotImplementedError`` until the scanner is implemented.

File entries are dicts with path, inode, mode, content_hash, mtime, size,
xattrs ({"security.capability": "cap_net_raw+ep"}), optional caps, optional
is_dir, optional overlay, and optional missing.  The baseline identity is
the pair inode + content hash; hard links collapse into one inode record.
Files on nosuid mounts are not risky, and world-writable non-sticky
directories are only reported on normal mounts.
"""


class BaselineError(Exception):
    """Baseline failure carrying a stable ``reason`` code."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class SuidCapabilityBaseline:
    """Build and compare a privilege baseline."""

    def __init__(
        self,
        exclude_prefixes=("/var/lib/docker", "/var/lib/containers", "/snap"),
        exemptions=(),
    ):
        self.exclude_prefixes = tuple(exclude_prefixes)
        self.exemptions = set(exemptions)
        self._mounts = {}
        self._baseline = None
        self._audit = []

    def add_mount(self, mountpoint, options=()):
        """Register mount options such as ("nosuid", "rw")."""
        raise NotImplementedError

    def add_exemption(self, path):
        """Add an exact exemption path.

        Wildcards raise ``BaselineError("wildcard_exemption")`` so a pattern
        cannot cover newly planted files.
        """
        raise NotImplementedError

    def snapshot(self, entries):
        """Build the baseline and return ``{"count", "by_inode"}``.

        Hard links share one inode record with sorted ``paths``.  If any
        alias is exempt the whole inode is skipped; container, missing, and
        nosuid entries are skipped too.  Capability names are compared as
        sets.
        """
        raise NotImplementedError

    def compare(self, entries):
        """Compare current entries with the stored baseline.

        Returns ``{"findings": [...], "skipped": [...]}``.  Findings have
        kind ("new_suid", "new_capability", "replaced",
        "world_writable_dir"), path, paths, inode, content_hash, caps, mode.
        A baseline path that now has a different inode is "replaced".
        Missing entries are listed under "skipped" and never abort the scan.
        """
        raise NotImplementedError

    def audit(self):
        """Return one audit row per finding, including hash and caps."""
        raise NotImplementedError
