"""Tutorial step gate with account and character scoped state."""


class TutorialGate:
    def __init__(self, tutorials=None, version=1, state=None):
        self.tutorials = dict(tutorials or {})
        self.version = version
        self.state = state

    def start(self, subject_id, tutorial_id, scope="character", level=1, quests=None):
        raise NotImplementedError

    def advance(self, subject_id, tutorial_id, step_id, scope="character"):
        raise NotImplementedError

    def skip(self, subject_id, tutorial_id, level, quests=None, scope="character"):
        raise NotImplementedError

    def resume(self, subject_id, tutorial_id, scope="character"):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def transfer_state(self, old_id, new_id, scope="character"):
        raise NotImplementedError
