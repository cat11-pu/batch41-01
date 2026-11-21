import threading


class LockstepClock:
    """Fixed-step logical clock with server-authoritative time."""

    def __init__(self, step_ms=50, max_catch_up=4, frame_space=65536):
        if step_ms <= 0:
            raise ValueError("step_ms must be positive")
        if frame_space <= 1:
            raise ValueError("frame_space must be greater than one")
        if max_catch_up < 0:
            raise ValueError("max_catch_up must not be negative")

        self.step_ms = step_ms
        self.max_catch_up = max_catch_up
        self.frame_space = frame_space
        self.frame = 0

        self._lock = threading.RLock()
        self._next_due_ms = step_ms
        self._paused = False
        self._paused_at_ms = None

        default_fps = 1000 / step_ms
        if default_fps.is_integer():
            default_fps = int(default_fps)
        self._default_fps = default_fps

        self._clients = {}
        self._pending = {}
        self._inputs = {}
        self._history = []

    def current_frame(self):
        with self._lock:
            return self.frame

    def _take_frame(self):
        frame = (self.frame + 1) % self.frame_space
        self.frame = frame
        self._history.append(frame)
        return frame

    def advance(self, server_ms, client_ms=None):
        """Advance using server time only; client time is presentation data."""
        with self._lock:
            if self._paused:
                return []

            frames = []
            while server_ms >= self._next_due_ms:
                frames.append(self._take_frame())
                self._next_due_ms += self.step_ms
            return frames

    def catch_up(self, target_frame, budget=None):
        """Move toward target without skipping frames or exceeding the budget."""
        with self._lock:
            if self._paused:
                return []

            target_frame %= self.frame_space
            if self.compare_frame(target_frame, self.frame) <= 0:
                return []

            limit = self.max_catch_up if budget is None else max(0, int(budget))
            distance = (target_frame - self.frame) % self.frame_space
            count = min(distance, limit)

            frames = [self._take_frame() for _ in range(count)]
            if count:
                self._next_due_ms += count * self.step_ms
            return frames

    def pause(self, now_ms):
        with self._lock:
            if not self._paused:
                self._paused = True
                self._paused_at_ms = now_ms
            return self.frame

    def resume(self, now_ms):
        with self._lock:
            if self._paused:
                self._paused = False
                self._paused_at_ms = None
                self._next_due_ms = now_ms + self.step_ms
            return self.frame

    def subscribe(self, client_id, spectator=False):
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                client = {
                    "spectator": bool(spectator),
                    "seq": 0,
                    "fps": self._default_fps,
                }
                self._clients[client_id] = client
                self._pending[client_id] = set()
            elif spectator:
                client["spectator"] = True
            return dict(client)

    def broadcast(self):
        """Broadcast one packet to players and spectators independently."""
        with self._lock:
            players = {}
            spectators = {}
            for client_id, client in self._clients.items():
                client["seq"] += 1
                seq = client["seq"]
                self._pending[client_id].add(seq)
                packet = {"seq": seq, "frame": self.frame}
                if client["spectator"]:
                    spectators[client_id] = packet
                else:
                    players[client_id] = packet
            return {
                "frame": self.frame,
                "players": players,
                "spectators": spectators,
            }

    def unpack(self, packets):
        """Order merged packets by sequence and reject gaps/duplicates."""
        ordered = sorted(packets, key=lambda packet: packet["seq"])
        for previous, current in zip(ordered, ordered[1:]):
            if current["seq"] == previous["seq"]:
                raise ValueError("duplicate packet sequence")
            if current["seq"] != previous["seq"] + 1:
                raise ValueError("packet sequence gap")
        return ordered

    def reconnect(self, client_id, last_frame):
        with self._lock:
            if self.compare_frame(self.frame, last_frame) <= 0:
                frames = []
            else:
                distance = (self.frame - last_frame) % self.frame_space
                frames = [
                    (last_frame + offset) % self.frame_space
                    for offset in range(1, distance + 1)
                ]

            inputs = {}
            for frame in frames:
                frame_inputs = self._inputs.get(frame)
                if frame_inputs:
                    inputs[frame] = [entry["action"] for entry in frame_inputs]
            return {"frames": frames, "inputs": inputs}

    def set_presentation_rate(self, client_id, fps):
        if fps <= 0:
            raise ValueError("fps must be positive")
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                raise KeyError(client_id)
            client["fps"] = fps
            return self.presentation_rate(client_id)

    def presentation_rate(self, client_id):
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                raise KeyError(client_id)
            fps = client["fps"]
            return {
                "fps": fps,
                "frame_ms": self.step_ms,
                "render_ms": 1000 / fps,
            }

    def record_input(self, frame, player, action):
        with self._lock:
            frame %= self.frame_space
            self._inputs.setdefault(frame, []).append(
                {"player": player, "action": action}
            )

    def replay(self):
        with self._lock:
            return [
                {
                    "frame": frame,
                    "inputs": [
                        {"player": entry["player"], "action": entry["action"]}
                        for entry in self._inputs.get(frame, [])
                    ],
                }
                for frame in self._history
            ]

    def ack(self, client_id, seq):
        with self._lock:
            pending = self._pending.get(client_id)
            if pending is None:
                return 0
            pending.difference_update(
                {pending_seq for pending_seq in pending if pending_seq <= seq}
            )
            return len(pending)

    def pending(self, client_id):
        with self._lock:
            return len(self._pending.get(client_id, ()))

    def compare_frame(self, a, b):
        a %= self.frame_space
        b %= self.frame_space
        distance = (a - b) % self.frame_space
        if distance == 0:
            return 0
        if distance < self.frame_space / 2:
            return 1
        return -1
