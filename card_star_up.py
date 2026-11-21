"""Card star-up service.

Contract used by the tests
--------------------------
* ``MAX_STAR`` is 6.
* Raising a card from star S to S+1 consumes exactly S materials.
* A material is valid when it has the same ``template_id`` and the same
  ``quality`` as the target card.  Materials are consumed by unique instance
  id, so repeated ids in one request count once.
* Star up keeps the card ``level`` and recomputes
  ``skill_points = 2 * star + level``.
* ``preview(card_id, material_ids)`` and the following ``star_up`` for the
  same request return the same ``attack_bonus_percent`` (one shared random
  source).
* A card already at ``MAX_STAR`` cannot be upgraded.
* ``locked`` cards keep the flag after star up and cannot be decomposed.
* ``decompose`` returns the star values of the materials actually invested
  (``refund_stars``) and ``shards = 10 * sum(refund_stars)``.
* ``star_up(..., request_id=...)`` is idempotent: repeating the same
  request_id returns the first result without consuming anything or
  incrementing the task counter.
* ``task_count(task_id)`` counts distinct successful star-up requests.
* ``revert`` restores the previous star / skill points / level and puts the
  consumed materials back into the bag.
* ``transfer`` returns ``{"server", "star", "version"}``; the version starts
  at 1 and increments on every transfer.
* Mutating one card is guarded by a per-card lock.
"""

from contextlib import contextmanager
import copy
import random
import threading


