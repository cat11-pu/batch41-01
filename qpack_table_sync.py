import contextlib
import json
import threading


_STATIC_TABLE = (
    (":authority", ""),
    (":method", "GET"),
    (":scheme", "https"),
    (":path", "/"),
)
_STATIC_LEN = len(_STATIC_TABLE)


_HUFFMAN_CODES = (
    0x1ff8, 0x7fffd8, 0xfffffe2, 0xfffffe3, 0xfffffe4, 0xfffffe5, 0xfffffe6,
    0xfffffe7, 0xfffffe8, 0xffffea, 0x3ffffffc, 0xfffffe9, 0xfffffea, 0x3ffffffd,
    0xfffffeb, 0xfffffec, 0xfffffed, 0xfffffee, 0xfffffef, 0xffffff0, 0xffffff1,
    0xffffff2, 0x3ffffffe, 0xffffff3, 0xffffff4, 0xffffff5, 0xffffff6, 0xffffff7,
    0xffffff8, 0xffffff9, 0xffffffa, 0xffffffb, 0x14, 0x3f8, 0x3f9, 0xffa, 0x1ff9,
    0x15, 0xf8, 0x7fa, 0x3fa, 0x3fb, 0xf9, 0x7fb, 0xfa, 0x16, 0x17, 0x18, 0x0,
    0x1, 0x2, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f, 0x5c, 0xfb, 0x7ffc, 0x20,
    0xffb, 0x3fc, 0x1ffa, 0x21, 0x5d, 0x5e, 0x5f, 0x60, 0x61, 0x62, 0x63, 0x64,
    0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x6b, 0x6c, 0x6d, 0x6e, 0x6f, 0x70,
    0x71, 0x72, 0xfc, 0x73, 0xfd, 0x1ffb, 0x7fff0, 0x1ffc, 0x3ffc, 0x22, 0x7ffd,
    0x3, 0x23, 0x4, 0x24, 0x5, 0x25, 0x26, 0x27, 0x6, 0x74, 0x75, 0x28, 0x29,
    0x2a, 0x7, 0x2b, 0x76, 0x2c, 0x8, 0x9, 0x2d, 0x77, 0x78, 0x79, 0x7a, 0x7b,
    0x7ffe, 0x7fc, 0x3ffd, 0x1ffd, 0xffffffc, 0xfffe6, 0x3fffd2, 0xfffe7, 0xfffe8,
    0x3fffd3, 0x3fffd4, 0x3fffd5, 0x7fffd9, 0x3fffd6, 0x7fffda, 0x7fffdb, 0x7fffdc,
    0x7fffdd, 0x7fffde, 0xffffeb, 0x7fffdf, 0xffffec, 0xffffed, 0x3fffd7, 0x7fffe0,
    0xffffee, 0x7fffe1, 0x7fffe2, 0x7fffe3, 0x7fffe4, 0x1fffdc, 0x3fffd8, 0x7fffe5,
    0x3fffd9, 0x7fffe6, 0x7fffe7, 0xffffef, 0x3fffda, 0x1fffdd, 0xfffe9, 0x3fffdb,
    0x3fffdc, 0x7fffe8, 0x7fffe9, 0x1fffde, 0x7fffea, 0x3fffdd, 0x3fffde, 0xfffff0,
    0x1fffdf, 0x3fffdf, 0x7fffeb, 0x7fffec, 0x1fffe0, 0x1fffe1, 0x3fffe0, 0x1fffe2,
    0x7fffed, 0x3fffe1, 0x7fffee, 0x7fffef, 0xfffea, 0x3fffe2, 0x3fffe3, 0x3fffe4,
    0x7ffff0, 0x3fffe5, 0x3fffe6, 0x7ffff1, 0x3ffffe0, 0x3ffffe1, 0xfffeb, 0x7fff1,
    0x3fffe7, 0x7ffff2, 0x3fffe8, 0x1ffffec, 0x3ffffe2, 0x3ffffe3, 0x3ffffe4,
    0x7ffffde, 0x7ffffdf, 0x3ffffe5, 0xfffff1, 0x1ffffed, 0x7fff2, 0x1fffe3,
    0x3ffffe6, 0x7ffffe0, 0x7ffffe1, 0x3ffffe7, 0x7ffffe2, 0xfffff2, 0x1fffe4,
    0x1fffe5, 0x3ffffe8, 0x3ffffe9, 0xffffffd, 0x7ffffe3, 0x7ffffe4, 0x7ffffe5,
    0xfffec, 0xfffff3, 0xfffed, 0x1fffe6, 0x3fffe9, 0x1fffe7, 0x1fffe8, 0x7ffff3,
    0x3fffea, 0x3fffeb, 0x1ffffee, 0x1ffffef, 0xfffff4, 0xfffff5, 0x3ffffea,
    0x7ffff4, 0x3ffffeb, 0x7ffffe6, 0x3ffffec, 0x3ffffed, 0x7ffffe7, 0x7ffffe8,
    0x7ffffe9, 0x7ffffea, 0x7ffffeb, 0xffffffe, 0x7ffffec, 0x7ffffed, 0x7ffffee,
    0x7ffffef, 0x7fffff0, 0x3ffffee, 0x3fffffff,
)

