"""SELinux boolean and policy module gate (initial skeleton)."""


class SelinuxBooleanGate:
    """Manage booleans, policy modules, relabels and access decisions.

    ``booleans`` maps a name to ``{"default": bool, "requires": [names]}``.
    ``filesystem`` maps a directory to its direct children and ``contexts``
    maps existing paths to contexts.  Module dicts contain ``name``,
    ``priority``, ``version``, ``types``, ``requires_types`` and
    ``type_context``.

    Boolean names are normalised to lower case with ``-`` mapped to ``_``.
    Boolean values are fail-closed.  ``load_module`` compares
    ``(priority, version)`` numerically; a lower pair is a rollback.  Module
    loads never leave partial state behind when a batch fails.
    """

    def __init__(self, enforcing=True, booleans=None, filesystem=None, contexts=None):
        self.enforcing = enforcing
        self.booleans = dict(booleans or {})
        self.filesystem = dict(filesystem or {})
        self.contexts = dict(contexts or {})
        self.permissive_domains = set()
        self.permissions = {}
        self.modules = {}
        self.audit = []

    def set_boolean(self, name, value, persist=True):
        raise NotImplementedError

    def get_boolean(self, name):
        raise NotImplementedError

    def state(self):
        raise NotImplementedError

    def reboot(self):
        raise NotImplementedError

    def load_module(self, module):
        raise NotImplementedError

    def load_batch(self, modules):
        raise NotImplementedError

    def unload_module(self, name):
        raise NotImplementedError

    def relabel(self, path, context, force=False):
        raise NotImplementedError

    def mark_permissive(self, domain):
        raise NotImplementedError

    def set_domain_permissions(self, domain, permissions):
        raise NotImplementedError

    def decide(self, domain, permission):
        raise NotImplementedError
