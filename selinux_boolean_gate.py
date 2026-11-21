"""SELinux boolean and policy module gate."""

import copy
import posixpath
import threading


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
        self._lock = threading.RLock()
        self.enforcing = bool(enforcing)

        self.booleans = {}
        for raw_name, raw_metadata in dict(booleans or {}).items():
            name = self._normalize_boolean_name(raw_name)
            if not name:
                continue
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            default = metadata.get("default", False)
            if not isinstance(default, bool):
                default = False
            requires = []
            raw_requires = metadata.get("requires", [])
            if isinstance(raw_requires, str):
                raw_requires = [raw_requires]
            for dependency in raw_requires or []:
                normalized = self._normalize_boolean_name(dependency)
                if normalized and normalized not in requires:
                    requires.append(normalized)
            self.booleans[name] = {
                "default": default,
                "requires": requires,
            }

        self.filesystem = copy.deepcopy(dict(filesystem or {}))
        self.contexts = copy.deepcopy(dict(contexts or {}))
        self.permissive_domains = set()
        self.permissions = {}
        self.modules = {}
        self.audit = []

        self._runtime = {
            name: metadata["default"] for name, metadata in self.booleans.items()
        }
        self._persisted = dict(self._runtime)

    @staticmethod
    def _normalize_boolean_name(name):
        if not isinstance(name, str):
            return ""
        return name.strip().lower().replace("-", "_")

    @staticmethod
    def _as_string_list(value, field):
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            raise ValueError(field)
        result = []
        for item in value:
            if not isinstance(item, str) or not item:
                raise ValueError(field)
            if item not in result:
                result.append(item)
        return result

    @staticmethod
    def _is_number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def _validate_module(self, module):
        if not isinstance(module, dict):
            return None, "invalid_module"

        name = module.get("name")
        priority = module.get("priority")
        version = module.get("version")
        if not isinstance(name, str) or not name.strip():
            return None, "invalid_module"
        if not self._is_number(priority) or not self._is_number(version):
            return None, "invalid_module"

        try:
            types = self._as_string_list(module.get("types", []), "types")
            requires_types = self._as_string_list(
                module.get("requires_types", []), "requires_types"
            )
        except ValueError:
            return None, "invalid_module"

        type_context = module.get("type_context", {})
        if type_context is None:
            type_context = {}
        if not isinstance(type_context, dict) or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in type_context.items()
        ):
            return None, "invalid_module"

        normalized = copy.deepcopy(module)
        normalized["name"] = name.strip()
        normalized["types"] = types
        normalized["requires_types"] = requires_types
        normalized["type_context"] = copy.deepcopy(type_context)
        return normalized, None

    @staticmethod
    def _available_types(modules):
        available = set()
        for module in modules.values():
            available.update(module.get("types", []))
        return available

    def _prepare_module_load(self, modules, module, validate_requirements=True):
        normalized, error = self._validate_module(module)
        if error:
            return None, {"ok": False, "code": error}

        name = normalized["name"]
        candidate = copy.deepcopy(modules)
        existing = candidate.get(name)
        if existing is not None:
            old_version = (existing["priority"], existing["version"])
            new_version = (normalized["priority"], normalized["version"])
            if new_version == old_version:
                return candidate, {
                    "ok": True,
                    "code": "already_loaded",
                    "name": name,
                }
            if new_version < old_version:
                code = (
                    "version_rollback"
                    if new_version[0] == old_version[0]
                    else "priority_rollback"
                )
                return None, {
                    "ok": False,
                    "code": code,
                    "name": name,
                    "current": {
                        "priority": existing["priority"],
                        "version": existing["version"],
                    },
                }

        candidate[name] = normalized
        if validate_requirements:
            available = self._available_types(candidate)
            for other_name, other in candidate.items():
                missing = [
                    required
                    for required in other["requires_types"]
                    if required not in available
                ]
                if missing:
                    return None, {
                        "ok": False,
                        "code": "missing_type",
                        "name": other_name,
                        "missing": missing,
                    }

        return candidate, {
            "ok": True,
            "code": "replaced" if existing is not None else "loaded",
            "name": name,
        }

    def set_boolean(self, name, value, persist=True):
        with self._lock:
            normalized = self._normalize_boolean_name(name)
            if not normalized or normalized not in self.booleans:
                return {
                    "ok": False,
                    "code": "unknown_boolean",
                    "name": normalized,
                }
            if not isinstance(value, bool):
                return {
                    "ok": False,
                    "code": "invalid_value",
                    "name": normalized,
                }

            missing = []
            if value:
                missing = [
                    dependency
                    for dependency in self.booleans[normalized]["requires"]
                    if not self._runtime.get(dependency, False)
                ]
            if missing:
                return {
                    "ok": False,
                    "code": "dependency_missing",
                    "name": normalized,
                    "missing": missing,
                }

            old = self._runtime[normalized]
            self._runtime[normalized] = value
            if persist:
                self._persisted[normalized] = value

            entry = {
                "name": normalized,
                "old": old,
                "new": value,
                "persisted": bool(persist),
            }
            self.audit.append(entry)
            return {
                "ok": True,
                "code": "updated" if old != value else "unchanged",
                **entry,
            }

    def get_boolean(self, name):
        with self._lock:
            normalized = self._normalize_boolean_name(name)
            return bool(self._runtime.get(normalized, False))

    def state(self):
        with self._lock:
            return {
                "runtime": dict(self._runtime),
                "persisted": dict(self._persisted),
            }

    def reboot(self):
        with self._lock:
            self._runtime = dict(self._persisted)
            return {
                "runtime": dict(self._runtime),
                "persisted": dict(self._persisted),
            }

    def load_module(self, module):
        with self._lock:
            candidate, result = self._prepare_module_load(self.modules, module)
            if not result["ok"]:
                return result
            if result["code"] != "already_loaded":
                self.modules = candidate
            return result

    def load_batch(self, modules):
        with self._lock:
            if isinstance(modules, (str, bytes, dict)) or modules is None:
                return {"ok": False, "code": "invalid_module"}
            try:
                module_list = list(modules)
            except TypeError:
                return {"ok": False, "code": "invalid_module"}

            candidate = copy.deepcopy(self.modules)
            results = []
            for module in module_list:
                candidate, result = self._prepare_module_load(
                    candidate, module, validate_requirements=False
                )
                if not result["ok"]:
                    return {
                        "ok": False,
                        "code": result["code"],
                        "module": result.get("name"),
                        "missing": result.get("missing", []),
                    }
                results.append(result)

            available = self._available_types(candidate)
            for name, module in candidate.items():
                missing = [
                    required
                    for required in module["requires_types"]
                    if required not in available
                ]
                if missing:
                    return {
                        "ok": False,
                        "code": "missing_type",
                        "module": name,
                        "missing": missing,
                    }

            self.modules = candidate
            return {
                "ok": True,
                "code": "loaded",
                "modules": [result["name"] for result in results],
            }

    def unload_module(self, name):
        with self._lock:
            if name not in self.modules:
                return {"ok": False, "code": "not_loaded", "name": name}

            remaining = copy.deepcopy(self.modules)
            removed = remaining.pop(name)
            available = self._available_types(remaining)

            for other_name, other in remaining.items():
                missing = [
                    required
                    for required in other["requires_types"]
                    if required in removed.get("types", [])
                    and required not in available
                ]
                if missing:
                    return {
                        "ok": False,
                        "code": "type_in_use",
                        "name": name,
                        "required_by": other_name,
                        "missing": missing,
                    }

            self.modules = remaining
            return {"ok": True, "code": "unloaded", "name": name}

    @staticmethod
    def _is_below(path, root):
        if root == "/":
            return isinstance(path, str) and path.startswith("/")
        return path == root or (
            isinstance(path, str) and path.startswith(root + "/")
        )

    def _children_of(self, path):
        raw_children = self.filesystem.get(path, [])
        if isinstance(raw_children, dict):
            raw_children = list(raw_children)
        elif isinstance(raw_children, str):
            raw_children = [raw_children]
        elif not isinstance(raw_children, (list, tuple, set)):
            raw_children = []

        children = []
        for child in raw_children:
            if not isinstance(child, str) or not child:
                continue
            if not child.startswith("/"):
                child = posixpath.join(path, child)
            if child != path:
                children.append(child)
        return children

    def _relabel_paths(self, path):
        paths = {path}
        pending = [path]
        while pending:
            current = pending.pop()
            for child in self._children_of(current):
                if child not in paths:
                    paths.add(child)
                    pending.append(child)

        known_paths = set(self.filesystem)
        known_paths.update(self.contexts)
        for known in known_paths:
            if isinstance(known, str) and self._is_below(known, path):
                paths.add(known)
        return paths

    def relabel(self, path, context, force=False):
        with self._lock:
            if not isinstance(path, str) or not path:
                return {"ok": False, "code": "invalid_path"}
            if not isinstance(context, str) or not context:
                return {"ok": False, "code": "invalid_context"}

            path = path.rstrip("/") or "/"
            paths = self._relabel_paths(path)
            conflicts = sorted(
                candidate
                for candidate in paths
                if not force
                and candidate in self.contexts
                and self.contexts[candidate] != context
            )
            if conflicts:
                return {
                    "ok": False,
                    "code": "context_conflict",
                    "path": path,
                    "conflicts": conflicts,
                }

            for candidate in paths:
                self.contexts[candidate] = context
            return {
                "ok": True,
                "code": "relabeled",
                "path": path,
                "paths": sorted(paths),
            }

    def mark_permissive(self, domain):
        with self._lock:
            if not isinstance(domain, str) or not domain:
                return {"ok": False, "code": "invalid_domain"}
            self.permissive_domains.add(domain)
            return {"ok": True, "code": "permissive", "domain": domain}

    def set_domain_permissions(self, domain, permissions):
        with self._lock:
            if not isinstance(domain, str) or not domain:
                return {"ok": False, "code": "invalid_domain"}
            if isinstance(permissions, str):
                permissions = [permissions]
            try:
                values = set(permissions or [])
            except TypeError:
                return {"ok": False, "code": "invalid_permissions"}
            if any(not isinstance(value, str) or not value for value in values):
                return {"ok": False, "code": "invalid_permissions"}
            self.permissions[domain] = values
            return {"ok": True, "code": "updated", "domain": domain}

    def decide(self, domain, permission):
        with self._lock:
            if not self.enforcing:
                return {
                    "allowed": True,
                    "enforced": False,
                    "mode": "permissive",
                    "reason": "global",
                }
            if domain in self.permissive_domains:
                return {
                    "allowed": True,
                    "enforced": False,
                    "mode": "permissive",
                    "reason": "domain",
                }

            allowed = permission in self.permissions.get(domain, set())
            return {
                "allowed": allowed,
                "enforced": True,
                "mode": "enforcing",
                "reason": "policy",
            }
