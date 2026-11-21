"""Kernel module signature gate."""


class ModuleSignatureGate:
    """Validate a module before it is inserted into the kernel.

    A module dict has ``name``, ``version``, ``vermagic``, ``signature``,
    ``params``, ``deps`` and ``builtin``.  A signature is
    ``{"key_id", "issuer", "chain", "digest", "valid"}``.  ``chain`` runs
    leaf -> root.  ``available`` maps dependency names to module dicts.

    ``load`` returns ``{"ok": bool, "code": str, "reason": str}``.  Revocation
    is checked on every load, never served from a positive cache.
    ``loaded(name)`` returns ``{"name", "version", "key_id"}`` for a loaded
    module and ``None`` otherwise.
    """

    def __init__(
        self,
        trusted_keys=(),
        trusted_roots=(),
        trusted_intermediates=(),
        revoked_keys=(),
        digest_whitelist=("sha256", "sha384"),
        kernel_vermagic="5.15.0-1",
        param_whitelist=None,
        enforce=True,
    ):
        self.trusted_keys = set(trusted_keys)
        self.trusted_roots = set(trusted_roots)
        self.trusted_intermediates = set(trusted_intermediates)
        self.revoked_keys = set(revoked_keys)
        self.digest_whitelist = {
            self._normalise_digest(name) for name in digest_whitelist
        }
        self.kernel_vermagic = kernel_vermagic
        self.param_whitelist = dict(param_whitelist or {})
        self.enforce = enforce
        self._loaded = {}

    @staticmethod
    def _normalise_digest(name):
        if not isinstance(name, str):
            return None
        return name.strip().lower().replace("-", "").replace("_", "")

    @staticmethod
    def _fail(code, reason):
        return {"ok": False, "code": code, "reason": reason}

    @staticmethod
    def _ok(code, reason):
        return {"ok": True, "code": code, "reason": reason}

    def _validate_identity(self, module):
        if not isinstance(module, dict):
            return "module must be a dict"
        for field in ("name", "version", "vermagic"):
            value = module.get(field)
            if not isinstance(value, str) or not value:
                return "module field %r is missing or invalid" % field
        params = module.get("params", {})
        if not isinstance(params, dict):
            return "module params must be a dict"
        deps = module.get("deps", [])
        if not isinstance(deps, (list, tuple)):
            return "module deps must be a list"
        if any(not isinstance(dep, str) or not dep for dep in deps):
            return "module contains an invalid dependency name"
        return None

    def _validate_signature(self, module):
        signature = module.get("signature")
        if signature is None:
            return self._fail(
                "missing_signature",
                "module %r has no signature section" % module.get("name"),
            )
        if not isinstance(signature, dict):
            return self._fail(
                "bad_signature",
                "module %r signature is not a PKCS#7 signature object"
                % module.get("name"),
            )

        digest = self._normalise_digest(signature.get("digest"))
        if digest is None or digest not in self.digest_whitelist:
            return self._fail(
                "weak_digest",
                "module %r uses disallowed digest %r"
                % (module.get("name"), signature.get("digest")),
            )

        if signature.get("valid") is not True:
            return self._fail(
                "bad_signature",
                "PKCS#7 signature verification failed for module %r"
                % module.get("name"),
            )

        key_id = signature.get("key_id")
        if not isinstance(key_id, str) or not key_id:
            return self._fail(
                "bad_signature",
                "module %r signature has no signing key id" % module.get("name"),
            )
        if key_id not in self.trusted_keys:
            return self._fail(
                "untrusted_key",
                "signing key %r for module %r is not in the trusted keyring"
                % (key_id, module.get("name")),
            )

        chain = signature.get("chain")
        if not isinstance(chain, (list, tuple)) or not chain:
            return self._fail(
                "chain_broken",
                "module %r has no certificate chain" % module.get("name"),
            )
        if any(not isinstance(item, str) or not item for item in chain):
            return self._fail(
                "chain_broken",
                "module %r certificate chain contains an invalid entry"
                % module.get("name"),
            )
        if chain[0] != key_id:
            return self._fail(
                "chain_broken",
                "module %r certificate chain does not start at signing key %r"
                % (module.get("name"), key_id),
            )

        issuer = signature.get("issuer")
        if not isinstance(issuer, str) or not issuer:
            return self._fail(
                "chain_broken",
                "module %r signature does not identify its issuer"
                % module.get("name"),
            )
        if len(chain) < 2 or chain[1] != issuer:
            return self._fail(
                "chain_broken",
                "module %r certificate chain is missing issuer %r"
                % (module.get("name"), issuer),
            )

        for intermediate in chain[1:-1]:
            if intermediate not in self.trusted_intermediates:
                return self._fail(
                    "untrusted_intermediate",
                    "intermediate certificate %r for module %r is not trusted"
                    % (intermediate, module.get("name")),
                )

        root = chain[-1]
        if root not in self.trusted_roots:
            return self._fail(
                "untrusted_root",
                "root certificate %r for module %r is not trusted"
                % (root, module.get("name")),
            )

        for certificate in chain:
            if certificate in self.revoked_keys:
                return self._fail(
                    "revoked_key",
                    "certificate %r in module %r chain is revoked"
                    % (certificate, module.get("name")),
                )

        return None

    def _validate_params(self, module):
        params = module.get("params", {})
        allowed = self.param_whitelist.get(module["name"], ())
        if isinstance(allowed, str):
            allowed = (allowed,)
        allowed = set(allowed or ())
        rejected = sorted(str(name) for name in params if name not in allowed)
        if rejected:
            return self._fail(
                "param_not_allowed",
                "module %r has parameters outside the whitelist: %s"
                % (module["name"], ", ".join(rejected)),
            )
        return None

    def _validate_module(self, module, available, stack):
        if module["name"] in stack:
            return self._fail(
                "dependency_cycle",
                "dependency cycle detected at module %r" % module["name"],
            )

        is_builtin_exempt = bool(module.get("builtin")) and self.enforce
        if not is_builtin_exempt and self.enforce:
            failure = self._validate_signature(module)
            if failure is not None:
                return failure

        if module["vermagic"] != self.kernel_vermagic:
            return self._fail(
                "vermagic_mismatch",
                "module %r vermagic %r does not match kernel %r"
                % (module["name"], module["vermagic"], self.kernel_vermagic),
            )

        failure = self._validate_params(module)
        if failure is not None:
            return failure

        plan = []
        for dep_name in module.get("deps", []):
            dep = available.get(dep_name)
            if dep is None:
                loaded_dep = self._loaded.get(dep_name)
                if loaded_dep is None:
                    return self._fail(
                        "dependency_missing",
                        "dependency %r of module %r is not available"
                        % (dep_name, module["name"]),
                    )
                if loaded_dep["key_id"] in self.revoked_keys:
                    return self._fail(
                        "revoked_key",
                        "loaded dependency %r uses revoked key %r"
                        % (dep_name, loaded_dep["key_id"]),
                    )
                continue

            if not isinstance(dep, dict):
                return self._fail(
                    "invalid_dependency",
                    "dependency %r of module %r is not a module dict"
                    % (dep_name, module["name"]),
                )
            if dep.get("name") != dep_name:
                return self._fail(
                    "invalid_dependency",
                    "dependency %r resolves to module %r"
                    % (dep_name, dep.get("name")),
                )
            identity_error = self._validate_identity(dep)
            if identity_error is not None:
                return self._fail(
                    "invalid_dependency",
                    "dependency %r is invalid: %s" % (dep_name, identity_error),
                )

            dep_failure = self._validate_module(
                dep, available, stack | {module["name"]}
            )
            if isinstance(dep_failure, dict):
                code = dep_failure["code"]
                if code == "missing_signature":
                    code = "dependency_unsigned"
                return self._fail(
                    code,
                    "dependency %r of module %r failed verification: %s"
                    % (dep_name, module["name"], dep_failure["reason"]),
                )
            plan.extend(dep_failure)

        plan.append(module)
        return plan

    def _signature_key(self, module):
        if module.get("builtin") and self.enforce:
            return None
        signature = module.get("signature") or {}
        return signature.get("key_id")

    def load(self, module, available=None):
        identity_error = self._validate_identity(module)
        if identity_error is not None:
            return self._fail("invalid_module", identity_error)

        available = available if isinstance(available, dict) else {}
        try:
            plan = self._validate_module(module, available, set())
        except (KeyError, TypeError) as exc:
            return self._fail(
                "invalid_module",
                "module %r could not be validated: %s" % (module.get("name"), exc),
            )
        if isinstance(plan, dict):
            return plan

        for entry in plan:
            name = entry["name"]
            existing = self._loaded.get(name)
            if existing is not None and existing["version"] != entry["version"]:
                return self._fail(
                    "name_conflict",
                    "module %r version %r is already loaded, cannot load version %r"
                    % (name, existing["version"], entry["version"]),
                )

        if module["name"] in self._loaded:
            existing = self._loaded[module["name"]]
            return self._fail(
                "name_conflict",
                "module %r version %r is already loaded"
                % (module["name"], existing["version"]),
            )

        for entry in plan:
            self._loaded[entry["name"]] = {
                "name": entry["name"],
                "version": entry["version"],
                "key_id": self._signature_key(entry),
            }

        if module.get("builtin") and self.enforce:
            return self._ok(
                "builtin_exempt",
                "builtin module %r is exempt from signature verification"
                % module["name"],
            )
        return self._ok(
            "loaded", "module %r passed signature verification" % module["name"]
        )

    def unload(self, name):
        return self._loaded.pop(name, None) is not None

    def revoke(self, key_id):
        self.revoked_keys.add(key_id)

    def loaded(self, name=None):
        if name is None:
            return [dict(entry) for entry in self._loaded.values()]
        entry = self._loaded.get(name)
        return dict(entry) if entry is not None else None
