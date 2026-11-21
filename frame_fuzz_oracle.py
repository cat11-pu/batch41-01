class OracleError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class CrashOracle:
    def __init__(self, memory_limit=1024, timeout=1.0):
        self.memory_limit = memory_limit
        self.timeout = timeout

    def classify(self, result):
        raise NotImplementedError

    def signature(self, result):
        raise NotImplementedError

    def dedupe(self, cases):
        raise NotImplementedError

    def minimize(self, case, runner):
        raise NotImplementedError

    def clone_corpus(self, corpus):
        raise NotImplementedError

    def parse_sanitizer(self, stderr):
        raise NotImplementedError

    def reap(self, pids):
        raise NotImplementedError

    def zombies(self):
        raise NotImplementedError

    def merge_coverage(self, bitmap):
        raise NotImplementedError

    def coverage_size(self):
        raise NotImplementedError

    def save_coverage(self, path):
        raise NotImplementedError

    def load_coverage(self, path):
        raise NotImplementedError

    def run(self, target, case, seed=None):
        raise NotImplementedError

    def run_many(self, target, cases, reuse_session=True, seed=None):
        raise NotImplementedError

    def mutate(self, data, seed):
        raise NotImplementedError

    def memory_ok(self, allocations):
        raise NotImplementedError
