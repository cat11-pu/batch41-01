import copy
import threading
import time
from contextlib import contextmanager


class MatchStateRollback:
    """Coordinates ranked-match rollback across rank, rewards and progress."""

    RANK_DELTA = {"win": 25, "loss": -20}
    HIDDEN_DELTA = {"win": 25, "loss": -20}

    def __init__(self, season=2, item_values=None):
        self.season = season
        self.item_values = dict(item_values or {})
        self._lock = threading.RLock()
        self._matches = {}
        self._plans = {}
        self._match_plans = {}
        self._players = {}
        self._mails = {}
        self._audit = []
        self._maintenance = set()
        self._cache = {}

    @staticmethod
    def _copy(value):
        return copy.deepcopy(value)

    @staticmethod
    def _opposite(result):
        if result == "loss":
            return "win"
        if result == "win":
            return "loss"
        raise ValueError("unsupported match result: %s" % result)

    @staticmethod
    def _claimed_quantities(claim):
        if not claim:
            return {}
        return {item: int(quantity or 0) for item, quantity in claim.items()}

    def _invalidate(self):
        self._cache.clear()

    def _append_audit(self, action, plan, operator, **extra):
        entry = {
            "action": action,
            "operator": operator or "system",
            "match_id": plan["match_id"],
            "plan_id": plan["plan_id"],
            "at": time.time(),
        }
        entry.update(extra)
        self._audit.append(entry)

    def record_match(self, match_id, result, players, season=None,
                     quests_after=None, achievements_after=None,
                     rewards=None, now=0.0):
        with self._lock:
            if match_id in self._matches:
                return self._copy(self._matches[match_id])

            if result not in self.RANK_DELTA:
                raise ValueError("unsupported match result: %s" % result)

            match_season = self.season if season is None else season
            quests_after = quests_after or {}
            achievements_after = achievements_after or {}
            rewards = rewards or {}

            match = {
                "match_id": match_id,
                "result": result,
                "season": match_season,
                "players": {},
                "mails": {},
            }

            for player_id, player in players.items():
                rank_before = int(player.get("rank_before", 0))
                hidden_before = int(player.get("hidden_before", 0))
                rank_after = rank_before + self.RANK_DELTA[result]
                hidden_after = hidden_before + self.HIDDEN_DELTA[result]
                quests_before = self._copy(player.get("quests", {}))
                achievements_before = self._copy(player.get("achievements", []))
                claimed = self._claimed_quantities(
                    player.get("claimed_rewards", {})
                )

                state = {
                    "rank": rank_after,
                    "hidden": hidden_after,
                    "inventory": self._copy(player.get("inventory", {})),
                    "currency": player.get("currency", 0),
                    "quests": self._copy(
                        quests_after.get(player_id, quests_before)
                    ),
                    "achievements": self._copy(
                        achievements_after.get(player_id, achievements_before)
                    ),
                    "claimed_rewards": self._copy(claimed),
                }

                player_rewards = self._copy(rewards.get(player_id, {}))
                mail_ids = []
                if player_rewards:
                    mail_id = "mail:%s:%s:reward" % (match_id, player_id)
                    claimed_from_reward = {
                        item: min(
                            int(claimed.get(item, 0)),
                            int(quantity),
                        )
                        for item, quantity in player_rewards.items()
                        if claimed.get(item, 0)
                    }
                    fully_claimed = all(
                        claimed_from_reward.get(item, 0) >= int(quantity)
                        for item, quantity in player_rewards.items()
                    )
                    mail = {
                        "mail_id": mail_id,
                        "match_id": match_id,
                        "player_id": player_id,
                        "kind": "reward",
                        "items": player_rewards,
                        "claimed": claimed_from_reward,
                        "status": "claimed" if fully_claimed else "sent",
                        "created_at": now,
                    }
                    self._mails[mail_id] = mail
                    match["mails"][player_id] = [mail_id]
                    mail_ids.append(mail_id)

                match["players"][player_id] = {
                    "rank_before": rank_before,
                    "rank_after": rank_after,
                    "hidden_before": hidden_before,
                    "hidden_after": hidden_after,
                    "quests_before": quests_before,
                    "quests_after": self._copy(state["quests"]),
                    "achievements_before": achievements_before,
                    "achievements_after": self._copy(state["achievements"]),
                    "inventory_before": self._copy(state["inventory"]),
                    "currency_before": state["currency"],
                    "rewards": player_rewards,
                    "mail_ids": mail_ids,
                }
                self._players[player_id] = state

            self._matches[match_id] = match
            self._invalidate()
            return self._copy(match)

    def plan(self, match_id, operator="system"):
        with self._lock:
            if match_id in self._match_plans:
                return self._copy(
                    self._plans[self._match_plans[match_id]]
                )

            if match_id not in self._matches:
                raise ValueError("unknown match: %s" % match_id)

            match = self._matches[match_id]
            cross_season = match["season"] != self.season
            plan_id = "plan:%s:%s" % (match_id, match["season"])
            plan = {
                "plan_id": plan_id,
                "match_id": match_id,
                "season": match["season"],
                "result": match["result"],
                "effective_result": self._opposite(match["result"]),
                "mode": "compensation" if cross_season else "rollback",
                "reason": "cross_season" if cross_season else "rollback",
                "status": "planned",
                "players": self._copy(match["players"]),
                "mails": self._copy(match["mails"]),
                "compensation_mails": [],
                "created_at": time.time(),
            }
            self._plans[plan_id] = plan
            self._match_plans[match_id] = plan_id
            self._append_audit("plan", plan, operator)
            return self._copy(plan)

    def apply(self, plan_id, operator="system"):
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise ValueError("unknown plan: %s" % plan_id)
            if plan["mode"] != "rollback":
                raise ValueError("cross-season plans can only be compensated")
            if plan["status"] == "compensated":
                raise ValueError("a compensated plan cannot be applied")
            if plan["status"] == "applied":
                return {
                    "applied": False,
                    "already": True,
                    "plan_id": plan_id,
                }

            with self.maintenance(plan["match_id"]):
                backup_players = self._copy(self._players)
                backup_mails = self._copy(self._mails)
                new_players = self._copy(self._players)
                new_mails = self._copy(self._mails)

                for player_id, snapshot in plan["players"].items():
                    state = new_players[player_id]
                    state["rank"] = snapshot["rank_before"]
                    state["hidden"] = (
                        snapshot["hidden_before"]
                        + self.HIDDEN_DELTA[plan["effective_result"]]
                    )
                    state["quests"] = self._copy(snapshot["quests_before"])
                    state["achievements"] = self._copy(
                        snapshot["achievements_before"]
                    )

                    claimed = state.get("claimed_rewards", {})
                    inventory = state.get("inventory", {})
                    currency = int(state.get("currency", 0))
                    for item, quantity in snapshot["rewards"].items():
                        reward_quantity = int(quantity or 0)
                        claimed_quantity = int(claimed.get(item, 0) or 0)
                        if claimed_quantity <= 0:
                            continue
                        reward_claimed = min(
                            claimed_quantity, reward_quantity
                        )
                        available = int(inventory.get(item, 0) or 0)
                        returned = min(reward_claimed, available)
                        if returned:
                            remaining = available - returned
                            if remaining:
                                inventory[item] = remaining
                            else:
                                inventory.pop(item, None)
                        consumed = reward_claimed - returned
                        if consumed:
                            currency += consumed * int(
                                self.item_values.get(item, 0)
                            )
                        del claimed[item]

                    state["inventory"] = inventory
                    state["currency"] = currency
                    state["claimed_rewards"] = claimed

                    for mail_id in snapshot["mail_ids"]:
                        if mail_id in new_mails:
                            new_mails[mail_id]["status"] = "void"

                plan["_pre_apply_players"] = backup_players
                plan["_pre_apply_mails"] = backup_mails
                self._players = new_players
                self._mails = new_mails
                plan["status"] = "applied"
                plan["applied_at"] = time.time()
                plan["applied_by"] = operator or "system"
                self._invalidate()

            self._append_audit("apply", plan, operator)
            return {"applied": True, "plan_id": plan_id}

    def revert(self, plan_id, operator="system"):
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise ValueError("unknown plan: %s" % plan_id)
            if plan["status"] == "reverted":
                return {
                    "reverted": False,
                    "already": True,
                    "plan_id": plan_id,
                }
            if plan["status"] != "applied":
                raise ValueError("only an applied plan can be reverted")

            with self.maintenance(plan["match_id"]):
                self._players = self._copy(plan.get("_pre_apply_players", {}))
                self._mails = self._copy(plan.get("_pre_apply_mails", {}))
                plan["status"] = "reverted"
                plan["reverted_at"] = time.time()
                plan["reverted_by"] = operator or "system"
                self._invalidate()

            self._append_audit("revert", plan, operator)
            return {"reverted": True, "plan_id": plan_id}

    def compensate(self, plan_id, reason="manual", operator="system"):
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise ValueError("unknown plan: %s" % plan_id)
            if plan["status"] == "applied":
                raise ValueError("a rolled-back plan cannot be compensated")
            if plan["status"] == "compensated":
                return {
                    "already": True,
                    "plan_id": plan_id,
                    "mails": self._copy(plan["compensation_mails"]),
                }
            if plan["status"] == "reverted":
                raise ValueError("a reverted plan cannot be compensated")

            mail_ids = []
            for player_id in plan["players"]:
                mail_id = "mail:%s:%s:compensation" % (plan_id, player_id)
                if mail_id not in self._mails:
                    self._mails[mail_id] = {
                        "mail_id": mail_id,
                        "match_id": plan["match_id"],
                        "player_id": player_id,
                        "kind": "compensation",
                        "items": self._copy(
                            plan["players"][player_id]["rewards"]
                        ),
                        "claimed": {},
                        "status": "compensation",
                        "created_at": time.time(),
                        "reason": reason,
                    }
                mail_ids.append(mail_id)

            plan["compensation_mails"] = mail_ids
            plan["status"] = "compensated"
            plan["compensation_reason"] = reason
            plan["compensated_at"] = time.time()
            plan["compensated_by"] = operator or "system"
            self._invalidate()
            self._append_audit(
                "compensate", plan, operator, reason=reason
            )
            return {
                "already": False,
                "plan_id": plan_id,
                "mails": self._copy(mail_ids),
            }

    def audit(self):
        with self._lock:
            return self._copy(self._audit)

    def query(self, player_id):
        with self._lock:
            if player_id in self._cache:
                return self._copy(self._cache[player_id])
            if player_id not in self._players:
                raise ValueError("unknown player: %s" % player_id)

            state = self._players[player_id]
            mails = [
                self._copy(mail)
                for mail in self._mails.values()
                if mail.get("player_id") == player_id
            ]
            result = {
                "rank": state["rank"],
                "hidden": state["hidden"],
                "inventory": self._copy(state["inventory"]),
                "currency": state["currency"],
                "quests": self._copy(state["quests"]),
                "achievements": self._copy(state["achievements"]),
                "claimed_rewards": self._copy(state["claimed_rewards"]),
                "mails": mails,
            }
            self._cache[player_id] = self._copy(result)
            return result

    def claim_reward(self, match_id, player_id, item):
        with self._lock:
            if self._maintenance:
                raise ValueError("rewards are frozen during maintenance")
            plan_id = self._match_plans.get(match_id)
            if plan_id:
                plan = self._plans[plan_id]
                if plan["status"] == "applied":
                    raise ValueError("rewards were voided by rollback")

            if player_id not in self._players:
                raise ValueError("unknown player: %s" % player_id)

            reward_mail = None
            for mail in self._mails.values():
                if (
                    mail.get("match_id") == match_id
                    and mail.get("player_id") == player_id
                    and mail.get("kind") == "reward"
                ):
                    reward_mail = mail
                    break
            if reward_mail is None:
                raise ValueError("no reward mail for this match")
            if reward_mail["status"] == "void":
                raise ValueError("reward mail is void")

            total = int(reward_mail["items"].get(item, 0) or 0)
            already = int(reward_mail.get("claimed", {}).get(item, 0) or 0)
            remaining = total - already
            if remaining <= 0:
                raise ValueError("reward has already been claimed")

            reward_mail.setdefault("claimed", {})[item] = total
            all_claimed = all(
                int(reward_mail["claimed"].get(reward_item, 0))
                >= int(quantity)
                for reward_item, quantity in reward_mail["items"].items()
            )
            reward_mail["status"] = "claimed" if all_claimed else "sent"

            state = self._players[player_id]
            state["inventory"][item] = (
                int(state["inventory"].get(item, 0)) + remaining
            )
            state["claimed_rewards"][item] = (
                int(state["claimed_rewards"].get(item, 0)) + remaining
            )
            self._invalidate()
            return {
                "claimed": remaining,
                "match_id": match_id,
                "player_id": player_id,
                "item": item,
            }

    def login(self, player_id):
        with self._lock:
            return not bool(self._maintenance)

    @contextmanager
    def maintenance(self, match_id):
        with self._lock:
            self._maintenance.add(match_id)
            self._invalidate()
        try:
            yield
        finally:
            with self._lock:
                self._maintenance.discard(match_id)
                self._invalidate()