_HUFFMAN_LENGTHS = (
    13, 23, 28, 28, 28, 28, 28, 28, 28, 24, 30, 28, 28, 30, 28, 28, 28, 28, 28,
    28, 28, 28, 30, 28, 28, 28, 28, 28, 28, 28, 28, 28, 6, 10, 10, 12, 13, 6,
    8, 11, 10, 10, 8, 11, 8, 6, 6, 6, 5, 5, 5, 6, 6, 6, 6, 6, 6, 6, 7, 8, 15,
    6, 12, 10, 13, 6, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
    7, 7, 7, 8, 7, 8, 13, 19, 13, 14, 6, 15, 5, 6, 5, 6, 5, 6, 6, 6, 5, 7, 7,
    6, 6, 6, 5, 6, 7, 6, 5, 5, 6, 7, 7, 7, 7, 7, 15, 11, 14, 13, 28, 20, 22,
    20, 20, 22, 22, 22, 23, 22, 23, 23, 23, 23, 23, 24, 23, 24, 24, 22, 23, 24,
    23, 23, 23, 23, 21, 22, 23, 22, 23, 23, 24, 22, 21, 20, 22, 22, 23, 23, 21,
    23, 22, 22, 24, 21, 22, 23, 23, 21, 21, 22, 21, 23, 22, 23, 23, 20, 22, 22,
    22, 23, 22, 22, 23, 26, 26, 20, 19, 22, 23, 22, 25, 26, 26, 26, 27, 27, 26,
    24, 25, 19, 21, 26, 27, 27, 26, 27, 24, 21, 21, 26, 26, 28, 27, 27, 27, 20,
    24, 20, 21, 22, 21, 21, 23, 22, 22, 25, 25, 24, 24, 26, 23, 26, 27, 26, 26,
    27, 27, 27, 27, 27, 28, 27, 27, 27, 27, 27, 26, 30,
)


def _build_huffman_trie():
    children = [{}]
    symbols = [None]
    for symbol, (code, length) in enumerate(zip(_HUFFMAN_CODES, _HUFFMAN_LENGTHS)):
        node = 0
        for shift in range(length - 1, -1, -1):
            bit = (code >> shift) & 1
            next_node = children[node].get(bit)
            if next_node is None:
                next_node = len(children)
                children[node][bit] = next_node
                children.append({})
                symbols.append(None)
            node = next_node
        symbols[node] = symbol
    return children, symbols


_HUFFMAN_TRIE, _HUFFMAN_SYMBOLS = _build_huffman_trie()


def _entry_size(name, value):
    return len(name.encode("utf-8")) + len(value.encode("utf-8")) + 32


def _require_non_negative_int(value, label):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("%s must be a non-negative integer" % label)
    return value