class CardStarUp:
    MAX_STAR = 6

    def __init__(self, rng=None):
        self.rng = rng if rng is not None else random.Random()
        self._cards = {}
        self._card_locks = {}
        self._registry_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._rng_lock = threading.Lock()
        self._preview_lock = threading.Lock()
        self._preview_cache = {}
        self._request_locks = {}
        self._request_results = {}
        self._task_requests = {}
        self._operation_counter = 0
        self._anonymous_counter = 0

    def add_card(
        self,
        instance_id,
        template_id,
        quality,
        star=1,
        level=1,
        locked=False,
        in_deck=False,
        deck_id=None,
    ):
        if not isinstance(star, int) or not 1 <= star <= self.MAX_STAR:
            raise ValueError("star must be between 1 and MAX_STAR")
        if not isinstance(level, int) or level < 1:
            raise ValueError("level must be a positive integer")

        with self._locks_for([instance_id]):
            with self._registry_lock:
                if instance_id in self._cards:
                    raise ValueError("card instance id already exists")
                self._cards[instance_id] = {
                    "instance_id": instance_id,
                    "template_id": template_id,
                    "quality": quality,
                    "star": star,
                    "level": level,
                    "skill_points": 2 * star + level,
                    "locked": bool(locked),
                    "in_bag": True,
                    "server": None,
                    "version": 0,
                    "in_deck": bool(in_deck or deck_id is not None),
                    "deck_id": deck_id,
                    "invested_stars": [],
                    "history": [],
                }
        return self.card_state(instance_id)

    def set_deck(self, card_id, deck_id=None, in_deck=True):
        """Assign or detach a card from a deck.

        Consuming a card that is still in an active deck is rejected by
        ``star_up`` and ``consume`` so the deck cannot silently lose a card.
        """

        with self._locks_for([card_id]):
            card = self._require_card(card_id)
            card["in_deck"] = bool(in_deck and deck_id is not None)
            card["deck_id"] = deck_id if card["in_deck"] else None
        return self.card_state(card_id)

    def assign_to_deck(self, card_id, deck_id):
        return self.set_deck(card_id, deck_id=deck_id, in_deck=True)

    def remove_from_deck(self, card_id):
        return self.set_deck(card_id, deck_id=None, in_deck=False)

    def consume(self, card_id, material_ids):
        """Consume a request's unique material instances.

        This is the low-level material operation used for auditing and
        compatibility.  It does not change the target's star; normal gameplay
        should use ``star_up``.
        """

        unique_ids = self._unique_instance_ids(material_ids)
        with self._locks_for([card_id] + list(unique_ids)):
            _, unique_ids, materials = self._validate_materials(
                card_id, unique_ids
            )
            for material in materials:
                material["in_bag"] = False
        return list(unique_ids)

    def preview(self, card_id, material_ids):
        unique_ids = self._unique_instance_ids(material_ids)
        with self._locks_for([card_id] + list(unique_ids)):
            card, unique_ids, _ = self._validate_materials(
                card_id, unique_ids
            )
            attack_bonus = self._get_attack_bonus(card, unique_ids)
            return self._build_preview(card, unique_ids, attack_bonus)

    def star_up(
        self, card_id, material_ids, task_id=None, request_id=None
    ):
        request_lock = None
        if request_id is not None:
            request_lock = self._request_lock_for(request_id)
            request_lock.acquire()

        try:
            if request_id is not None:
                existing = self._request_result(request_id)
                if existing is not None:
                    return existing

            unique_ids = self._unique_instance_ids(material_ids)
            with self._locks_for([card_id] + list(unique_ids)):
                card, unique_ids, materials = self._validate_materials(
                    card_id, unique_ids
                )
                attack_bonus = self._get_attack_bonus(card, unique_ids)
                sequence = self._next_operation_sequence()
                snapshot = {
                    "sequence": sequence,
                    "star": card["star"],
                    "level": card["level"],
                    "skill_points": card["skill_points"],
                    "locked": card["locked"],
                    "invested_count": len(card["invested_stars"]),
                    "materials": [
                        {
                            "instance_id": material["instance_id"],
                            "in_bag": material["in_bag"],
                        }
                        for material in materials
                    ],
                }

                for material in materials:
                    material["in_bag"] = False
                card["star"] += 1
                card["skill_points"] = 2 * card["star"] + card["level"]
                card["invested_stars"].extend(
                    material["star"] for material in materials
                )
                card["history"].append(snapshot)

                result = self._build_star_up_result(
                    card, unique_ids, attack_bonus
                )

            self._discard_attack_bonus(card_id, card["star"] - 1, unique_ids)
            self._record_success(request_id, task_id, result)
            return copy.deepcopy(result)
        finally:
            if request_lock is not None:
                request_lock.release()

    def decompose(self, card_id):
        with self._locks_for([card_id]):
            card = self._require_card(card_id)
            if not card["in_bag"]:
                raise ValueError("card is not in the bag")
            if card["locked"]:
                raise ValueError("locked card cannot be decomposed")

            refund_stars = list(card["invested_stars"])
            card["in_bag"] = False
            return {
                "refund_stars": refund_stars,
                "shards": 10 * sum(refund_stars),
            }

    def revert(self, card_id):
        for _ in range(3):
            with self._locks_for([card_id]):
                card = self._require_card(card_id)
                if not card["in_bag"]:
                    raise ValueError("card is not in the bag")
                if not card["history"]:
                    raise ValueError("no star-up operation to revert")
                snapshot = copy.deepcopy(card["history"][-1])

            material_ids = [
                item["instance_id"] for item in snapshot["materials"]
            ]
            with self._locks_for([card_id] + material_ids):
                card = self._require_card(card_id)
                if (
                    not card["history"]
                    or card["history"][-1]["sequence"]
                    != snapshot["sequence"]
                ):
                    continue
                if not card["in_bag"]:
                    raise ValueError("card is not in the bag")

                for item in snapshot["materials"]:
                    material = self._require_card(item["instance_id"])
                    material["in_bag"] = item["in_bag"]

                card["star"] = snapshot["star"]
                card["level"] = snapshot["level"]
                card["skill_points"] = snapshot["skill_points"]
                card["locked"] = snapshot["locked"]
                del card["invested_stars"][snapshot["invested_count"] :]
                card["history"].pop()
                return self.card_state(card_id)

        return self.card_state(card_id)

    def transfer(self, card_id, server):
        with self._locks_for([card_id]):
            card = self._require_card(card_id)
            if not card["in_bag"]:
                raise ValueError("card is not in the bag")
            card["server"] = server
            card["version"] += 1
            return {
                "server": card["server"],
                "star": card["star"],
                "version": card["version"],
            }

    def card_state(self, instance_id):
        with self._locks_for([instance_id]):
            card = self._require_card(instance_id)
            public_fields = (
                "instance_id",
                "template_id",
                "quality",
                "star",
                "level",
                "skill_points",
                "locked",
                "in_bag",
                "server",
                "version",
                "in_deck",
                "deck_id",
            )
            return {
                field: copy.deepcopy(card[field]) for field in public_fields
            }

    def task_count(self, task_id):
        with self._state_lock:
            return len(self._task_requests.get(task_id, set()))

    def _require_card(self, card_id):
        card = self._cards.get(card_id)
        if card is None:
            raise ValueError("unknown card instance")
        return card

    @staticmethod
    def _unique_instance_ids(material_ids):
        if material_ids is None:
            return []
        if isinstance(material_ids, str):
            values = [material_ids]
        else:
            try:
                values = list(material_ids)
            except TypeError as exc:
                raise ValueError("material_ids must be iterable") from exc

        unique_ids = []
        seen = set()
        for instance_id in values:
            try:
                is_seen = instance_id in seen
            except TypeError as exc:
                raise ValueError("material ids must be hashable") from exc
            if is_seen:
                continue
            seen.add(instance_id)
            unique_ids.append(instance_id)
        return unique_ids

    def _validate_materials(self, card_id, unique_ids):
        card = self._require_card(card_id)
        if not card["in_bag"]:
            raise ValueError("target card is not in the bag")
        if card["star"] >= self.MAX_STAR:
            raise ValueError("card is already at MAX_STAR")
        if len(unique_ids) != card["star"]:
            raise ValueError("material count must equal the current star")

        materials = []
        for instance_id in unique_ids:
            if instance_id == card_id:
                raise ValueError("a card cannot consume itself")
            material = self._require_card(instance_id)
            if not material["in_bag"]:
                raise ValueError("material is not in the bag")
            if material["locked"]:
                raise ValueError("locked material cannot be consumed")
            if material["in_deck"]:
                raise ValueError("material is still assigned to a deck")
            if material["template_id"] != card["template_id"]:
                raise ValueError("material template does not match")
            if material["quality"] != card["quality"]:
                raise ValueError("material quality does not match")
            materials.append(material)
        return card, unique_ids, materials

    def _build_preview(self, card, unique_ids, attack_bonus):
        next_star = card["star"] + 1
        return {
            "card_id": card["instance_id"],
            "star": next_star,
            "level": card["level"],
            "skill_points": 2 * next_star + card["level"],
            "materials": list(unique_ids),
            "consumed": list(unique_ids),
            "attack_bonus_percent": attack_bonus,
        }

    def _build_star_up_result(self, card, unique_ids, attack_bonus):
        return {
            "card_id": card["instance_id"],
            "star": card["star"],
            "level": card["level"],
            "skill_points": card["skill_points"],
            "consumed": list(unique_ids),
            "attack_bonus_percent": attack_bonus,
        }

    def _get_attack_bonus(self, card, unique_ids):
        key = (
            card["instance_id"],
            card["star"],
            tuple(sorted((repr(item) for item in unique_ids))),
        )
        with self._preview_lock:
            if key in self._preview_cache:
                return self._preview_cache[key]
            with self._rng_lock:
                attack_bonus = self._roll_attack_bonus()
            self._preview_cache[key] = attack_bonus
            return attack_bonus

    def _discard_attack_bonus(self, card_id, star, unique_ids):
        key = (
            card_id,
            star,
            tuple(sorted((repr(item) for item in unique_ids))),
        )
        with self._preview_lock:
            self._preview_cache.pop(key, None)

    def _roll_attack_bonus(self):
        rng = self.rng
        if hasattr(rng, "randint"):
            return int(rng.randint(5, 8))
        if hasattr(rng, "choice"):
            return int(rng.choice((5, 6, 7, 8)))
        if hasattr(rng, "uniform"):
            return max(1, int(round(rng.uniform(5, 8))))
        if hasattr(rng, "random"):
            return 5 + int(rng.random() * 4)
        if callable(rng):
            return 5 + int(float(rng()) * 4)
        raise ValueError("rng must provide a random number operation")

    def _request_lock_for(self, request_id):
        with self._state_lock:
            lock = self._request_locks.get(request_id)
            if lock is None:
                lock = threading.RLock()
                self._request_locks[request_id] = lock
            return lock

    def _request_result(self, request_id):
        with self._state_lock:
            result = self._request_results.get(request_id)
            return copy.deepcopy(result) if result is not None else None

    def _record_success(self, request_id, task_id, result):
        with self._state_lock:
            if request_id is None:
                self._anonymous_counter += 1
                token = ("anonymous", self._anonymous_counter)
            else:
                token = ("request", request_id)
                self._request_results[request_id] = copy.deepcopy(result)
            if task_id is not None:
                self._task_requests.setdefault(task_id, set()).add(token)

    def _next_operation_sequence(self):
        with self._state_lock:
            self._operation_counter += 1
            return self._operation_counter

    def _lock_for(self, card_id):
        with self._registry_lock:
            lock = self._card_locks.get(card_id)
            if lock is None:
                lock = threading.RLock()
                self._card_locks[card_id] = lock
            return lock

    @contextmanager
    def _locks_for(self, card_ids):
        ordered_ids = sorted(set(card_ids), key=repr)
        locks = [self._lock_for(card_id) for card_id in ordered_ids]
        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()
