class ScopeMatrix:
    """Guild role, permission and channel scope resolver."""

    def __init__(self):
        pass

    def create_guild(self, guild_id, leader_id, member_cap=10):
        raise NotImplementedError

    def create_role(self, guild_id, role_id, name, rank, permissions=None,
                    parent=None, channels=None):
        raise NotImplementedError

    def assign_role(self, guild_id, account_id, role_id, actor):
        raise NotImplementedError

    def delete_role(self, guild_id, role_id, actor):
        raise NotImplementedError

    def check(self, guild_id, account_id, permission, channel=None, zone="z1"):
        raise NotImplementedError

    def grant(self, guild_id, role_id, permission, actor, channel=None):
        raise NotImplementedError

    def revoke(self, guild_id, role_id, permission, actor):
        raise NotImplementedError

    def join(self, guild_id, account_id):
        raise NotImplementedError

    def kick(self, guild_id, account_id, actor):
        raise NotImplementedError

    def borrow_lock(self, guild_id, account_id, resource, now=0.0, ttl=30.0):
        raise NotImplementedError

    def dissolve(self, guild_id, actor):
        raise NotImplementedError

    def request_join(self, guild_id, account_id):
        raise NotImplementedError

    def approve(self, guild_id, request_id, actor):
        raise NotImplementedError

    def rename_role(self, guild_id, role_id, new_name, actor):
        raise NotImplementedError

    def audit(self, guild_id):
        raise NotImplementedError

    def version(self, guild_id):
        raise NotImplementedError

    def act(self, guild_id, account_id, permission, action, expected_version,
            channel=None, zone="z1"):
        raise NotImplementedError

    def transfer_leader(self, guild_id, from_account, to_account, now=0.0):
        raise NotImplementedError

    def leader(self, guild_id):
        raise NotImplementedError
