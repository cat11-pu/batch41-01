import base64
import hashlib
import hmac
import json
import os
import tempfile

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class TicketEpochKeeper:
    def __init__(self, state_path=None, grace=300, ticket_ttl=600, early_data_limit=16384):
        self.state_path = state_path
        self.grace = grace
        self.ticket_ttl = ticket_ttl
        self.early_data_limit = early_data_limit
        self.epochs = {}
        self.current_epoch = 1
        self.revoked = set()
        self.identities = {}
        self.replay = set()
        self.events = []
        self._key_counter = 0
        if state_path and os.path.exists(state_path):
            self._load()
        if self.current_epoch not in self.epochs:
            self._install_epoch(self.current_epoch, now=0)

    def _install_epoch(self, epoch, now):
        existing_ids = {meta["key_id"] for meta in self.epochs.values()}
        while True:
            key = os.urandom(32)
            self._key_counter += 1
            digest = hashlib.sha256(
                key + self._key_counter.to_bytes(8, "big")
            ).hexdigest()
            key_id = digest[:16]
            if key_id not in existing_ids:
                break
        self.epochs[epoch] = {
            "key": key,
            "key_id": key_id,
            "created_at": now,
            "rotated_at": None,
        }

    def _record(self, event, reason=None, **fields):
        entry = {"event": event, "reason": reason}
        entry.update(fields)
        self.events.append(entry)

    def _purge_epochs(self, now):
        doomed = [
            epoch
            for epoch, meta in self.epochs.items()
            if epoch != self.current_epoch
            and meta["rotated_at"] is not None
            and now > meta["rotated_at"] + self.grace
        ]
        for epoch in doomed:
            del self.epochs[epoch]

    @staticmethod
    def _canonical(payload):
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def _mac(self, key, payload):
        return hmac.new(key, self._canonical(payload), hashlib.sha256).hexdigest()

    def _epoch_for_key_id(self, key_id):
        for meta in self.epochs.values():
            if meta["key_id"] == key_id:
                return meta
        return None

    def _persist(self):
        if not self.state_path:
            return
        data = {
            "current_epoch": self.current_epoch,
            "epochs": {
                str(epoch): {
                    "key": meta["key"].hex(),
                    "key_id": meta["key_id"],
                    "created_at": meta["created_at"],
                    "rotated_at": meta["rotated_at"],
                }
                for epoch, meta in self.epochs.items()
            },
            "revoked": sorted(self.revoked),
            "identities": dict(self.identities),
            "replay": sorted(self.replay),
            "events": list(self.events),
            "key_counter": self._key_counter,
        }
        directory = os.path.dirname(os.path.abspath(self.state_path))
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle)
            os.replace(tmp_path, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _load(self):
        with open(self.state_path) as handle:
            data = json.load(handle)
        self.current_epoch = data["current_epoch"]
        self.epochs = {
            int(epoch): {
                "key": bytes.fromhex(meta["key"]),
                "key_id": meta["key_id"],
                "created_at": meta["created_at"],
                "rotated_at": meta["rotated_at"],
            }
            for epoch, meta in data["epochs"].items()
        }
        self.revoked = set(data.get("revoked", []))
        self.identities = dict(data.get("identities", {}))
        self.replay = set(data.get("replay", []))
        self.events = list(data.get("events", []))
        self._key_counter = data.get("key_counter", 0)

    def bind_identity(self, fingerprint, identity):
        self.identities[fingerprint] = identity
        self._record("bind_identity", fingerprint=fingerprint, identity=identity)
        self._persist()

    def revoke_cert(self, fingerprint):
        self.revoked.add(fingerprint)
        self._record("revoke_cert", fingerprint=fingerprint)
        self._persist()

    def rotate(self, now):
        old = self.epochs[self.current_epoch]
        old["rotated_at"] = now
        new_epoch = self.current_epoch + 1
        self.current_epoch = new_epoch
        self._install_epoch(new_epoch, now)
        self._purge_epochs(now)
        meta = self.epochs[new_epoch]
        self._record("rotate", epoch=new_epoch, key_id=meta["key_id"], now=now)
        self._persist()
        return {"epoch": new_epoch, "key_id": meta["key_id"]}

    def issue(self, fingerprint, alpn, now):
        meta = self.epochs[self.current_epoch]
        payload = {
            "epoch": self.current_epoch,
            "key_id": meta["key_id"],
            "fingerprint": fingerprint,
            "alpn": alpn,
            "issued_at": now,
            "expires_at": now + self.ticket_ttl,
            "nonce": os.urandom(8).hex(),
        }
        envelope = {"payload": payload, "mac": self._mac(meta["key"], payload)}
        token = base64.urlsafe_b64encode(
            json.dumps(envelope, separators=(",", ":")).encode()
        ).decode()
        ticket = "{}.{}".format(meta["key_id"], token)
        self._record(
            "issue",
            epoch=self.current_epoch,
            key_id=meta["key_id"],
            fingerprint=fingerprint,
            now=now,
        )
        return {
            "ticket": ticket,
            "epoch": self.current_epoch,
            "key_id": meta["key_id"],
            "expires_at": payload["expires_at"],
        }

    def resume(self, ticket, fingerprint, alpn, method="GET", early_data=b"",
               replay_key=None, now=0, chain_ok=True):
        def reject(reason):
            self._record("resume", reason=reason, fingerprint=fingerprint, now=now)
            return {
                "ok": False,
                "reject_reason": reason,
                "identity": None,
                "epoch": None,
                "key_id": None,
                "early_data_accepted": False,
            }

        if not ticket:
            return reject("empty_ticket")
        try:
            key_id, token = ticket.split(".", 1)
            raw = base64.b64decode(token.encode(), altchars=b"-_", validate=True)
            envelope = json.loads(raw.decode())
            payload = envelope["payload"]
            mac = envelope["mac"]
            epoch = payload["epoch"]
            expires_at = payload["expires_at"]
            ticket_fingerprint = payload["fingerprint"]
            ticket_alpn = payload["alpn"]
            ticket_key_id = payload["key_id"]
        except (ValueError, KeyError, TypeError):
            return reject("malformed_ticket")

        self._purge_epochs(now)
        meta = self._epoch_for_key_id(key_id)
        if meta is None:
            return reject("unknown_epoch")
        if ticket_key_id != key_id or not hmac.compare_digest(
            self._mac(meta["key"], payload), mac
        ):
            return reject("bad_mac")
        if now > expires_at:
            return reject("expired")
        if not chain_ok:
            return reject("bad_chain")
        if fingerprint in self.revoked:
            return reject("revoked")
        if ticket_fingerprint != fingerprint:
            return reject("cert_mismatch")
        if ticket_alpn != alpn:
            return reject("alpn_mismatch")

        wants_early_data = bool(early_data) or replay_key is not None
        if wants_early_data:
            if len(early_data) > self.early_data_limit:
                return reject("early_data_too_large")
            if method not in SAFE_METHODS:
                return reject("unsafe_method")
            if replay_key is not None:
                if replay_key in self.replay:
                    return reject("replay")
                self.replay.add(replay_key)

        identity = self.identities.get(fingerprint)
        self._record("resume", fingerprint=fingerprint, epoch=epoch, now=now)
        return {
            "ok": True,
            "reject_reason": None,
            "identity": identity,
            "epoch": epoch,
            "key_id": key_id,
            "early_data_accepted": wants_early_data,
        }

    def flush(self):
        self._persist()

    def ledger(self):
        return [dict(event) for event in self.events]

    def snapshot(self):
        return {
            "epoch": self.current_epoch,
            "epochs": sorted(self.epochs),
            "ledger_events": len(self.events),
            "revoked": len(self.revoked),
            "identities": len(self.identities),
            "replay_entries": len(self.replay),
        }
