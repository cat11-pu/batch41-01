"""AppArmor profile merge compiler."""

import copy
import functools
import json
import posixpath
import re


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
        self.variables = {}
        self._profiles = {}
        self._cache = {}
        for name, value in (variables or {}).items():
            self.set_variable(name, value)

    def set_variable(self, name, value):
        self.variables[self._variable_token(name)] = str(value)

    def add_profile(self, name, rules, source="memory"):
        if isinstance(rules, dict):
            rules = [rules]
        batches = self._profiles.setdefault(str(name), [])
        batches.append(
            {
                "source": str(source),
                "rules": copy.deepcopy(list(rules or [])),
            }
        )

    def compile(self, name):
        name = str(name)
        key = self._cache_key(name)
        cached = self._cache.get(key)
        if cached is not None:
            return copy.deepcopy(cached)

        result = self._compile(name)
        self._cache[key] = copy.deepcopy(result)
        while len(self._cache) > 64:
            self._cache.pop(next(iter(self._cache)))
        return copy.deepcopy(result)

    def path_matches(self, pattern, path):
        if not isinstance(pattern, str) or not isinstance(path, str):
            return False
        return self._path_matches(
            self._expand_variables(pattern),
            self._expand_variables(path),
        )

    def decision(self, name, path):
        result = self.compile(name)
        if not isinstance(path, str):
            return "deny"

        expanded_path = self._expand_variables(path)
        allowed = False
        for rule in result["rules"]:
            if rule.get("kind") != "file":
                continue
            if not self._path_matches(rule["path"], expanded_path):
                continue
            if rule.get("action") == "deny":
                return "deny"
            if rule.get("action") == "allow":
                allowed = True
        return "allow" if allowed else "deny"

    def _compile(self, name):
        errors = []
        warnings = []
        sources = {}
        result = {
            "name": name,
            "rules": [],
            "subprofiles": {},
            "errors": errors,
            "warnings": warnings,
            "sources": sources,
        }

        batches = self._profiles.get(name)
        if batches is None:
            errors.append(self._issue("profile_missing", "profile not found"))
            return result

        merged = self._new_merge_node()
        for batch in batches:
            self._merge_rules(merged, batch["rules"], batch["source"], errors)

        subprofile_nodes = []
        self._collect_subprofiles(merged, subprofile_nodes)
        valid_targets = set(self._profiles)
        valid_targets.update(subprofile_name for subprofile_name, _ in subprofile_nodes)

        self._compile_rules(
            merged,
            valid_targets,
            result["rules"],
            sources,
            errors,
            warnings,
        )

        for subprofile_name, node in subprofile_nodes:
            compiled_rules = result["subprofiles"].setdefault(subprofile_name, [])
            self._compile_rules(
                node,
                valid_targets,
                compiled_rules,
                sources,
                errors,
                warnings,
            )

        return result

    def _cache_key(self, name):
        payload = {
            "name": name,
            "variables": self.variables,
            "profiles": self._profiles.get(name, []),
            "profile_names": sorted(self._profiles),
        }
        return json.dumps(
            self._freeze(payload), sort_keys=True, separators=(",", ":")
        )

    @classmethod
    def _freeze(cls, value):
        if isinstance(value, dict):
            return {
                str(key): cls._freeze(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            return [cls._freeze(item) for item in value]
        if isinstance(value, set):
            return sorted((cls._freeze(item) for item in value), key=repr)
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)

    @staticmethod
    def _variable_token(name):
        name = str(name)
        if name.startswith("@{") and name.endswith("}"):
            return name
        return "@{%s}" % name

    @staticmethod
    def _new_merge_node():
        return {"rules": [], "subprofiles": {}}

    def _merge_rules(self, node, rules, source, errors):
        if not isinstance(rules, (list, tuple)):
            errors.append(
                self._issue(
                    "invalid_rules",
                    "rule collection must be a list",
                    source,
                )
            )
            return

        for rule in rules:
            if not isinstance(rule, dict):
                errors.append(
                    self._issue("unknown_rule", "rule must be a mapping", source)
                )
                continue

            if rule.get("kind") != "subprofile":
                node["rules"].append((copy.deepcopy(rule), source))
                continue

            subprofile_name = rule.get("name")
            if not isinstance(subprofile_name, str) or not subprofile_name:
                errors.append(
                    self._issue(
                        "subprofile_name_missing",
                        "subprofile has no name",
                        source,
                    )
                )
                continue

            child = node["subprofiles"].setdefault(
                subprofile_name, self._new_merge_node()
            )
            self._merge_rules(child, rule.get("rules", []), source, errors)

    def _collect_subprofiles(self, node, collected):
        for subprofile_name, child in node["subprofiles"].items():
            collected.append((subprofile_name, child))
            self._collect_subprofiles(child, collected)

    def _compile_rules(
        self,
        node,
        valid_targets,
        compiled_rules,
        sources,
        errors,
        warnings,
    ):
        for rule, source in node["rules"]:
            kind = rule.get("kind")
            if kind == "file":
                self._compile_file(
                    rule, source, compiled_rules, sources, errors
                )
            elif kind == "network":
                self._compile_network(rule, source, compiled_rules, errors, warnings)
            elif kind == "jump":
                self._compile_jump(rule, source, valid_targets, compiled_rules, errors)
            else:
                errors.append(
                    self._issue(
                        "unknown_rule",
                        "unknown rule kind: %r" % (kind,),
                        source,
                    )
                )

    def _compile_file(self, rule, source, compiled_rules, sources, errors):
        path = rule.get("path")
        if not isinstance(path, str) or not path:
            errors.append(
                self._issue("file_path_missing", "file rule has no path", source)
            )
            return

        action = str(rule.get("action", "allow")).lower()
        if action not in ("allow", "deny"):
            errors.append(
                self._issue(
                    "invalid_action",
                    "unsupported file action: %r" % (rule.get("action"),),
                    source,
                )
            )
            return

        expanded_path = self._expand_variables(path)
        compiled = copy.deepcopy(rule)
        compiled["kind"] = "file"
        compiled["action"] = action
        compiled["path"] = expanded_path
        compiled["match_file"] = True
        compiled["match_dir"] = expanded_path.endswith("/")
        compiled["source"] = source
        compiled_rules.append(compiled)
        sources[expanded_path] = source

    def _compile_network(self, rule, source, compiled_rules, errors, warnings):
        family = rule.get("family")
        if family in (None, ""):
            errors.append(
                self._issue(
                    "network_family_missing",
                    "network rule has no address family",
                    source,
                )
            )
            return

        action = str(rule.get("action", "allow")).lower()
        if action not in ("allow", "deny"):
            errors.append(
                self._issue(
                    "invalid_action",
                    "unsupported network action: %r" % (rule.get("action"),),
                    source,
                )
            )
            return

        compiled = copy.deepcopy(rule)
        compiled["kind"] = "network"
        compiled["action"] = action
        compiled["family"] = family

        protocol = rule.get("protocol")
        if protocol in (None, ""):
            protocol = "tcp"
        compiled["protocol"] = str(protocol).lower()

        port = rule.get("port")
        if port in (None, ""):
            port = 0
            warnings.append(
                self._issue(
                    "network_port_unspecified",
                    "network rule has no port; using the unrestricted port 0",
                    source,
                )
            )
        elif isinstance(port, str) and port.isdigit():
            port = int(port)
        compiled["port"] = port
        compiled["source"] = source
        compiled_rules.append(compiled)

    def _compile_jump(self, rule, source, valid_targets, compiled_rules, errors):
        target = rule.get("target")
        if not isinstance(target, str) or not target or target not in valid_targets:
            errors.append(
                self._issue(
                    "missing_jump_target",
                    "jump target does not exist: %r" % (target,),
                    source,
                )
            )
            return

        compiled = copy.deepcopy(rule)
        compiled["source"] = source
        compiled_rules.append(compiled)

    @staticmethod
    def _issue(code, message, source=None):
        issue = {"code": code, "message": message}
        if source is not None:
            issue["source"] = source
        return issue

    def _expand_variables(self, text, seen=None, depth=0):
        if not isinstance(text, str):
            return text
        if seen is None:
            seen = set()
        if depth > 32:
            return text

        result = []
        index = 0
        while index < len(text):
            if text.startswith("\\@{", index):
                end = text.find("}", index + 3)
                if end != -1:
                    result.append(text[index + 1 : end + 1])
                    index = end + 1
                    continue

            if text[index] == "\\":
                result.append(text[index])
                if index + 1 < len(text):
                    result.append(text[index + 1])
                    index += 2
                else:
                    index += 1
                continue

            if text.startswith("@{", index):
                end = text.find("}", index + 2)
                if end != -1:
                    token = text[index : end + 1]
                    value = self.variables.get(token)
                    if value is None or token in seen:
                        result.append(token)
                    else:
                        result.append(
                            self._expand_variables(
                                value, seen | {token}, depth + 1
                            )
                        )
                    index = end + 1
                    continue

            result.append(text[index])
            index += 1
        return "".join(result)

    def _path_matches(self, pattern, path):
        if not isinstance(pattern, str) or not isinstance(path, str):
            return False
        if not pattern or not path:
            return False

        trailing_slash = pattern.endswith("/")
        normalized_pattern = self._normalize_path(pattern)
        normalized_path = self._normalize_path(path)
        if normalized_path == ".." or normalized_path.startswith("../"):
            return False

        if not trailing_slash:
            return self._glob_match(normalized_pattern, normalized_path)

        if normalized_pattern == "/":
            return normalized_path.startswith("/")
        return self._glob_match(
            normalized_pattern, normalized_path
        ) or self._glob_match(normalized_pattern + "/**", normalized_path)

    @staticmethod
    def _normalize_path(path):
        normalized = posixpath.normpath(path)
        if normalized.startswith("//"):
            normalized = "/" + normalized.lstrip("/")
        return normalized

    @classmethod
    def _glob_match(cls, pattern, path):
        try:
            regex = cls._glob_regex(pattern)
        except (re.error, RecursionError):
            return False
        return regex.fullmatch(path) is not None

    @staticmethod
    @functools.lru_cache(maxsize=1024)
    def _glob_regex(pattern):
        return re.compile(ProfileMergeCompiler._glob_to_regex(pattern))

    @staticmethod
    def _glob_to_regex(pattern):
        result = []
        index = 0
        while index < len(pattern):
            char = pattern[index]

            if char == "\\":
                if index + 1 < len(pattern):
                    result.append(re.escape(pattern[index + 1]))
                    index += 2
                else:
                    result.append(re.escape("\\"))
                    index += 1
                continue

            if char == "*":
                if index + 1 < len(pattern) and pattern[index + 1] == "*":
                    if index + 2 < len(pattern) and pattern[index + 2] == "/":
                        result.append("(?:.*/)?")
                        index += 3
                    else:
                        result.append(".*")
                        index += 2
                else:
                    result.append("[^/]*")
                    index += 1
                continue

            if char == "?":
                result.append("[^/]")
                index += 1
                continue

            if char == "[":
                end = index + 1
                if end < len(pattern) and pattern[end] in ("!", "^"):
                    end += 1
                if end < len(pattern) and pattern[end] == "]":
                    end += 1
                while end < len(pattern) and pattern[end] != "]":
                    end += 1
                if end >= len(pattern):
                    result.append(re.escape("["))
                    index += 1
                    continue

                content = pattern[index + 1 : end]
                if content.startswith("!"):
                    content = "^" + content[1:]
                result.append("[" + content + "]")
                index = end + 1
                continue

            if char == "{":
                end = index + 1
                depth = 1
                parts = []
                part_start = end
                while end < len(pattern):
                    if pattern[end] == "{":
                        depth += 1
                    elif pattern[end] == "}":
                        depth -= 1
                        if depth == 0:
                            parts.append(pattern[part_start:end])
                            break
                    elif pattern[end] == "," and depth == 1:
                        parts.append(pattern[part_start:end])
                        part_start = end + 1
                    end += 1

                if depth != 0:
                    result.append(re.escape("{"))
                    index += 1
                    continue

                if len(parts) == 1:
                    result.append(re.escape(pattern[index : end + 1]))
                else:
                    alternatives = [
                        ProfileMergeCompiler._glob_to_regex(part)
                        for part in parts
                    ]
                    result.append("(?:" + "|".join(alternatives) + ")")
                index = end + 1
                continue

            result.append(re.escape(char))
            index += 1

        return "".join(result)
