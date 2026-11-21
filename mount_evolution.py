class MountEvolution:
    """Mount evolution state machine with codex, binding and speed caps."""

    SKILLS = {1: "dash", 2: "glide", 3: "soar"}
    APPEARANCE = {0: "young", 1: "adult", 2: "elder", 3: "celestial"}
    MATERIAL_PER_STAGE = {"shard": 2}

    def __init__(self, speed_cap=1.0, success_rate=0.6, max_stage=3):
        self.speed_cap = speed_cap
        self.success_rate = success_rate
        self.max_stage = max_stage

    def add_mount(self, mount_id, species, owner, bind_type="character",
                  level=1, speed=0.0, materials=None):
        raise NotImplementedError

    def preview(self, mount_id):
        raise NotImplementedError

    def evolve(self, mount_id, materials=None, roll=0.5, now=0.0):
        raise NotImplementedError

    def feed(self, mount_id, item, value=1, now=0.0):
        raise NotImplementedError

    def release(self, mount_id, now=0.0):
        raise NotImplementedError

    def bind(self, mount_id, bind_type):
        raise NotImplementedError

    def mount_up(self, mount_id):
        raise NotImplementedError

    def dismount(self, mount_id):
        raise NotImplementedError

    def speed(self, mount_id, team_aura=0.0):
        raise NotImplementedError

    def codex(self, owner):
        raise NotImplementedError

    def begin_evolution(self, mount_id):
        raise NotImplementedError

    def finish_evolution(self, token):
        raise NotImplementedError

    def state(self, mount_id):
        raise NotImplementedError
