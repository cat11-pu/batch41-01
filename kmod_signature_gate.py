"""Kernel module signature gate (initial skeleton)."""


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
        self.digest_whitelist = tuple(digest_whitelist)
        self.kernel_vermagic = kernel_vermagic
        self.param_whitelist = dict(param_whitelist or {})
        self.enforce = enforce
        self._loaded = {}

    def load(self, module, available=None):
        raise NotImplementedError

    def unload(self, name):
        raise NotImplementedError

    def revoke(self, key_id):
        raise NotImplementedError

    def loaded(self, name=None):
        raise NotImplementedError