class HeaderTableSync:
    """QPACK encoder/decoder dynamic table synchronisation."""

    def __init__(self, capacity=4096, max_blocked=2, max_header_list=1024, worker_id=0):
        self.capacity = _require_non_negative_int(capacity, "capacity")
        self.max_blocked = _require_non_negative_int(max_blocked, "max_blocked")
        self.max_header_list = _require_non_negative_int(
            max_header_list, "max_header_list"
        )
        self.worker_id = worker_id

        self.capacity_history = []
        self.blocked = set()

        self._entries = {}
        self._order = []
        self._references = {}
        self._stream_refs = {}
        self._stream_insertions = {}
        self._known_streams = set()
        self._size = 0
        self._next_abs = _STATIC_LEN
        self._goaway_last = None
        self._lock = threading.RLock()

    def dynamic_count(self):
        with self._lock:
            return len(self._order)

    def size(self):
        with self._lock:
            return self._size

    def _find_abs(self, index):
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise KeyError(index)
        if index < _STATIC_LEN or index not in self._entries:
            raise KeyError(index)
        return index

    def insert(self, name, value, stream_id=None):
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("header name and value must be strings")
        if stream_id is not None:
            _require_non_negative_int(stream_id, "stream_id")

        with self._lock:
            self._check_insert_allowed(stream_id)

            if self._order:
                newest = self._order[-1]
                entry = self._entries[newest]
                if entry[0] == name and entry[1] == value:
                    return newest

            cost = _entry_size(name, value)
            self._evict_locked(cost)

            absolute_index = self._next_abs
            self._next_abs += 1
            self._entries[absolute_index] = (name, value, cost, stream_id)
            self._order.append(absolute_index)
            self._size += cost
            if stream_id is not None:
                self._stream_insertions.setdefault(stream_id, []).append(
                    absolute_index
                )
            return absolute_index

    def lookup(self, index, relative=False):
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise KeyError(index)

        with self._lock:
            if relative:
                if index >= len(self._order):
                    raise KeyError(index)
                absolute_index = self._order[-1 - index]
                entry = self._entries[absolute_index]
                return (entry[0], entry[1])

            if index < _STATIC_LEN:
                return _STATIC_TABLE[index]
            entry = self._entries.get(index)
            if entry is None:
                raise KeyError(index)
            return (entry[0], entry[1])

    def reference(self, index, stream_id):
        _require_non_negative_int(stream_id, "stream_id")
        with self._lock:
            self._find_abs(index)
            refs = self._references.setdefault(index, set())
            refs.add(stream_id)
            self._stream_refs.setdefault(stream_id, set()).add(index)
            return len(refs)

    def release(self, index, stream_id):
        _require_non_negative_int(stream_id, "stream_id")
        with self._lock:
            if index not in self._entries:
                raise KeyError(index)
            refs = self._references.get(index)
            if not refs or stream_id not in refs:
                return False
            refs.discard(stream_id)
            if not refs:
                self._references.pop(index, None)
            stream_refs = self._stream_refs.get(stream_id)
            if stream_refs is not None:
                stream_refs.discard(index)
                if not stream_refs:
                    self._stream_refs.pop(stream_id, None)
            return True

    def _evict(self):
        with self._lock:
            return self._evict_locked(0)

    def _evict_locked(self, extra_size=0):
        if extra_size < 0:
            raise ValueError("extra_size must be non-negative")
        if self._size + extra_size <= self.capacity:
            return []

        candidates = []
        remaining = self._size
        for absolute_index in self._order:
            if remaining + extra_size <= self.capacity:
                break
            if self._references.get(absolute_index):
                continue
            candidates.append(absolute_index)
            remaining -= self._entries[absolute_index][2]

        if remaining + extra_size > self.capacity:
            raise RuntimeError(
                "dynamic table capacity exceeded by referenced entries"
            )

        for absolute_index in candidates:
            self._remove_entry_locked(absolute_index)
        return candidates

    def _remove_entry_locked(self, absolute_index):
        entry = self._entries.pop(absolute_index)
        self._order.remove(absolute_index)
        self._size -= entry[2]

        refs = self._references.pop(absolute_index, set())
        for stream_id in refs:
            stream_refs = self._stream_refs.get(stream_id)
            if stream_refs is not None:
                stream_refs.discard(absolute_index)
                if not stream_refs:
                    self._stream_refs.pop(stream_id, None)

        stream_id = entry[3]
        if stream_id is not None:
            insertions = self._stream_insertions.get(stream_id)
            if insertions is not None:
                try:
                    insertions.remove(absolute_index)
                except ValueError:
                    pass
                if not insertions:
                    self._stream_insertions.pop(stream_id, None)

    def reset_stream(self, stream_id):
        _require_non_negative_int(stream_id, "stream_id")
        with self._lock:
            for absolute_index in list(self._stream_refs.get(stream_id, ())):
                refs = self._references.get(absolute_index)
                if refs is not None:
                    refs.discard(stream_id)
                    if not refs:
                        self._references.pop(absolute_index, None)
            self._stream_refs.pop(stream_id, None)

            removed = 0
            for absolute_index in list(self._order):
                entry = self._entries.get(absolute_index)
                if entry is not None and entry[3] == stream_id:
                    self._remove_entry_locked(absolute_index)
                    removed += 1

            self._stream_insertions.pop(stream_id, None)
            self._known_streams.discard(stream_id)
            self.blocked.discard(stream_id)
            return removed

    def begin_blocked(self, stream_id):
        _require_non_negative_int(stream_id, "stream_id")
        with self._lock:
            self._check_stream_allowed(stream_id)
            if stream_id in self.blocked:
                return False
            if len(self.blocked) >= self.max_blocked:
                raise RuntimeError("too many blocked streams")
            self.blocked.add(stream_id)
            return True

    def end_blocked(self, stream_id):
        _require_non_negative_int(stream_id, "stream_id")
        with self._lock:
            if stream_id not in self.blocked:
                return False
            self.blocked.remove(stream_id)
            return True

    def apply_capacity_updates(self, values):
        try:
            updates = list(values)
        except TypeError as exc:
            raise ValueError("capacity updates must be iterable") from exc

        for value in updates:
            _require_non_negative_int(value, "capacity")

        with self._lock:
            snapshot = self._snapshot_locked()
            try:
                for value in updates:
                    self.capacity = value
                    self.capacity_history.append(value)
                    self._evict_locked(0)
            except Exception:
                self._restore_locked(snapshot)
                raise
            return list(self.capacity_history)

    def begin_stream(self, stream_id):
        _require_non_negative_int(stream_id, "stream_id")
        with self._lock:
            self._check_stream_allowed(stream_id)
            self._known_streams.add(stream_id)
            return True

    def goaway(self, last_stream_id):
        _require_non_negative_int(last_stream_id, "last_stream_id")
        with self._lock:
            if self._goaway_last is None:
                self._goaway_last = last_stream_id
            else:
                self._goaway_last = min(self._goaway_last, last_stream_id)
            return self._goaway_last

    def _check_stream_allowed(self, stream_id):
        if self._goaway_last is not None and stream_id > self._goaway_last:
            raise RuntimeError("stream exceeds GOAWAY last_stream_id")

    def _check_insert_allowed(self, stream_id):
        if self._goaway_last is None:
            return
        if stream_id is None:
            raise RuntimeError("new encoder insertions are not allowed after GOAWAY")
        self._check_stream_allowed(stream_id)

    def decode_header_list(self, headers):
        if headers is None:
            raise ValueError("header list is required")
        try:
            iterator = iter(headers)
        except TypeError as exc:
            raise ValueError("header list must be iterable") from exc

        decoded = []
        total = 0
        for field in iterator:
            if not isinstance(field, (tuple, list)) or len(field) != 2:
                raise ValueError("each header field must be a name/value pair")
            name, value = field
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("header names and values must be strings")
            total += _entry_size(name, value)
            if total > self.max_header_list:
                raise ValueError("header list exceeds configured limit")
            decoded.append((name, value))
        return decoded

    def huffman_encode(self, text):
        if isinstance(text, str):
            raw = text.encode("utf-8")
        elif isinstance(text, (bytes, bytearray, memoryview)):
            raw = bytes(text)
        else:
            raise TypeError("text must be str or bytes-like")

        output = bytearray()
        current = 0
        bit_count = 0
        for symbol in raw:
            code = _HUFFMAN_CODES[symbol]
            length = _HUFFMAN_LENGTHS[symbol]
            current = (current << length) | code
            bit_count += length
            while bit_count >= 8:
                bit_count -= 8
                output.append((current >> bit_count) & 0xFF)
                current &= (1 << bit_count) - 1

        if bit_count:
            padding = 8 - bit_count
            output.append(((current << padding) | ((1 << padding) - 1)) & 0xFF)
        return bytes(output)

    def huffman_decode(self, data):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ValueError("encoded data must be bytes-like")
        data = bytes(data)
        if not data:
            raise ValueError("empty Huffman payload")

        output = bytearray()
        node = 0
        pending_bits = 0
        pending_value = 0

        for octet in data:
            for shift in range(7, -1, -1):
                bit = (octet >> shift) & 1
                pending_bits += 1
                pending_value = (pending_value << 1) | bit
                next_node = _HUFFMAN_TRIE[node].get(bit)
                if next_node is None:
                    raise ValueError("invalid Huffman code")
                node = next_node
                symbol = _HUFFMAN_SYMBOLS[node]
                if symbol is None:
                    continue
                if symbol == 256:
                    raise ValueError("unexpected EOS symbol in Huffman payload")
                output.append(symbol)
                node = 0
                pending_bits = 0
                pending_value = 0

        if node != 0:
            if pending_bits == 0 or pending_bits > 7:
                raise ValueError("truncated Huffman payload")
            if pending_value != (1 << pending_bits) - 1:
                raise ValueError("invalid Huffman padding")

        try:
            return bytes(output).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Huffman payload is not valid UTF-8") from exc

    def export_state(self):
        with self._lock:
            entries = []
            for absolute_index in self._order:
                name, value, _cost, stream_id = self._entries[absolute_index]
                entries.append(
                    {
                        "index": absolute_index,
                        "name": name,
                        "value": value,
                        "stream_id": stream_id,
                    }
                )
            references = {
                str(absolute_index): sorted(stream_ids)
                for absolute_index, stream_ids in self._references.items()
            }
            return {
                "version": 1,
                "capacity": self.capacity,
                "capacity_history": list(self.capacity_history),
                "next_abs": self._next_abs,
                "entries": entries,
                "references": references,
                "blocked": sorted(self.blocked),
                "goaway_last_stream_id": self._goaway_last,
            }

    def adopt_state(self, state):
        if isinstance(state, (str, bytes, bytearray)):
            try:
                state = json.loads(state)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid serialized table state") from exc
        if not isinstance(state, dict):
            raise ValueError("table state must be a mapping")

        capacity = _require_non_negative_int(state.get("capacity", 0), "capacity")
        raw_history = state.get("capacity_history", [])
        if not isinstance(raw_history, (list, tuple)):
            raise ValueError("capacity_history must be a sequence")
        history = [
            _require_non_negative_int(value, "capacity") for value in raw_history
        ]

        raw_entries = state.get("entries", [])
        if not isinstance(raw_entries, (list, tuple)):
            raise ValueError("entries must be a sequence")

        entries = {}
        order = []
        stream_insertions = {}
        total_size = 0
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                raise ValueError("entry must be a mapping")
            absolute_index = raw_entry.get("index")
            if (
                not isinstance(absolute_index, int)
                or isinstance(absolute_index, bool)
                or absolute_index < _STATIC_LEN
            ):
                raise ValueError("invalid dynamic table index")
            if order and absolute_index <= order[-1]:
                raise ValueError("dynamic table entries must be strictly ordered")
            name = raw_entry.get("name")
            value = raw_entry.get("value")
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("entry name and value must be strings")
            stream_id = raw_entry.get("stream_id")
            if stream_id is not None:
                stream_id = _require_non_negative_int(stream_id, "stream_id")
            cost = _entry_size(name, value)
            entries[absolute_index] = (name, value, cost, stream_id)
            order.append(absolute_index)
            total_size += cost
            if stream_id is not None:
                stream_insertions.setdefault(stream_id, []).append(absolute_index)

        if total_size > capacity:
            raise ValueError("dynamic table state exceeds capacity")

        next_abs = state.get(
            "next_abs", (order[-1] + 1) if order else _STATIC_LEN
        )
        if (
            not isinstance(next_abs, int)
            or isinstance(next_abs, bool)
            or next_abs < _STATIC_LEN
        ):
            raise ValueError("invalid next_abs")
        if order and next_abs <= order[-1]:
            raise ValueError("next_abs must be greater than existing indices")

        raw_references = state.get("references", {})
        if not isinstance(raw_references, dict):
            raise ValueError("references must be a mapping")
        references = {}
        stream_refs = {}
        for raw_index, raw_streams in raw_references.items():
            try:
                absolute_index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid reference index") from exc
            if absolute_index not in entries:
                raise ValueError("reference points to a missing entry")
            if not isinstance(raw_streams, (list, tuple, set)):
                raise ValueError("reference stream list must be a sequence")
            stream_ids = {
                _require_non_negative_int(stream_id, "stream_id")
                for stream_id in raw_streams
            }
            if stream_ids:
                references[absolute_index] = stream_ids
                for stream_id in stream_ids:
                    stream_refs.setdefault(stream_id, set()).add(absolute_index)

        raw_blocked = state.get("blocked", [])
        if not isinstance(raw_blocked, (list, tuple, set)):
            raise ValueError("blocked streams must be a sequence")
        blocked = {
            _require_non_negative_int(stream_id, "stream_id")
            for stream_id in raw_blocked
        }
        if len(blocked) > self.max_blocked:
            raise ValueError("adopted state exceeds blocked stream limit")

        goaway_last = state.get("goaway_last_stream_id")
        if goaway_last is not None:
            goaway_last = _require_non_negative_int(
                goaway_last, "goaway_last_stream_id"
            )

        known_streams = set(stream_insertions)
        known_streams.update(stream_refs)
        known_streams.update(blocked)

        with self._lock:
            self.capacity = capacity
            self.capacity_history = history
            self._entries = entries
            self._order = order
            self._references = references
            self._stream_refs = stream_refs
            self._stream_insertions = stream_insertions
            self._known_streams = known_streams
            self._size = total_size
            self._next_abs = next_abs
            self._goaway_last = goaway_last
            self.blocked = blocked
            return self

    def _snapshot_locked(self):
        return {
            "capacity": self.capacity,
            "capacity_history": list(self.capacity_history),
            "size": self._size,
            "next_abs": self._next_abs,
            "entries": dict(self._entries),
            "order": list(self._order),
            "references": {
                index: set(stream_ids)
                for index, stream_ids in self._references.items()
            },
            "stream_refs": {
                stream_id: set(indices)
                for stream_id, indices in self._stream_refs.items()
            },
            "stream_insertions": {
                stream_id: list(indices)
                for stream_id, indices in self._stream_insertions.items()
            },
            "known_streams": set(self._known_streams),
            "blocked": set(self.blocked),
            "goaway_last": self._goaway_last,
        }

    def _restore_locked(self, snapshot):
        self.capacity = snapshot["capacity"]
        self.capacity_history = snapshot["capacity_history"]
        self._size = snapshot["size"]
        self._next_abs = snapshot["next_abs"]
        self._entries = snapshot["entries"]
        self._order = snapshot["order"]
        self._references = snapshot["references"]
        self._stream_refs = snapshot["stream_refs"]
        self._stream_insertions = snapshot["stream_insertions"]
        self._known_streams = snapshot["known_streams"]
        self.blocked = snapshot["blocked"]
        self._goaway_last = snapshot["goaway_last"]

    @contextlib.contextmanager
    def transaction(self):
        with self._lock:
            snapshot = self._snapshot_locked()
            try:
                yield self
            except BaseException:
                self._restore_locked(snapshot)
                raise
