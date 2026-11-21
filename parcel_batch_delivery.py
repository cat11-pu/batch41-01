class ParcelBatchDelivery:
    """Batch parcel delivery with idempotent receipts and a player ledger."""

    def __init__(self, mail_capacity=5, expire_seconds=604800, home_server="s1"):
        self.mail_capacity = mail_capacity
        self.expire_seconds = expire_seconds
        self.home_server = home_server
        self._batches = {}
        self._mails = {}
        self._player_mails = {}
        self._ledger = {}
        self._audit = []
        self._mail_seq = 0

    def plan(self, batch_id, entries, now=0.0):
        normalized = [self._normalize_entry(entry) for entry in entries]
        if batch_id in self._batches:
            batch = self._batches[batch_id]
            if self._signature(batch["entries"]) != self._signature(normalized):
                raise ValueError("batch %s already planned with different entries" % batch_id)
        else:
            batch = {
                "batch_id": batch_id,
                "entries": normalized,
                "offset": 0,
                "receipts": {},
                "revoked": False,
                "created_at": now,
            }
            self._batches[batch_id] = batch
            self._audit.append({"batch_id": batch_id, "action": "plan", "at": now,
                                "planned": len(normalized)})
        return {"batch_id": batch_id, "planned": len(normalized)}

    def dispatch(self, batch_id, now=0.0, limit=None):
        batch = self._require_batch(batch_id)
        self._ensure_active(batch)
        total = len(batch["entries"])
        forward = batch["offset"] < total
        if forward:
            indexes = range(batch["offset"], total)
            if limit is not None:
                indexes = list(indexes)[:limit]
        else:
            indexes = range(total)
        delivered = skipped = processed = 0
        for index in indexes:
            processed += 1
            if self._attempt_delivery(batch, index, now) == "delivered":
                delivered += 1
            else:
                skipped += 1
            if forward:
                batch["offset"] += 1
        result = {
            "batch_id": batch_id,
            "delivered": delivered,
            "skipped": skipped,
            "processed": processed,
            "offset": batch["offset"],
            "done": batch["offset"] >= total,
        }
        self._audit.append({"batch_id": batch_id, "action": "dispatch", "at": now,
                            "delivered": delivered, "skipped": skipped,
                            "offset": batch["offset"]})
        return result

    def retry(self, batch_id, now=0.0, limit=None):
        batch = self._require_batch(batch_id)
        self._ensure_active(batch)
        candidates = [index for index in range(len(batch["entries"]))
                      if not self._has_delivery_receipt(batch, index)]
        if limit is not None:
            candidates = candidates[:limit]
        delivered = skipped = 0
        for index in candidates:
            if self._attempt_delivery(batch, index, now) == "delivered":
                delivered += 1
            else:
                skipped += 1
        self._audit.append({"batch_id": batch_id, "action": "retry", "at": now,
                            "delivered": delivered, "skipped": skipped})
        return {"batch_id": batch_id, "delivered": delivered,
                "skipped": skipped, "processed": len(candidates),
                "offset": batch["offset"],
                "done": batch["offset"] >= len(batch["entries"])}

    def revoke(self, batch_id, actor="system", now=0.0):
        batch = self._require_batch(batch_id)
        revoked_unclaimed = 0
        clawbacks = 0
        for index, entry in enumerate(batch["entries"]):
            receipt = self._delivery_receipt(batch, index)
            if receipt is None:
                continue
            mail = self._mails.get(receipt["mail_id"])
            if mail is None:
                continue
            for item_name, qty in mail.get("claimed", {}).items():
                self._append_ledger(mail["player"], {
                    "kind": "revoke_claimed", "item": item_name,
                    "qty": -qty, "at": now, "batch_id": batch_id,
                    "mail_id": mail["mail_id"],
                })
                clawbacks += 1
            if not mail["deleted"]:
                revoked_unclaimed += 1
            mail["deleted"] = True
            mail["deleted_at"] = now
            mail["status"] = "revoked"
            self._audit.append({"batch_id": batch_id, "action": "revoke",
                                "at": now, "actor": actor,
                                "mail_id": mail["mail_id"],
                                "player": entry["player"]})
        batch["revoked"] = True
        return {"batch_id": batch_id, "revoked_unclaimed": revoked_unclaimed,
                "clawbacks": clawbacks}

    def audit(self, batch_id=None):
        if batch_id is None:
            return [dict(entry) for entry in self._audit]
        return [dict(entry) for entry in self._audit if entry["batch_id"] == batch_id]

    def ledger(self, player_id):
        return [dict(entry) for entry in self._ledger.get(player_id, [])]

    def claim(self, mail_id, items=None, now=0.0):
        mail = self._mails.get(mail_id)
        if mail is None or mail["deleted"] or mail["status"] == "revoked":
            raise ValueError("mail %s is not claimable" % mail_id)
        remaining = {entry["item"]: entry for entry in mail["items"] if entry["qty"] > 0}
        wanted = list(remaining) if items is None else list(items)
        unknown = [name for name in wanted if name not in remaining]
        if unknown:
            raise ValueError("unknown attachment items: %s" % ", ".join(unknown))
        claimed = {}
        for name in wanted:
            attachment = remaining[name]
            qty = attachment["qty"]
            claimed[name] = qty
            attachment["qty"] -= qty
            mail.setdefault("claimed", {})
            mail["claimed"][name] = mail["claimed"].get(name, 0) + qty
            self._append_ledger(mail["player"], {
                "kind": "claim", "item": name, "qty": qty, "at": now,
                "batch_id": mail["batch_id"], "mail_id": mail_id,
            })
        mail["items"] = [entry for entry in mail["items"] if entry["qty"] > 0]
        mail["status"] = "claimed" if not mail["items"] else "partial"
        self._audit.append({"batch_id": mail["batch_id"], "action": "claim",
                            "at": now, "mail_id": mail_id, "claimed": dict(claimed)})
        return {
            "claimed": claimed,
            "remaining": {entry["item"]: entry["qty"] for entry in mail["items"]},
        }

    def delete_mail(self, mail_id, now=0.0):
        mail = self._mails.get(mail_id)
        if mail is None:
            return False
        if not mail["deleted"]:
            mail["deleted"] = True
            mail["deleted_at"] = now
            self._audit.append({"batch_id": mail["batch_id"], "action": "delete",
                                "at": now, "mail_id": mail_id,
                                "player": mail["player"]})
        return True

    def inbox(self, player_id, include_deleted=False):
        mails = []
        for mail_id in self._player_mails.get(player_id, []):
            mail = self._mails[mail_id]
            if include_deleted or not mail["deleted"]:
                mails.append(self._serialize_mail(mail))
        return mails

    def reconcile(self, batch_id):
        batch = self._require_batch(batch_id)
        delivered = 0
        missing = []
        for index, entry in enumerate(batch["entries"]):
            if self._has_delivery_receipt(batch, index):
                delivered += 1
            else:
                missing.append(entry["player"])
        return {
            "batch_id": batch_id,
            "planned": len(batch["entries"]),
            "delivered": delivered,
            "missing": missing,
            "offset": batch["offset"],
            "revoked": batch["revoked"],
        }

    def _attempt_delivery(self, batch, index, now):
        entry = batch["entries"][index]
        if self._has_delivery_receipt(batch, index):
            return "skipped"
        receipts = batch["receipts"].setdefault(index, [])
        if self._active_mail_count(entry["player"]) >= self.mail_capacity:
            receipts.append({"status": "skipped", "at": now,
                             "reason": "mailbox_full"})
            self._audit.append({"batch_id": batch["batch_id"], "action": "skip",
                                "at": now, "player": entry["player"],
                                "reason": "mailbox_full"})
            return "skipped"
        self._mail_seq += 1
        mail_id = "M%d" % self._mail_seq
        mail = {
            "mail_id": mail_id,
            "batch_id": batch["batch_id"],
            "entry_index": index,
            "player": entry["player"],
            "server": entry["server"],
            "items": [{"item": name, "qty": qty, "bound": entry["bound"]}
                      for name, qty in sorted(entry["items"].items())],
            "status": "unclaimed",
            "deleted": False,
            "claimed": {},
            "created_at": now,
            "expires_at": now + self.expire_seconds,
        }
        self._mails[mail_id] = mail
        self._player_mails.setdefault(entry["player"], []).append(mail_id)
        receipts.append({"status": "delivered", "at": now, "mail_id": mail_id})
        for attachment in mail["items"]:
            name = attachment["item"]
            qty = attachment["qty"]
            self._append_ledger(entry["player"], {
                "kind": "deliver", "item": name, "qty": qty, "at": now,
                "batch_id": batch["batch_id"], "mail_id": mail_id,
                "bound": entry["bound"], "server": entry["server"],
            })
        self._audit.append({"batch_id": batch["batch_id"], "action": "deliver",
                            "at": now, "mail_id": mail_id,
                            "player": entry["player"]})
        return "delivered"

    def _active_mail_count(self, player_id):
        return sum(1 for mail_id in self._player_mails.get(player_id, [])
                   if not self._mails[mail_id]["deleted"])

    def _has_delivery_receipt(self, batch, index):
        return self._delivery_receipt(batch, index) is not None

    def _delivery_receipt(self, batch, index):
        for receipt in batch["receipts"].get(index, []):
            if receipt["status"] == "delivered":
                return receipt
        return None

    def _append_ledger(self, player_id, entry):
        self._ledger.setdefault(player_id, []).append(entry)

    def _require_batch(self, batch_id):
        if batch_id not in self._batches:
            raise KeyError("unknown batch: %s" % batch_id)
        return self._batches[batch_id]

    def _ensure_active(self, batch):
        if batch["revoked"]:
            raise ValueError("batch %s has been revoked" % batch["batch_id"])

    def _normalize_entry(self, entry):
        return {
            "player": entry["player"],
            "server": entry.get("server", self.home_server),
            "items": dict(entry["items"]),
            "bound": bool(entry.get("bound", False)),
        }

    def _signature(self, entries):
        return tuple(
            (entry["player"], entry["server"], entry["bound"],
             tuple(sorted(entry["items"].items())))
            for entry in entries
        )

    def _serialize_mail(self, mail):
        return {
            "mail_id": mail["mail_id"],
            "batch_id": mail["batch_id"],
            "player": mail["player"],
            "server": mail["server"],
            "items": [dict(attachment) for attachment in mail["items"]],
            "status": mail["status"],
            "deleted": mail["deleted"],
            "created_at": mail["created_at"],
            "expires_at": mail["expires_at"],
        }
