"""Firmware rollback verification gate."""


_REQUIRED_PACKAGE_FIELDS = ("version", "model", "hardware", "signatures", "payload")


class FirmwareRollbackGate:
    """Verify an OTA firmware package against device rollback state.

    ``package`` fields: version (int or numeric string), model, hardware,
    incremental, baseline, payload, signatures.  A signature is
    ``{"signer", "key_id", "chain", "digest", "valid", "not_after"}`` where
    ``chain`` runs leaf -> root.  ``device`` fields: current_version, counter,
    secure_counter, partition_counters, active_partition, clock_synced and
    installed_versions.

    ``verify`` returns ``{"allowed": bool, "code": str, "reason": str,
    "version": <int>, "signers": [...]}``.  On success the device version and
    every partition counter move forward; failures never mutate the device.
    Every attempt is appended to ``self.audit_log`` with the target version
    and the signers seen.
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
        self.audit_log = []

    def verify(self, package, device, now=None, recovery=False):
        result = self._verify(package, device, now=now)
        self.audit_log.append(
            {
                "version": result["version"],
                "signers": list(result["signers"]),
                "code": result["code"],
                "allowed": result["allowed"],
                "recovery": bool(recovery),
            }
        )
        return result

    def _verify(self, package, device, now=None):
        if not isinstance(package, dict) or any(
            field not in package for field in _REQUIRED_PACKAGE_FIELDS
        ):
            return self._deny("invalid_package", "package is missing required fields")
        try:
            version = int(package["version"])
        except (TypeError, ValueError):
            return self._deny("invalid_package", "package version is not numeric")
        signatures = package["signatures"]
        if not isinstance(signatures, list):
            return self._deny("invalid_package", "signatures must be a list", version)

        if package["model"] != device.get("model"):
            return self._deny(
                "model_mismatch",
                "package model %r does not match device model %r"
                % (package["model"], device.get("model")),
                version,
            )
        if package["hardware"] != device.get("hardware"):
            return self._deny(
                "hardware_mismatch",
                "package hardware %r does not match device hardware %r"
                % (package["hardware"], device.get("hardware")),
                version,
            )

        counter = device.get("counter", 0)
        secure_counter = device.get("secure_counter", 0)
        partitions = device.get("partition_counters") or {}
        if counter < secure_counter or (
            partitions and counter < max(partitions.values())
        ):
            return self._deny(
                "counter_reset",
                "rollback counter %s is below the secure counter %s"
                % (counter, secure_counter),
                version,
            )
        if partitions and (
            len(set(partitions.values())) > 1
            or secure_counter not in partitions.values()
        ):
            return self._deny(
                "partition_desync",
                "partition counters %r are out of sync with the secure counter %s"
                % (partitions, secure_counter),
                version,
            )

        current = device.get("current_version", 0)
        if version < current:
            return self._deny(
                "rollback",
                "firmware version %s is older than installed version %s"
                % (version, current),
                version,
            )
        if version == current or version in device.get("installed_versions", []):
            return self._deny(
                "duplicate_version",
                "firmware version %s is already installed" % version,
                version,
            )

        if package.get("incremental"):
            baseline = package.get("baseline")
            if baseline is None:
                return self._deny(
                    "missing_baseline",
                    "incremental package does not declare a baseline version",
                    version,
                )
            if int(baseline) != current:
                return self._deny(
                    "baseline_mismatch",
                    "incremental baseline %s does not match installed version %s"
                    % (baseline, current),
                    version,
                )

        if not signatures:
            return self._deny(
                "missing_signature",
                "package carries no signatures",
                version,
            )

        if not device.get("clock_synced", False):
            return self._deny(
                "clock_untrusted",
                "device clock is not synchronized; failing closed",
                version,
            )

        signers = []
        for signature in signatures:
            code, reason = self._check_signature(signature, now)
            if code is not None:
                return self._deny(code, reason, version)
            signers.append(signature.get("signer"))

        if not self.dual_signers.issubset(set(signers)):
            return self._deny(
                "dual_sign_required",
                "dual-sign policy requires signers %r, got %r"
                % (sorted(self.dual_signers), signers),
                version,
            )

        device["current_version"] = version
        device["counter"] = version
        device["secure_counter"] = version
        for partition in device.get("partition_counters", {}):
            device["partition_counters"][partition] = version
        device.setdefault("installed_versions", []).append(version)
        return {
            "allowed": True,
            "code": "accepted",
            "reason": "firmware version %s accepted" % version,
            "version": version,
            "signers": signers,
        }

    def _check_signature(self, signature, now):
        key_id = signature.get("key_id")
        if key_id in self.revoked_keys:
            return "revoked_key", "signing key %r has been revoked" % key_id
        if not signature.get("valid", False):
            return "invalid_signature", "signature for key %r is invalid" % key_id
        chain = signature.get("chain") or []
        if not chain or chain[-1] not in self.trusted_roots:
            return (
                "chain_incomplete",
                "certificate chain does not terminate at a trusted root",
            )
        for intermediate in chain[1:-1]:
            if intermediate not in self.trusted_intermediates:
                return (
                    "untrusted_chain",
                    "intermediate certificate %r is not trusted" % intermediate,
                )
        not_after = signature.get("not_after")
        if not_after is not None and now is not None and now > not_after:
            return (
                "signature_expired",
                "signing certificate expired at %s (now %s)" % (not_after, now),
            )
        return None, ""

    @staticmethod
    def _deny(code, reason, version=None):
        return {
            "allowed": False,
            "code": code,
            "reason": reason,
            "version": version,
            "signers": [],
        }
