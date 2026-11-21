"""Account shared codex collection with sets and backfill."""


class CodexCollection:
    def __init__(self, entries=None, sets=None, season="S1", state=None):
        self.entries = dict(entries or {})
        self.sets = dict(sets or {})
        self.season = season
        self.state = state if state is not None else {}
        self.state.setdefault("accounts", {})

    def _account(self, account_id):
        accounts = self.state["accounts"]
        account = accounts.get(account_id)
        if account is None:
            account = {
                "entries": {},
                "characters": {},
                "claimed_sets": set(),
                "version": 0,
                "cache": None,
            }
            accounts[account_id] = account
        return account

    def _season_allowed(self, entry_id):
        entry = self.entries[entry_id]
        return entry.get("season") is None or entry["season"] == self.season

    @staticmethod
    def _points(account, entries):
        return sum(entries[entry_id]["points"] for entry_id in account["entries"])

    def _active_sets(self, account):
        active = []
        for set_id, spec in self.sets.items():
            if all(entry_id in account["entries"] for entry_id in spec["required"]):
                active.append(set_id)
        return sorted(active)

    @staticmethod
    def _bump(account):
        account["version"] += 1
        account["cache"] = None

    def unlock(self, account_id, entry_id, character_id=None, season=None, achieved=False):
        if entry_id not in self.entries:
            raise ValueError(f"unknown codex entry: {entry_id}")

        entry = self.entries[entry_id]
        active_season = season if season is not None else self.season
        if entry.get("season") is not None and entry["season"] != active_season:
            raise ValueError(f"entry {entry_id} is locked outside season {entry['season']}")
        if entry.get("hidden") and not achieved:
            raise ValueError(f"hidden entry {entry_id} can only be unlocked when achieved")

        account = self._account(account_id)
        if entry_id in account["entries"]:
            added = False
        else:
            account["entries"][entry_id] = True
            added = True
            self._bump(account)

        if character_id is not None:
            unlocked = account["characters"].setdefault(character_id, set())
            unlocked.add(entry_id)

        return {"added": added, "points": self._points(account, self.entries)}

    def merge(self, account_id, snapshot):
        account = self._account(account_id)
        added = []
        for entry_id in snapshot.get("entries", []):
            if entry_id not in self.entries:
                continue
            if not self._season_allowed(entry_id):
                continue
            if entry_id not in account["entries"]:
                account["entries"][entry_id] = True
                added.append(entry_id)

        claimed_before = len(account["claimed_sets"])
        account["claimed_sets"].update(snapshot.get("claimed_sets", []))
        if added or len(account["claimed_sets"]) != claimed_before:
            self._bump(account)

        return {
            "added": added,
            "points": self._points(account, self.entries),
            "version": account["version"],
        }

    def grant(self, account_id, set_id):
        if set_id not in self.sets:
            raise ValueError(f"unknown codex set: {set_id}")
        account = self._account(account_id)
        if set_id in account["claimed_sets"]:
            return {"granted": False, "duplicate": True, "set": set_id}

        required = self.sets[set_id]["required"]
        if not all(entry_id in account["entries"] for entry_id in required):
            raise ValueError(f"set {set_id} is not complete")

        account["claimed_sets"].add(set_id)
        self._bump(account)
        return {
            "granted": True,
            "duplicate": False,
            "set": set_id,
            "reward": self.sets[set_id]["reward"],
        }

    def backfill(self, accounts, batch_size=100, rate_limit=None):
        result = {"processed": 0, "batches": 0, "granted": [], "skipped": []}
        account_ids = list(accounts)
        for start in range(0, len(account_ids), batch_size):
            result["batches"] += 1
            for account_id in account_ids[start:start + batch_size]:
                account = self.state["accounts"].get(account_id)
                result["processed"] += 1
                if account is None:
                    continue
                for set_id, spec in self.sets.items():
                    if set_id in account["claimed_sets"]:
                        result["skipped"].append({"account": account_id, "set": set_id})
                        continue
                    if all(entry_id in account["entries"] for entry_id in spec["required"]):
                        claim = self.grant(account_id, set_id)
                        result["granted"].append(
                            {"account": account_id, "set": set_id, "reward": claim["reward"]}
                        )
            if rate_limit is not None:
                rate_limit()
        return result

    def revoke(self, account_id, entry_id):
        account = self.state["accounts"].get(account_id)
        if account is not None and entry_id in account["entries"]:
            return {"retained": True, "entry": entry_id}
        return {"retained": False, "entry": entry_id}

    def _build_detail(self, account_id, account):
        visible = []
        for entry_id in sorted(account["entries"]):
            entry = self.entries[entry_id]
            if entry.get("hidden") and entry_id not in account["entries"]:
                continue
            visible.append(
                {
                    "id": entry_id,
                    "points": entry["points"],
                    "hidden": bool(entry.get("hidden")),
                    "season": entry.get("season"),
                }
            )
        return {
            "account": account_id,
            "entries": visible,
            "points": self._points(account, self.entries),
            "characters": {
                character_id: len(entry_ids)
                for character_id, entry_ids in sorted(account["characters"].items())
            },
            "claimed_sets": sorted(account["claimed_sets"]),
            "active_sets": self._active_sets(account),
            "version": account["version"],
        }

    def detail(self, account_id):
        account = self.state["accounts"].get(account_id)
        if account is None:
            return {
                "account": account_id,
                "entries": [],
                "points": 0,
                "characters": {},
                "claimed_sets": [],
                "active_sets": [],
                "version": 0,
            }
        if account["cache"] is None or account["cache"].get("version") != account["version"]:
            account["cache"] = self._build_detail(account_id, account)
        return account["cache"]

    def leaderboard(self):
        board = []
        for account_id, account in self.state["accounts"].items():
            info = self.detail(account_id)
            board.append({"account": account_id, "points": info["points"], "version": info["version"]})
        board.sort(key=lambda row: (-row["points"], row["account"]))
        return board
