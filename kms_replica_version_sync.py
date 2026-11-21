"""Cross-region KMS version reconciliation (initial skeleton)."""


class KmsReplicaVersionSync:
    """Reconcile key versions across regions.

    An event is ``{"key", "version", "region", "op", "material"}`` where
    ``op`` is ``put``, ``disable`` or ``delete`` and version is an int or a
    ``vN`` string compared numerically.  Each region has a monotonic version
    vector.  Delete tombstones survive compaction and block resurrection.
    Conflicting writes from two regions resolve deterministically in favour of
    the lexicographically smaller region id and are recorded in ``conflicts``.
    A read is routed to any region that already has the newest version.
    """

    def __init__(self, regions=("r1", "r2", "r3")):
        self.regions = tuple(regions)
        self.history = {}
        self.vector = {region: 0 for region in self.regions}
        self.conflicts = []
        self.audit = []
        self._compacted = set()

    def apply_event(self, event):
        raise NotImplementedError

    def sync(self, events):
        raise NotImplementedError

    def read(self, key, version=None, region=None):
        raise NotImplementedError

    def current_version(self, key):
        raise NotImplementedError

    def versions(self, key):
        raise NotImplementedError

    def lag(self, region):
        raise NotImplementedError

    def compact(self):
        raise NotImplementedError

    def import_material(self, key, version, material, checksum):
        raise NotImplementedError
