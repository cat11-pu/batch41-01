"""Baseline for setuid/setgid and file-capability binaries.

File entries are dicts with path, inode, mode, content_hash, mtime, size,
xattrs ({"security.capability": "cap_net_raw+ep"}), optional caps, optional
is_dir, optional overlay, and optional missing.  The baseline identity is
the pair inode + content hash; hard links collapse into one inode record.
Files on nosuid mounts are not risky, and world-writable non-sticky
directories are only reported on normal mounts.
"""

import glob
import os
import re


_CAPABILITY_RE = re.compile(r"cap_[a-z0-9_]+", re.IGNORECASE)
_PRIVILEGE_BITS = 0o6000
_WORLD_WRITABLE = 0o002
_STICKY = 0o1000


def _normalize_mountpoint(mountpoint):
    path = os.fspath(mountpoint)
    return path.rstrip("/") or "/"


def _normalize_prefix(prefix):
    path = os.fspath(prefix)
    return path.rstrip("/") or "/"


def _mode(entry):
    try:
        return int(entry.get("mode") or 0)
    except (TypeError, ValueError):
        return 0


def _has_privilege_bits(entry):
    return bool(_mode(entry) & _PRIVILEGE_BITS)


def _paths(group):
    return sorted({entry.get("path") for entry in group if entry.get("path")})


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
        self.exclude_prefixes = tuple(
            _normalize_prefix(prefix) for prefix in exclude_prefixes
        )
        self.exemptions = set()
        for path in exemptions:
            self.add_exemption(path)
        self._mounts = {}
        self._baseline = None
        self._audit = []

    def add_mount(self, mountpoint, options=()):
        """Register mount options such as ("nosuid", "rw")."""
        if isinstance(options, str):
            options = options.split(",")
        self._mounts[_normalize_mountpoint(mountpoint)] = frozenset(
            str(option).strip().lower() for option in options if str(option).strip()
        )

    def add_exemption(self, path):
        """Add an exact exemption path.

        Wildcards raise ``BaselineError("wildcard_exemption")`` so a pattern
        cannot cover newly planted files.
        """
        path = os.fspath(path)
        if glob.has_magic(path):
            raise BaselineError("wildcard_exemption")
        self.exemptions.add(path)

    def snapshot(self, entries):
        """Build the baseline and return ``{"count", "by_inode"}``.

        Hard links share one inode record with sorted ``paths``.  If any
        alias is exempt the whole inode is skipped; container, missing, and
        nosuid entries are skipped too.  Capability names are compared as
        sets.
        """
        by_inode = {}
        for inode, group in self._group_entries(entries).items():
            paths = _paths(group)
            if any(path in self.exemptions for path in paths):
                continue

            usable = [entry for entry in group if self._is_scannable(entry)]
            if not usable:
                continue

            usable.sort(key=lambda entry: entry.get("path") or "")
            record = usable[0]
            caps = self._capabilities(record)
            if record.get("is_dir") or not (_has_privilege_bits(record) or caps):
                continue

            by_inode[inode] = {
                "inode": inode,
                "paths": paths,
                "content_hash": record.get("content_hash"),
                "caps": caps,
                "mode": _mode(record),
                "xattrs": dict(record.get("xattrs") or {}),
            }

        self._baseline = {"count": len(by_inode), "by_inode": by_inode}
        return self._baseline

    def compare(self, entries):
        """Compare current entries with the stored baseline.

        Returns ``{"findings": [...], "skipped": [...]}``.  Findings have
        kind ("new_suid", "new_capability", "replaced",
        "world_writable_dir"), path, paths, inode, content_hash, caps, mode.
        A baseline path that now has a different inode is "replaced".
        Missing entries are listed under "skipped" and never abort the scan.
        """
        entries = list(entries)
        baseline = self._baseline["by_inode"] if self._baseline else {}
        baseline_paths = {
            path: record
            for record in baseline.values()
            for path in record.get("paths", ())
        }

        findings = []
        skipped = []
        seen_skipped = set()
        for entry in entries:
            if entry and entry.get("missing"):
                path = entry.get("path")
                if path not in seen_skipped:
                    skipped.append(path)
                    seen_skipped.add(path)

        for inode, group in self._group_entries(entries).items():
            paths = _paths(group)
            if any(path in self.exemptions for path in paths):
                continue

            usable = [entry for entry in group if self._is_scannable(entry)]
            if not usable:
                continue
            usable.sort(key=lambda entry: entry.get("path") or "")

            directories = [entry for entry in usable if entry.get("is_dir")]
            if directories:
                directory = directories[0]
                mode = _mode(directory)
                if mode & _WORLD_WRITABLE and not mode & _STICKY:
                    findings.append(
                        self._finding(
                            "world_writable_dir", inode, paths, directory, []
                        )
                    )
                continue

            current = usable[0]
            caps = self._capabilities(current)
            content_hash = current.get("content_hash")
            previous = baseline.get(inode)

            if previous is not None:
                if content_hash != previous.get("content_hash"):
                    kind = "replaced"
                elif set(caps) != set(previous.get("caps", ())):
                    kind = "new_capability"
                elif _has_privilege_bits(current) and not (
                    _mode(previous) & _PRIVILEGE_BITS
                ):
                    kind = "new_suid"
                else:
                    kind = None
            elif any(path in baseline_paths for path in paths):
                kind = "replaced"
            elif caps:
                kind = "new_capability"
            elif _has_privilege_bits(current):
                kind = "new_suid"
            else:
                kind = None

            if kind is not None:
                findings.append(self._finding(kind, inode, paths, current, caps))

        findings.sort(key=lambda finding: (finding["path"] or "", finding["kind"]))
        self._audit.extend(
            {
                **finding,
                "paths": list(finding["paths"]),
                "caps": list(finding["caps"]),
            }
            for finding in findings
        )
        return {"findings": findings, "skipped": skipped}

    def audit(self):
        """Return one audit row per finding, including hash and caps."""
        return [
            {**row, "paths": list(row["paths"]), "caps": list(row["caps"])}
            for row in self._audit
        ]

    def _group_entries(self, entries):
        groups = {}
        for entry in entries:
            if not entry:
                continue
            groups.setdefault(entry.get("inode"), []).append(entry)
        return groups

    def _is_scannable(self, entry):
        path = entry.get("path") or ""
        if entry.get("missing") or self._is_excluded(entry) or self._is_nosuid(path):
            return False
        return True

    def _is_excluded(self, entry):
        if entry.get("overlay"):
            return True
        path = entry.get("path") or ""
        for prefix in self.exclude_prefixes:
            if prefix == "/" or path == prefix or path.startswith(prefix + "/"):
                return True
        return False

    def _is_nosuid(self, path):
        options = frozenset()
        best_length = -1
        for mountpoint, mount_options in self._mounts.items():
            matches = (
                mountpoint == "/"
                or path == mountpoint
                or path.startswith(mountpoint + "/")
            )
            if matches and len(mountpoint) > best_length:
                options = mount_options
                best_length = len(mountpoint)
        return "nosuid" in options

    @staticmethod
    def _capabilities(entry):
        xattrs = entry.get("xattrs") or {}
        raw = xattrs.get("security.capability")
        if raw is None:
            raw = entry.get("caps")
        if raw is None:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        if isinstance(raw, (list, tuple, set, frozenset)):
            raw = " ".join(str(item) for item in raw)
        return sorted(
            {match.group(0).lower() for match in _CAPABILITY_RE.finditer(str(raw))}
        )

    @staticmethod
    def _finding(kind, inode, paths, entry, caps):
        return {
            "kind": kind,
            "path": entry.get("path"),
            "paths": paths,
            "inode": inode,
            "content_hash": entry.get("content_hash"),
            "caps": list(caps),
            "mode": _mode(entry),
        }
