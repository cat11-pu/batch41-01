import math
import unicodedata


class DnsScorerError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _shannon_entropy(text):
    if not text:
        return 0.0
    counts = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = float(len(text))
    entropy = 0.0
    for count in counts.values():
        probability = count / length
        entropy -= probability * math.log2(probability)
    return entropy


def _population_stddev(values):
    if len(values) < 2:
        return 0.0
    mean = sum(values) / float(len(values))
    variance = sum((value - mean) ** 2 for value in values) / float(len(values))
    return math.sqrt(variance)


class TunnelScorer:
    def __init__(self, whitelist=(), threshold=1.5, window=60.0):
        self.whitelist = [self.normalize(name) for name in whitelist]
        self.threshold = threshold
        self.window = float(window)
        self._observed = {}
        self._events = {}

    def normalize(self, name):
        if name is None:
            raise DnsScorerError("missing_qname")
        folded = unicodedata.normalize("NFKC", str(name)).lower()
        folded = folded.rstrip(".")
        labels = []
        for label in folded.split("."):
            try:
                if any(ord(char) > 127 for char in label):
                    label = label.encode("idna").decode("ascii")
            except (UnicodeError, UnicodeDecodeError):
                pass
            labels.append(label)
        return ".".join(labels)

    def is_whitelisted(self, name):
        normalized = self.normalize(name)
        candidate = normalized[:-1] if normalized.endswith(".") else normalized
        return candidate in self.whitelist

    def observe(self, event):
        qname = event.get("qname")
        if qname is None:
            raise DnsScorerError("missing_qname")
        client = event.get("client", "c1")
        record = {
            "qname": self.normalize(qname),
            "qtype": str(event.get("qtype", "A")).upper(),
            "response_bytes": int(event.get("response_bytes", 0) or 0),
            "rcode": str(event.get("rcode", "NOERROR")).upper(),
            "rtt": float(event.get("rtt", 0.0) or 0.0),
            "now": float(event.get("now", 0.0) or 0.0),
            "stream": event.get("stream", "main"),
        }
        self._observed[client] = self._observed.get(client, 0) + 1
        self._events.setdefault(client, []).append(record)
        return record

    def ingest(self, events):
        for event in events:
            self.observe(event)
        return len(events)

    def _window_events(self, client, now):
        events = self._events.get(client, [])
        cutoff = now - self.window
        active = [record for record in events if record["now"] > cutoff]
        self._events[client] = active
        return active

    @staticmethod
    def _latest_now(now, events):
        if now is not None:
            return float(now)
        if events:
            return max(record["now"] for record in events)
        return 0.0

    def features(self, client, now=None):
        stored = list(self._events.get(client, ()))
        current = self._latest_now(now, stored)
        events = self._window_events(client, current)
        queries = len(events)
        unique_qnames = len({record["qname"] for record in events})
        nxdomain = sum(1 for record in events if record["rcode"] == "NXDOMAIN")
        txt_events = [record for record in events if record["qtype"] == "TXT"]
        txt_bytes = sum(record["response_bytes"] for record in txt_events)

        label_counts = []
        max_label_lengths = []
        entropies = []
        for record in events:
            labels = [label for label in record["qname"].rstrip(".").split(".") if label]
            label_counts.append(len(labels))
            if labels:
                longest = max(labels, key=len)
                max_label_lengths.append(len(longest))
                entropies.append(_shannon_entropy(longest))

        rtt_values = [record["rtt"] for record in events]
        rate = queries / self.window if self.window > 0 else 0.0
        return {
            "observed": self._observed.get(client, 0),
            "queries": queries,
            "unique_qnames": unique_qnames,
            "nx_ratio": nxdomain / float(queries) if queries else 0.0,
            "txt_bytes": txt_bytes,
            "txt_queries": len(txt_events),
            "entropy": sum(entropies) / len(entropies) if entropies else 0.0,
            "labels": max(label_counts) if label_counts else 0,
            "max_label_len": max(max_label_lengths) if max_label_lengths else 0,
            "rtt_jitter": _population_stddev(rtt_values),
            "rate": rate,
        }

    def score(self, client, now=None):
        stats = self.features(client, now=now)
        queries = stats["queries"]
        if queries == 0:
            return 0

        score = 0.0
        score += min(stats["txt_bytes"] / 4000.0, 1.0)
        score += 0.5 * min(stats["txt_queries"] / float(queries), 1.0)
        score += 0.25 * min(stats["max_label_len"] / 40.0, 1.0)
        score += 0.25 * min(max(stats["labels"] - 3, 0) / 5.0, 1.0)
        if queries >= 6:
            score += 1.5 * min(stats["nx_ratio"], 1.0)
        return score

    def verdict(self, client, now=None):
        if self.score(client, now=now) >= self.threshold:
            return "tunnel"
        return "benign"

    def export_state(self):
        return {
            "observed": dict(self._observed),
            "events": {
                client: [dict(record) for record in records]
                for client, records in self._events.items()
            },
        }

    def merge_state(self, state):
        for client, count in state.get("observed", {}).items():
            self._observed[client] = self._observed.get(client, 0) + count
        for client, records in state.get("events", {}).items():
            self._events.setdefault(client, []).extend(
                dict(record) for record in records
            )
        return self.export_state()
