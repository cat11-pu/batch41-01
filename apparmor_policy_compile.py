"""AppArmor profile merge compiler (initial skeleton)."""


class ProfileMergeCompiler:
    """Merge same-named profiles and compile effective rules.

    Rules are dicts.  File rules use ``{"kind": "file", "action": "allow" or
    "deny", "path": <pattern>}``; network rules use ``{"kind": "network",
    "action", "family", "protocol", "port"}``; subprofiles use
    ``{"kind": "subprofile", "name", "rules"}``; jumps use
    ``{"kind": "jump", "target"}``.  ``@{NAME}`` variables are expanded from
    the compiler variables and a value containing spaces remains one token.

    ``compile`` returns ``{"name", "rules", "subprofiles", "errors",
    "warnings", "sources"}``.  Unknown rule kinds are errors.  Deny wins over
    allow for the same path regardless of rule order.
    """

    def __init__(self, variables=None):
        self.variables = dict(variables or {})

    def set_variable(self, name, value):
        raise NotImplementedError

    def add_profile(self, name, rules, source="memory"):
        raise NotImplementedError

    def compile(self, name):
        raise NotImplementedError

    def path_matches(self, pattern, path):
        raise NotImplementedError

    def decision(self, name, path):
        raise NotImplementedError
