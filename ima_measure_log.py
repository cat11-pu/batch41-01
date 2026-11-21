"""IMA measurement log parser and PCR replayer (initial skeleton)."""

import hashlib


class MeasurementLogReplayer:
    """Replay an IMA ascii measurement log into PCR extension values.

    A record line is ``[<seq>|]<pcr> <template> <algo>:<digest-hex> <path>``.
    ``seq`` is optional and defines write order for concurrent writers.  Path
    escapes are ``\\n`` and ``\\\\``.  Each record extends PCR ``pcr`` with
    ``sha1(current_pcr_bytes || template_digest_bytes)`` starting from twenty
    zero bytes.

    ``replay`` returns ``{"entries", "count", "pcr", "valid", "errors",
    "policy_gaps"}``.  ``pcr`` maps an index to ``{"expected", "actual"}``.
    """

    def __init__(
        self,
        algorithms=("sha1", "sha256", "sha384"),
        templates=("ima-ng", "ima-sig"),
        max_entries=1000,
        required_paths=(),
    ):
        self.algorithms = tuple(algorithms)
        self.templates = tuple(templates)
        self.max_entries = max_entries
        self.required_paths = tuple(required_paths)

    def replay(self, log_text, pcr_values=None):
        raw_entries = []
        errors = []

        for line_index, line in enumerate(log_text.split("\n")):
            if not line.strip():
                continue
            entry, line_errors = self._parse_line(line, line_index)
            if line_errors:
                errors.extend(line_errors)
            else:
                raw_entries.append(entry)

        if len(raw_entries) > self.max_entries:
            errors.append(
                {
                    "code": "entry_limit",
                    "message": "log contains %d entries, limit is %d"
                    % (len(raw_entries), self.max_entries),
                }
            )

        if errors:
            return {
                "entries": [],
                "count": 0,
                "pcr": {},
                "valid": False,
                "errors": errors,
                "policy_gaps": [],
            }

        ordered_entries = sorted(raw_entries, key=lambda entry: entry["sort_key"])
        entries = [
            {
                "seq": entry["seq"],
                "pcr": entry["pcr"],
                "template": entry["template"],
                "algo": entry["algo"],
                "digest": entry["digest"],
                "path": entry["path"],
            }
            for entry in ordered_entries
        ]

        pcr_state = {}
        for entry in ordered_entries:
            index = entry["pcr"]
            previous = pcr_state.get(index, b"\x00" * 20)
            pcr_state[index] = hashlib.sha1(
                previous + entry["digest_bytes"]
            ).digest()

        pcr = {}
        for index, value in pcr_state.items():
            expected = value.hex()
            pcr_item = {"expected": expected, "actual": None}
            if pcr_values and index in pcr_values:
                actual = pcr_values[index]
                pcr_item["actual"] = actual
                if actual != expected:
                    errors.append(
                        {
                            "code": "pcr_mismatch",
                            "message": "PCR %d expected %s but actual value is %s"
                            % (index, expected, actual),
                        }
                    )
            pcr[index] = pcr_item

        measured_paths = {entry["path"] for entry in entries}
        policy_gaps = [
            path for path in self.required_paths if path not in measured_paths
        ]
        for path in policy_gaps:
            errors.append(
                {
                    "code": "policy_gap",
                    "message": "required path was not measured: %s" % path,
                }
            )

        return {
            "entries": entries,
            "count": len(entries),
            "pcr": pcr,
            "valid": not errors,
            "errors": errors,
            "policy_gaps": policy_gaps,
        }

    def _parse_line(self, line, line_index):
        errors = []
        tokens = line.split(" ")
        position = 0
        seq = None

        first = tokens[position]
        if "|" in first:
            seq_text, pcr_token = first.split("|", 1)
            try:
                seq = int(seq_text)
            except ValueError:
                errors.append(
                    self._error("bad_sequence", line_index, "invalid sequence number")
                )
        else:
            pcr_token = first

        pcr_text = pcr_token
        try:
            pcr_index = int(pcr_text)
        except ValueError:
            errors.append(
                self._error(
                    "missing_pcr_index",
                    line_index,
                    "record does not start with a PCR index: %r" % pcr_text,
                )
            )
            return None, errors

        if not 0 <= pcr_index <= 23:
            errors.append(
                self._error(
                    "pcr_out_of_range",
                    line_index,
                    "PCR index %d is outside the 0..23 range" % pcr_index,
                )
            )

        position += 1
        remaining = tokens[position:]
        if len(remaining) < 3:
            errors.append(
                self._error("truncated_tail", line_index, "record is missing fields")
            )
            return None, errors

        template = remaining[0]
        if template not in self.templates:
            errors.append(
                self._error(
                    "bad_template", line_index, "unsupported template: %s" % template
                )
            )

        algo_digest = remaining[1]
        if ":" not in algo_digest:
            errors.append(
                self._error(
                    "bad_digest", line_index, "missing algorithm/digest separator"
                )
            )
            return None, errors

        algo, digest_hex = algo_digest.split(":", 1)
        if algo not in self.algorithms:
            errors.append(
                self._error(
                    "bad_algorithm", line_index, "unsupported algorithm: %s" % algo
                )
            )

        digest_bytes = None
        try:
            digest_bytes = bytes.fromhex(digest_hex)
        except ValueError:
            errors.append(
                self._error("bad_digest", line_index, "digest is not hexadecimal")
            )

        if digest_bytes is not None:
            expected_length = hashlib.new(algo).digest_size if algo in self.algorithms else None
            if expected_length is not None and len(digest_bytes) != expected_length:
                errors.append(
                    self._error(
                        "bad_digest",
                        line_index,
                        "digest length does not match %s" % algo,
                    )
                )
                digest_bytes = None

        if errors:
            return None, errors

        path = self._unescape_path(" ".join(remaining[2:]))
        entry = {
            "seq": seq,
            "pcr": pcr_index,
            "template": template,
            "algo": algo,
            "digest": digest_hex,
            "digest_bytes": digest_bytes,
            "path": path,
            "sort_key": seq if seq is not None else line_index,
        }
        return entry, errors

    @staticmethod
    def _unescape_path(path):
        result = []
        index = 0
        while index < len(path):
            char = path[index]
            if char == "\\" and index + 1 < len(path):
                following = path[index + 1]
                if following == "n":
                    result.append("\n")
                    index += 2
                    continue
                if following == "\\":
                    result.append("\\")
                    index += 2
                    continue
            result.append(char)
            index += 1
        return "".join(result)

    @staticmethod
    def _error(code, line_index, message):
        return {"code": code, "line": line_index, "message": message}
