class HeaderTableSync:
    """QPACK encoder/decoder dynamic table synchronisation."""

    def __init__(self, capacity=4096, max_blocked=2, max_header_list=1024, worker_id=0):
        raise NotImplementedError()

    def dynamic_count(self):
        raise NotImplementedError()

    def size(self):
        raise NotImplementedError()

    def _find_abs(self, index):
        raise NotImplementedError()

    def insert(self, name, value, stream_id=None):
        raise NotImplementedError()

    def lookup(self, index, relative=False):
        raise NotImplementedError()

    def reference(self, index, stream_id):
        raise NotImplementedError()

    def release(self, index, stream_id):
        raise NotImplementedError()

    def _evict(self):
        raise NotImplementedError()

    def reset_stream(self, stream_id):
        raise NotImplementedError()

    def begin_blocked(self, stream_id):
        raise NotImplementedError()

    def end_blocked(self, stream_id):
        raise NotImplementedError()

    def apply_capacity_updates(self, values):
        raise NotImplementedError()

    def begin_stream(self, stream_id):
        raise NotImplementedError()

    def goaway(self, last_stream_id):
        raise NotImplementedError()

    def decode_header_list(self, headers):
        raise NotImplementedError()

    def huffman_encode(self, text):
        raise NotImplementedError()

    def huffman_decode(self, data):
        raise NotImplementedError()

    def export_state(self):
        raise NotImplementedError()

    def adopt_state(self, state):
        raise NotImplementedError()

    def transaction(self):
        raise NotImplementedError()
