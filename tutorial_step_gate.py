"""Tutorial step gate with account and character scoped state."""

import copy


class TutorialGate:
    """Advance tutorials through a persisted, per-subject state machine."""

    VALID_SCOPES = ("account", "character")

    def __init__(self, tutorials=None, version=1, state=None):
        self.tutorials = copy.deepcopy(dict(tutorials or {}))
        self.version = version
        if state is None:
            self.state = {"version": version, "scopes": {}}
        else:
            self.state = copy.deepcopy(state)
            self.state.setdefault("scopes", {})

        self._migrate_all()
        self.state["version"] = self.version

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    def _tutorial(self, tutorial_id):
        try:
            return self.tutorials[tutorial_id]
        except KeyError:
            raise ValueError("unknown tutorial: %s" % tutorial_id)

    def _steps(self, tutorial_id):
        return list(self._tutorial(tutorial_id).get("steps", []))

    def _step_ids(self, tutorial_id):
        return [step["id"] for step in self._steps(tutorial_id)]

    def _step_def(self, tutorial_id, step_id):
        for step in self._steps(tutorial_id):
            if step["id"] == step_id:
                return step
        raise ValueError("unknown tutorial step: %s" % step_id)

    def _subjects(self, scope, create=False):
        scopes = self.state.setdefault("scopes", {})
        if create:
            return scopes.setdefault(scope, {})
        return scopes.get(scope, {})

    def _entries(self, scope, subject_id, create=False):
        subjects = self._subjects(scope, create=create)
        if create:
            return subjects.setdefault(subject_id, {}).setdefault("tutorials", {})
        subject = subjects.get(subject_id)
        if not subject:
            return {}
        return subject.setdefault("tutorials", {})

    def _entry(self, subject_id, tutorial_id, scope):
        if scope not in self.VALID_SCOPES:
            raise ValueError("unknown scope: %s" % scope)
        entry = self._entries(scope, subject_id).get(tutorial_id)
        if entry is None:
            raise ValueError(
                "tutorial %s is not started for %s:%s"
                % (tutorial_id, scope, subject_id)
            )
        return entry

    def _new_entry(self, subject_id, tutorial_id, scope):
        step_ids = self._step_ids(tutorial_id)
        return {
            "tutorial_id": tutorial_id,
            "scope": scope,
            "subject_id": subject_id,
            "current_step": step_ids[0] if step_ids else None,
            "completed": [],
            "unlocked": [],
            "rewards": [],
            "granted_steps": [],
            "done": not step_ids,
            "skipped": False,
            "tutorial_version": self.version,
        }

    def _view(self, entry):
        return {
            "tutorial_id": entry["tutorial_id"],
            "scope": entry["scope"],
            "subject_id": entry["subject_id"],
            "step": entry["current_step"],
            "completed": list(entry["completed"]),
            "unlocked": list(entry["unlocked"]),
            "rewards": list(entry["rewards"]),
            "done": entry["done"],
            "skipped": entry["skipped"],
            "version": entry["tutorial_version"],
        }

    @staticmethod
    def _append_once(values, value):
        if value not in values:
            values.append(value)

    def _migrate_entry(self, entry):
        tutorial_id = entry["tutorial_id"]
        if tutorial_id not in self.tutorials:
            return

        steps = self._steps(tutorial_id)
        step_ids = [step["id"] for step in steps]
        completed_set = set(entry.get("completed", []))

        if entry.get("skipped"):
            completed = list(step_ids)
            current_step = None
            done = True
        else:
            completed = [step_id for step_id in step_ids if step_id in completed_set]
            current_step = next(
                (step_id for step_id in step_ids if step_id not in completed_set),
                None,
            )
            done = current_step is None

        unlocked = list(entry.get("unlocked", []))
        if entry.get("skipped"):
            for step in steps:
                if step.get("unlock"):
                    self._append_once(unlocked, step["unlock"])
        else:
            completed_lookup = set(completed)
            for step in steps:
                if step["id"] in completed_lookup and step.get("unlock"):
                    self._append_once(unlocked, step["unlock"])

        entry["completed"] = completed
        entry["current_step"] = current_step
        entry["done"] = done
        entry["unlocked"] = unlocked
        entry["tutorial_version"] = self.version

    def _migrate_all(self):
        for subjects in self.state.get("scopes", {}).values():
            for subject in subjects.values():
                for entry in subject.get("tutorials", {}).values():
                    self._migrate_entry(entry)

    def _active_entry(self, scope, subject_id):
        for entry in self._entries(scope, subject_id).values():
            if not entry["done"]:
                return entry
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self, subject_id, tutorial_id, scope="character", level=1, quests=None):
        if scope not in self.VALID_SCOPES:
            raise ValueError("unknown scope: %s" % scope)
        self._tutorial(tutorial_id)

        existing = self._entries(scope, subject_id).get(tutorial_id)
        if existing is not None:
            self._migrate_entry(existing)
            return self._view(existing)

        active = self._active_entry(scope, subject_id)
        if active is not None:
            raise ValueError(
                "tutorial %s is already active for %s:%s"
                % (active["tutorial_id"], scope, subject_id)
            )

        entry = self._new_entry(subject_id, tutorial_id, scope)
        self._entries(scope, subject_id, create=True)[tutorial_id] = entry
        return self._view(entry)

    def advance(self, subject_id, tutorial_id, step_id, scope="character"):
        entry = self._entry(subject_id, tutorial_id, scope)
        self._migrate_entry(entry)

        if step_id in entry["completed"]:
            result = self._view(entry)
            result["reward"] = None
            result["duplicate"] = True
            return result

        if entry["done"] or step_id != entry["current_step"]:
            raise ValueError(
                "step %s is out of order; expected %s"
                % (step_id, entry["current_step"])
            )

        step = self._step_def(tutorial_id, step_id)
        entry["completed"].append(step_id)
        if step.get("unlock"):
            self._append_once(entry["unlocked"], step["unlock"])

        reward = step.get("reward")
        if reward and step_id not in entry["granted_steps"]:
            entry["granted_steps"].append(step_id)
            entry["rewards"].append(reward)

        step_ids = self._step_ids(tutorial_id)
        completed_set = set(entry["completed"])
        next_step = next(
            (candidate for candidate in step_ids if candidate not in completed_set),
            None,
        )
        entry["current_step"] = next_step
        entry["done"] = next_step is None

        result = self._view(entry)
        result["reward"] = reward
        result["duplicate"] = False
        return result

    def skip(self, subject_id, tutorial_id, level, quests=None, scope="character"):
        tutorial = self._tutorial(tutorial_id)
        entry = self._entry(subject_id, tutorial_id, scope)
        self._migrate_entry(entry)

        required_level = tutorial.get("skip_level", 0)
        if level < required_level:
            raise ValueError(
                "level %s is below skip requirement %s"
                % (level, required_level)
            )

        required_quest = tutorial.get("skip_quest")
        if required_quest and required_quest not in (quests or []):
            raise ValueError("missing skip prerequisite quest: %s" % required_quest)

        if entry["done"]:
            return self._view(entry)

        entry["completed"] = self._step_ids(tutorial_id)
        for step in self._steps(tutorial_id):
            if step.get("unlock"):
                self._append_once(entry["unlocked"], step["unlock"])
        entry["current_step"] = None
        entry["done"] = True
        entry["skipped"] = True
        return self._view(entry)

    def resume(self, subject_id, tutorial_id, scope="character"):
        entry = self._entry(subject_id, tutorial_id, scope)
        self._migrate_entry(entry)
        return self._view(entry)

    def snapshot(self):
        self.state["version"] = self.version
        return copy.deepcopy(self.state)

    def transfer_state(self, old_id, new_id, scope="character"):
        if scope == "all":
            scopes = list(self.VALID_SCOPES)
        else:
            if scope not in self.VALID_SCOPES:
                raise ValueError("unknown scope: %s" % scope)
            scopes = [scope]

        moved = False
        for item_scope in scopes:
            subjects = self._subjects(item_scope)
            if old_id not in subjects:
                continue
            if new_id in subjects:
                raise ValueError(
                    "transfer target already has tutorial state: %s:%s"
                    % (item_scope, new_id)
                )
            subject = subjects.pop(old_id)
            subjects[new_id] = subject
            for entry in subject.get("tutorials", {}).values():
                entry["subject_id"] = new_id
            moved = True

        if not moved:
            raise ValueError(
                "no tutorial state to transfer for %s:%s" % (scope, old_id)
            )
