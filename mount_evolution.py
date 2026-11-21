import threading
import uuid


class MountEvolution:
    """Mount evolution state machine with codex, binding and speed caps."""

    SKILLS = {1: "dash", 2: "glide", 3: "soar"}
    APPEARANCE = {0: "young", 1: "adult", 2: "elder", 3: "celestial"}
    MATERIAL_PER_STAGE = {"shard": 2}
    BIND_TYPES = {"character", "account"}

    def __init__(self, speed_cap=1.0, success_rate=0.6, max_stage=3):
        self.speed_cap = speed_cap
        self.success_rate = success_rate
        self.max_stage = max_stage
        self._mounts = {}
        self._tokens = {}
        self._guard = threading.RLock()

    def add_mount(self, mount_id, species, owner, bind_type="character",
                  level=1, speed=0.0, materials=None):
        with self._guard:
            if mount_id in self._mounts:
                raise ValueError("mount already exists: %s" % mount_id)
            if bind_type not in self.BIND_TYPES:
                raise ValueError("unsupported bind type: %s" % bind_type)

            stock = dict(materials or {})
            for item, amount in stock.items():
                if not isinstance(amount, (int, float)) or amount < 0:
                    raise ValueError("invalid material amount for %s" % item)

            record = {
                "mount_id": mount_id,
                "species": species,
                "owner": owner,
                "bind_type": bind_type,
                "level": level,
                "speed": speed,
                "stage": 0,
                "stock": stock,
                "invested": {},
                "skills": [],
                "appearance": self._appearance_for_stage(0),
                "riding": False,
                "last_evolved_at": None,
                "last_fed_at": None,
                "_evolving": False,
                "_token": None,
                "_evolution_lock": threading.Lock(),
            }
            self._mounts[mount_id] = record
            return self._snapshot(record)

    def preview(self, mount_id):
        with self._guard:
            record = self._require(mount_id)
            stage = record["stage"]
            if stage >= self.max_stage:
                raise ValueError("mount is already at max stage")
            return {
                "mount_id": mount_id,
                "current_stage": stage,
                "next_stage": stage + 1,
                "success_rate": self.success_rate,
                "materials": dict(self.MATERIAL_PER_STAGE),
                "bind_type": record["bind_type"],
                "can_evolve": not record["riding"] and not record["_evolving"],
            }

    def evolve(self, mount_id, materials=None, roll=0.5, now=0.0):
        token = self.begin_evolution(mount_id)
        try:
            with self._guard:
                record = self._require(mount_id)
                cost = dict(self.MATERIAL_PER_STAGE)
                source = record["stock"] if materials is None else materials
                self._validate_payment(source, cost)

                success = self._roll_succeeds(roll)
                result = {
                    "mount_id": mount_id,
                    "success": success,
                    "success_rate": self.success_rate,
                    "stage": record["stage"],
                    "materials": cost,
                }

                if not success:
                    return result

                next_stage = record["stage"] + 1
                if next_stage > self.max_stage:
                    raise ValueError("mount is already at max stage")

                if materials is None:
                    remaining = dict(record["stock"])
                    for item, amount in cost.items():
                        remaining[item] -= amount
                    record["stock"] = remaining
                else:
                    for item, amount in cost.items():
                        source[item] -= amount

                record["stage"] = next_stage
                record["skills"] = self._skills_for_stage(next_stage)
                record["appearance"] = self._appearance_for_stage(next_stage)
                record["last_evolved_at"] = now

                result.update({
                    "stage": next_stage,
                    "skills": list(record["skills"]),
                    "appearance": record["appearance"],
                    "bind_type": record["bind_type"],
                    "level": record["level"],
                })
                return result
        finally:
            self.finish_evolution(token)

    def feed(self, mount_id, item, value=1, now=0.0):
        with self._guard:
            record = self._require(mount_id)
            if record["_evolving"]:
                raise ValueError("mount is evolving")
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError("feed value must be non-negative")
            record["invested"][item] = record["invested"].get(item, 0) + value
            record["last_fed_at"] = now
            return {
                "mount_id": mount_id,
                "item": item,
                "value": value,
                "invested": dict(record["invested"]),
            }

    def release(self, mount_id, now=0.0):
        with self._guard:
            record = self._require(mount_id)
            if record["_evolving"]:
                raise ValueError("mount is evolving")

            invested = dict(record["invested"])
            refund = sum(invested.values())
            owner = record["owner"]
            species = record["species"]
            del self._mounts[mount_id]

            return {
                "mount_id": mount_id,
                "owner": owner,
                "species": species,
                "refund": refund,
                "refund_items": invested,
                "codex_count": self._codex_count(owner, species),
            }

    def bind(self, mount_id, bind_type):
        with self._guard:
            record = self._require(mount_id)
            if record["_evolving"]:
                raise ValueError("mount is evolving")
            if bind_type not in self.BIND_TYPES:
                raise ValueError("unsupported bind type: %s" % bind_type)
            record["bind_type"] = bind_type
            return self._snapshot(record)

    def mount_up(self, mount_id):
        with self._guard:
            record = self._require(mount_id)
            if record["_evolving"]:
                raise ValueError("cannot ride a mount while it is evolving")
            record["riding"] = True
            return {"mount_id": mount_id, "riding": True}

    def dismount(self, mount_id):
        with self._guard:
            record = self._require(mount_id)
            record["riding"] = False
            return {"mount_id": mount_id, "riding": False}

    def speed(self, mount_id, team_aura=0.0):
        with self._guard:
            record = self._require(mount_id)
            total = float(record["speed"]) + float(team_aura)
            return min(max(total, 0.0), float(self.speed_cap))

    def codex(self, owner):
        with self._guard:
            entries = {}
            for record in self._mounts.values():
                if record["owner"] != owner:
                    continue
                species = record["species"]
                if species not in entries:
                    entries[species] = {"species": species, "count": 0}
                entries[species]["count"] += 1
            return list(entries.values())

    def begin_evolution(self, mount_id):
        with self._guard:
            record = self._require(mount_id)
            if record["_evolving"]:
                raise ValueError("mount evolution already in progress")
            if record["riding"]:
                raise ValueError("cannot evolve a mounted mount")
            if record["stage"] >= self.max_stage:
                raise ValueError("mount is already at max stage")
            if not record["_evolution_lock"].acquire(blocking=False):
                raise ValueError("mount evolution already in progress")

            token = uuid.uuid4().hex
            record["_evolving"] = True
            record["_token"] = token
            self._tokens[token] = mount_id
            return token

    def finish_evolution(self, token):
        with self._guard:
            mount_id = self._tokens.pop(token, None)
            if mount_id is None:
                raise ValueError("unknown evolution token")
            record = self._mounts.get(mount_id)
            if record is None:
                raise ValueError("mount no longer exists")
            record["_evolving"] = False
            record["_token"] = None
            record["_evolution_lock"].release()
            return {"mount_id": mount_id, "released": True}

    def state(self, mount_id):
        with self._guard:
            return self._snapshot(self._require(mount_id))

    def _require(self, mount_id):
        record = self._mounts.get(mount_id)
        if record is None:
            raise ValueError("unknown mount: %s" % mount_id)
        return record

    def _snapshot(self, record):
        return {
            "mount_id": record["mount_id"],
            "species": record["species"],
            "owner": record["owner"],
            "stage": record["stage"],
            "level": record["level"],
            "bind_type": record["bind_type"],
            "speed": record["speed"],
            "stock": dict(record["stock"]),
            "invested": dict(record["invested"]),
            "skills": list(record["skills"]),
            "appearance": record["appearance"],
            "riding": record["riding"],
            "evolving": record["_evolving"],
        }

    def _skills_for_stage(self, stage):
        return [self.SKILLS[level] for level in sorted(self.SKILLS)
                if level <= stage]

    def _appearance_for_stage(self, stage):
        if stage in self.APPEARANCE:
            return self.APPEARANCE[stage]
        available = [level for level in self.APPEARANCE if level <= stage]
        return self.APPEARANCE[max(available)] if available else None

    def _roll_succeeds(self, roll):
        rate = float(self.success_rate)
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        return float(roll) < rate

    def _validate_payment(self, source, cost):
        if not isinstance(source, dict):
            raise ValueError("materials must be a mapping")
        for item, amount in cost.items():
            if source.get(item, 0) < amount:
                raise ValueError("insufficient material: %s" % item)

    def _codex_count(self, owner, species):
        return sum(1 for record in self._mounts.values()
                   if record["owner"] == owner and record["species"] == species)
