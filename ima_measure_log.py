"""IMA measurement log parser and PCR replayer (initial skeleton)."""


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
        raise NotImplementedError
