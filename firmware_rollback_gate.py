"""Firmware rollback verification gate (initial skeleton)."""


class FirmwareRollbackGate:
    """Verify an OTA firmware package against device rollback state.

    ``package`` fields: version (int or numeric string), model, hardware,
    incremental, baseline, payload, signatures.  A signature is
    ``{"signer", "key_id", "chain", "digest", "valid", "not_after"}`` where
    ``chain`` runs leaf -> root.  ``device`` fields: current_version, counter,
    secure_counter, partition_counters, active_partition, clock_synced and
    installed_versions.

    ``verify`` returns ``{"allowed": bool, "code": str, "reason": str,
    "version": <int>}``.  On success the device version and every partition
    counter move forward; failures never mutate the device.
    """

    def __init__(
        self,
        trusted_roots=(),
        trusted_intermediates=(),
        revoked_keys=(),
        dual_signers=(),
    ):
        self.trusted_roots = set(trusted_roots)
        self.trusted_intermediates = set(trusted_intermediates)
        self.revoked_keys = set(revoked_keys)
        self.dual_signers = set(dual_signers)

    def verify(self, package, device, now=None, recovery=False):
        raise NotImplementedError
