"""Server merge cutover ledger."""

import hashlib
import json
from copy import deepcopy


class MergeCutover:
    SCOPES = ("role", "gold", "bag", "mail", "guild_warehouse")
    FROZEN_OPS = ("auction", "mail")

    def __init__(self, source=None):
        self.source = deepcopy(source or {})
        self.target = {}
        self._migrations = {}
        self._target_ops = []

    def _migration(self, migration_id):
        if migration_id not in self._migrations:
            raise ValueError("unknown migration: %s" % migration_id)
        return self._migrations[migration_id]

    def _active_frozen_characters(self):
        frozen = set()
        for migration in self._migrations.values():
            if migration["status"] == "frozen":
                frozen.update(migration["frozen"])
        return frozen

    def freeze(self, migration_id, characters=None, now=0):
        existing = self._migrations.get(migration_id)
        if existing is not None:
            return {
                "idempotent": True,
                "migration_id": migration_id,
                "frozen": list(existing["frozen"]),
            }
        characters = list(characters or [])
        self._migrations[migration_id] = {
            "status": "frozen",
            "frozen": list(characters),
            "frozen_at": now,
            "snapshot": {
                cid: deepcopy(self.source.get(cid, {})) for cid in characters
            },
            "checkpoints": {},
            "watermark": 0,
            "copied": [],
            "commit_result": None,
            "rollback_result": None,
        }
        return {
            "idempotent": False,
            "migration_id": migration_id,
            "frozen": list(characters),
        }

    def copy(self, migration_id, batch_id, character_ids, offset=0, limit=None):
        migration = self._migration(migration_id)
        if migration["status"] != "frozen":
            raise ValueError("migration %s is not active" % migration_id)
        checkpoints = migration["checkpoints"]
        start = max(checkpoints.get(batch_id, 0), offset)
        if limit is None:
            end = len(character_ids)
        else:
            end = min(len(character_ids), start + limit)
        for cid in character_ids[start:end]:
            self.target[cid] = deepcopy(
                migration["snapshot"].get(cid) or self.source.get(cid, {})
            )
            if cid not in migration["copied"]:
                migration["copied"].append(cid)
            self._target_ops.append({
                "character_id": cid,
                "op": "copy",
                "state": deepcopy(self.target[cid]),
                "origin": "migration",
                "migration_id": migration_id,
            })
        checkpoints[batch_id] = end
        return {"copied": end - start, "offset": end, "batch_id": batch_id}

    def replay(self, migration_id, events):
        migration = self._migration(migration_id)
        applied_order = []
        skipped = 0
        for event in sorted(events, key=lambda item: item["seq"]):
            seq = event["seq"]
            if seq <= migration["watermark"]:
                skipped += 1
                continue
            amount = event.get("amount", 0)
            self._apply_target(event["character_id"], event["op"], amount)
            self._target_ops.append({
                "character_id": event["character_id"],
                "op": event["op"],
                "amount": amount,
                "origin": "migration",
                "migration_id": migration_id,
            })
            migration["watermark"] = seq
            applied_order.append(seq)
        return {
            "order": applied_order,
            "applied": len(applied_order),
            "skipped": skipped,
        }

    def commit(self, migration_id):
        migration = self._migration(migration_id)
        if migration["commit_result"] is not None:
            result = deepcopy(migration["commit_result"])
            result["idempotent"] = True
            return result
        if migration["status"] != "frozen":
            raise ValueError("migration %s cannot commit" % migration_id)
        migration["status"] = "committed"
        tokens = sorted(migration["copied"])
        result = {
            "idempotent": False,
            "status": "committed",
            "tokens_reissued": tokens,
            "outbox": [{
                "type": "cutover",
                "migration_id": migration_id,
                "characters": list(tokens),
            }],
        }
        migration["commit_result"] = deepcopy(result)
        return result

    def rollback(self, migration_id):
        migration = self._migration(migration_id)
        if migration["rollback_result"] is not None:
            result = dict(migration["rollback_result"])
            result["idempotent"] = True
            return result
        compensated = 0
        for entry in reversed(self._target_ops):
            if entry.get("origin") != "migration":
                continue
            if entry.get("migration_id") != migration_id:
                continue
            self._compensate(entry)
            compensated += 1
        preserved = sum(
            1 for entry in self._target_ops if entry.get("origin") == "user"
        )
        migration["status"] = "rolled_back"
        result = {
            "status": "rolled_back",
            "compensated": compensated,
            "preserved_ops": preserved,
        }
        migration["rollback_result"] = dict(result)
        return result

    def verify(self, migration_id):
        migration = self._migration(migration_id)
        frozen = migration["frozen"]
        mismatched = []
        for scope in self.SCOPES:
            expected = self._checksum(scope, frozen, migration["snapshot"])
            actual = self._checksum(scope, frozen, self.target)
            if expected != actual:
                mismatched.append(scope)
        return {
            "ok": not mismatched,
            "scopes": list(self.SCOPES),
            "mismatched": mismatched,
        }

    @staticmethod
    def _checksum(scope, character_ids, store):
        if scope == "role":
            data = {cid: cid in store for cid in character_ids}
        else:
            data = {
                cid: store.get(cid, {}).get(scope) for cid in character_ids
            }
        blob = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _compensate(self, entry):
        state = self.target.get(entry["character_id"])
        if state is None:
            return
        if entry["op"] == "add_gold":
            state["gold"] = state.get("gold", 0) - entry.get("amount", 0)
        elif entry["op"] == "copy":
            snapshot = entry.get("state", {})
            state["gold"] = state.get("gold", 0) - snapshot.get("gold", 0)
            for scope in ("bag", "guild_warehouse"):
                items = state.get(scope, {})
                for item, count in snapshot.get(scope, {}).items():
                    remaining = items.get(item, 0) - count
                    if remaining > 0:
                        items[item] = remaining
                    else:
                        items.pop(item, None)
            mail = state.get("mail", [])
            for message in snapshot.get("mail", []):
                if message in mail:
                    mail.remove(message)

    def _apply_target(self, character_id, op, amount):
        state = self.target.setdefault(character_id, {
            "gold": 0, "bag": {}, "mail": [], "guild_warehouse": {},
        })
        if op == "add_gold":
            state["gold"] = state.get("gold", 0) + amount
        return state

    def write_source(self, character_id, op="add_gold", amount=0):
        frozen = self._active_frozen_characters()
        if character_id in frozen:
            raise ValueError(
                "character %s is frozen for migration" % character_id
            )
        if frozen and op in self.FROZEN_OPS:
            raise ValueError("op %s is frozen during migration" % op)
        state = self.source.setdefault(character_id, {
            "gold": 0, "bag": {}, "mail": [], "guild_warehouse": {},
        })
        if op == "add_gold":
            state["gold"] = state.get("gold", 0) + amount
        return state

    def write_target(self, character_id, op="add_gold", amount=0):
        state = self._apply_target(character_id, op, amount)
        self._target_ops.append({
            "character_id": character_id,
            "op": op,
            "amount": amount,
            "origin": "user",
        })
        return state

    def target_state(self, character_id):
        return self.target.get(character_id, {})
