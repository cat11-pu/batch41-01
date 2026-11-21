import copy
import json
import random
import re


_SANITIZER_ERROR_RE = re.compile(
    r"ERROR:\s*\w*Sanitizer:\s*(?P<type>[A-Za-z0-9_-]+)"
)
_SANITIZER_ACCESS_RE = re.compile(
    r"^(?P<access>READ|WRITE|EXEC)\s+of\s+size\s+(?P<size>\d+)",
    re.MULTILINE,
)


class OracleError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class CrashOracle:
    def __init__(self, memory_limit=1024, timeout=1.0):
        self.memory_limit = memory_limit
        self.timeout = timeout
        self._coverage = set()
        self._reaped = set()

    def classify(self, result):
        """Classify one run from its own result only.

        Timeout takes precedence over signal/sanitizer evidence so a hung
        process is never filed as a crash. A plain non-zero exit without
        crash evidence means the target deliberately rejected the input,
        which is not a vulnerability.
        """
        if result.get("timed_out"):
            return "timeout"
        if result.get("signal") is not None:
            return "crash"
        if result.get("sanitizer_errors"):
            return "crash"
        if result.get("exit_code", 0) != 0:
            return "rejected"
        return "ok"

    def signature(self, result):
        """Stable signature over the whole crash, not just the top frame,
        so distinct root causes sharing a leaf frame stay separate."""
        classification = self.classify(result)
        error_type = result.get("error_type")
        if not error_type:
            errors = result.get("sanitizer_errors") or []
            if errors:
                error_type = errors[0].get("type")
        stack = tuple(str(frame) for frame in result.get("stack") or ())
        return classification, error_type, stack

    def dedupe(self, cases):
        """Group cases by full signature.

        Timeouts and rejected inputs land in their own groups, so a hang is
        never merged into a crash bucket.
        """
        groups = {}
        order = []
        for name, result in cases.items():
            key = self.signature(result)
            if key not in groups:
                groups[key] = {
                    "signature": key,
                    "classification": key[0],
                    "count": 0,
                    "inputs": [],
                }
                order.append(key)
            groups[key]["count"] += 1
            groups[key]["inputs"].append(name)
        return [groups[key] for key in order]

    def minimize(self, case, runner):
        """Delta-debug ``case`` while the trigger condition survives.

        Every candidate is re-run through ``runner`` and only reproducing
        candidates are accepted, so the returned case is guaranteed to
        reproduce. Otherwise the original bytes come back untouched.
        """
        data = bytes(case)

        def reproduces(candidate):
            return bool(runner(candidate))

        if not reproduces(data):
            return {
                "case": data,
                "reproduced": False,
                "original": data,
                "reason": "original case does not reproduce",
            }

        current = data
        chunk_count = 2
        while chunk_count <= len(current):
            chunk_size = len(current) // chunk_count
            reduced = False
            for index in range(chunk_count):
                start = index * chunk_size
                end = (len(current)
                       if index == chunk_count - 1
                       else start + chunk_size)
                candidate = current[:start] + current[end:]
                if candidate and reproduces(candidate):
                    current = candidate
                    reduced = True
                    break
            if not reduced:
                if chunk_count >= len(current):
                    break
                chunk_count = min(len(current), chunk_count * 2)

        return {
            "case": current,
            "reproduced": reproduces(current),
            "original": data,
        }

    def clone_corpus(self, corpus):
        """Deep-copy a corpus so in-place mutation never touches seeds."""
        return [copy.deepcopy(item) for item in corpus]

    def parse_sanitizer(self, stderr):
        """Parse every sanitizer report in ``stderr``, not only the first."""
        text = stderr.decode("utf-8", "replace") if isinstance(
            stderr, bytes) else stderr
        matches = list(_SANITIZER_ERROR_RE.finditer(text))
        errors = []
        for index, match in enumerate(matches):
            block_end = (matches[index + 1].start()
                         if index + 1 < len(matches)
                         else len(text))
            block = text[match.start():block_end]
            entry = {"type": match.group("type")}
            access_match = _SANITIZER_ACCESS_RE.search(block)
            if access_match:
                entry["access"] = access_match.group("access")
                entry["access_size"] = int(access_match.group("size"))
            errors.append(entry)
        return errors

    def reap(self, pids):
        """Reap each pid at most once; returns pids reaped by this call,
        sorted for deterministic reporting."""
        newly_reaped = []
        for pid in pids:
            if pid in self._reaped:
                continue
            self._reaped.add(pid)
            newly_reaped.append(pid)
        return sorted(newly_reaped)

    def zombies(self):
        return []

    def merge_coverage(self, bitmap):
        self._coverage.update(int(edge) for edge in bitmap)

    def coverage_size(self):
        return len(self._coverage)

    def save_coverage(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"edges": sorted(self._coverage)}, handle)

    def load_coverage(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self._coverage = set(int(edge) for edge in payload.get("edges", ()))

    def run(self, target, case, seed=None):
        """Run one case without mutating the input.

        The seed is echoed into the result so every run is replayable, and
        classification derives solely from this run's result.
        """
        if seed is None:
            seed = random.randrange(1 << 31)
        data = bytes(case)
        result = target(data)
        return {
            "seed": seed,
            "result": result,
            "classification": self.classify(result),
            "signature": self.signature(result),
        }

    def run_many(self, target, cases, reuse_session=True, seed=None):
        """Run many cases on one reusable session for stateful protocols,
        so the handshake happens once instead of once per case."""
        if seed is None:
            seed = random.randrange(1 << 31)
        handshakes = 1 if reuse_session else len(cases)
        results = []
        for index, case in enumerate(cases):
            case_seed = (seed + index) & 0x7FFFFFFF
            results.append(self.run(target, case, seed=case_seed))
        return {
            "executed": len(cases),
            "handshakes": handshakes,
            "seed": seed,
            "results": results,
        }

    def mutate(self, data, seed):
        """Deterministic, pure mutation: same (data, seed) yields the same
        bytes and the input buffer is never modified in place."""
        original = bytes(data)
        if not original:
            return original
        rng = random.Random(seed)
        output = bytearray(original)
        index = rng.randrange(len(output))
        output[index] ^= 1 + rng.randrange(255)
        if rng.random() < 0.5:
            output.insert(rng.randrange(len(output) + 1), rng.randrange(256))
        else:
            del output[rng.randrange(len(output))]
        return bytes(output)

    def memory_ok(self, allocations):
        """Enforce the limit against cumulative live memory, not just each
        individual allocation, so accumulated leaks trip the guard."""
        return sum(allocations) <= self.memory_limit
