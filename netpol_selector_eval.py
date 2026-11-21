class PolicyError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class PolicyEvaluator:
    def __init__(self):
        pass

    def add_namespace(self, name, labels):
        raise NotImplementedError

    def add_policy(self, policy):
        raise NotImplementedError

    def update_policy(self, policy):
        raise NotImplementedError

    def delete_policy(self, namespace, name):
        raise NotImplementedError

    def delete_namespace(self, namespace):
        raise NotImplementedError

    def evaluate(self, pod, peer, direction, port=None):
        raise NotImplementedError

    def version(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
