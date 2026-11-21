import threading


class _Role:
    __slots__ = ("role_id", "name", "rank", "permissions")

    def __init__(self, role_id, name, rank):
        self.role_id = role_id
        self.name = name
        self.rank = rank
        # permission -> None (any channel) or set of allowed channels
        self.permissions = {}


class _Guild:
    def __init__(self, guild_id, leader_id, member_cap):
        self.guild_id = guild_id
        self.leader_id = leader_id
        self.member_cap = member_cap
        self.members = {leader_id}
        self.roles = {}
        self.assignments = {}
        self.version = 0
        self.cache = {}
        self.locks = {}
        self.requests = {}
        self.request_seq = 0
        self.audit_log = []
        self.dissolved = False
        self.last_transfer = None


class ScopeMatrix:
    """Guild role, permission and channel scope resolver."""

    TRANSFER_COOLDOWN = 3600.0

    def __init__(self):
        self._mu = threading.RLock()
        self._guilds = {}

    def _guild(self, guild_id):
        return self._guilds[guild_id]

    def _bump(self, guild):
        guild.version += 1
        guild.cache.clear()

    def _ensure_active(self, guild):
        if guild.dissolved:
            raise ValueError("guild is dissolved")

    def _actor_rank(self, guild, actor):
        if actor == guild.leader_id:
            return float("inf")
        ranks = [guild.roles[r].rank
                 for r in guild.assignments.get(actor, ())
                 if r in guild.roles]
        return max(ranks) if ranks else float("-inf")

    def create_guild(self, guild_id, leader_id, member_cap=10):
        with self._mu:
            self._guilds[guild_id] = _Guild(guild_id, leader_id, member_cap)

    def create_role(self, guild_id, role_id, name, rank, permissions=None,
                    parent=None, channels=None):
        with self._mu:
            guild = self._guild(guild_id)
            self._ensure_active(guild)
            role = _Role(role_id, name, rank)
            for perm in permissions or ():
                role.permissions[perm] = None
            for perm, chans in (channels or {}).items():
                role.permissions[perm] = set(chans)
            if parent is not None:
                parent_role = guild.roles.get(parent)
                if parent_role is None:
                    raise ValueError("parent role does not exist")
                for perm, chans in parent_role.permissions.items():
                    self._merge_permission(role.permissions, perm, chans)
            guild.roles[role_id] = role
            self._bump(guild)

    @staticmethod
    def _merge_permission(permissions, perm, chans):
        if perm not in permissions:
            permissions[perm] = None if chans is None else set(chans)
            return
        existing = permissions[perm]
        if existing is None or chans is None:
            permissions[perm] = None
        else:
            permissions[perm] = existing | set(chans)

    def assign_role(self, guild_id, account_id, role_id, actor):
        with self._mu:
            guild = self._guild(guild_id)
            self._ensure_active(guild)
            if account_id not in guild.members:
                raise ValueError("account is not a member")
            if role_id not in guild.roles:
                raise ValueError("role does not exist")
            guild.assignments.setdefault(account_id, set()).add(role_id)
            self._bump(guild)

    def delete_role(self, guild_id, role_id, actor):
        with self._mu:
            guild = self._guild(guild_id)
            self._ensure_active(guild)
            if role_id not in guild.roles:
                raise ValueError("role does not exist")
            del guild.roles[role_id]
            for roles in guild.assignments.values():
                roles.discard(role_id)
            guild.audit_log.append({
                "op": "delete_role", "role_id": role_id, "actor": actor,
            })
            self._bump(guild)

    def check(self, guild_id, account_id, permission, channel=None, zone="z1"):
        with self._mu:
            guild = self._guild(guild_id)
            if guild.dissolved or account_id not in guild.members:
                return False
            key = (account_id, permission, channel)
            cached = guild.cache.get(key)
            if cached is not None and cached[0] == guild.version:
                return cached[1]
            result = self._compute_check(guild, account_id, permission,
                                         channel)
            guild.cache[key] = (guild.version, result)
            return result

    def _compute_check(self, guild, account_id, permission, channel):
        if account_id == guild.leader_id:
            return True
        for role_id in guild.assignments.get(account_id, ()):
            role = guild.roles.get(role_id)
            if role is None or permission not in role.permissions:
                continue
            chans = role.permissions[permission]
            if chans is None:
                return True
            if channel is not None and channel in chans:
                return True
        return False

    def grant(self, guild_id, role_id, permission, actor, channel=None):
        with self._mu:
            guild = self._guild(guild_id)
            self._ensure_active(guild)
            role = guild.roles.get(role_id)
            if role is None:
                raise ValueError("role does not exist")
            if self._actor_rank(guild, actor) <= role.rank:
                raise ValueError(
                    "actor must outrank the target role and cannot self-grant")
            self._merge_permission(
                role.permissions, permission,
                None if channel is None else {channel})
            guild.audit_log.append({
                "op": "grant", "role_id": role_id, "permission": permission,
                "channel": channel, "actor": actor,
            })
            self._bump(guild)

    def revoke(self, guild_id, role_id, permission, actor):
        with self._mu:
            guild = self._guild(guild_id)
            self._ensure_active(guild)
            role = guild.roles.get(role_id)
            if role is None:
                raise ValueError("role does not exist")
            if self._actor_rank(guild, actor) <= role.rank:
                raise ValueError(
                    "actor must outrank the target role and cannot self-revoke")
            role.permissions.pop(permission, None)
            guild.audit_log.append({
                "op": "revoke", "role_id": role_id, "permission": permission,
                "actor": actor,
            })
            self._bump(guild)

    def join(self, guild_id, account_id):
        with self._mu:
            guild = self._guild(guild_id)
            self._ensure_active(guild)
            if account_id in guild.members:
                return
            if len(guild.members) >= guild.member_cap:
                raise ValueError("guild member cap reached")
            guild.members.add(account_id)
            self._bump(guild)

    def kick(self, guild_id, account_id, actor):
        with self._mu:
            guild = self._guild(guild_id)
            self._ensure_active(guild)
            if account_id == guild.leader_id:
                raise ValueError("cannot kick the leader")
            guild.members.discard(account_id)
            guild.assignments.pop(account_id, None)
            for resource, (owner, _expires) in list(guild.locks.items()):
                if owner == account_id:
                    del guild.locks[resource]
            self._bump(guild)

    def borrow_lock(self, guild_id, account_id, resource, now=0.0, ttl=30.0):
        with self._mu:
            guild = self._guild(guild_id)
            self._ensure_active(guild)
            if account_id not in guild.members:
                return None
            held = guild.locks.get(resource)
            if held is not None and held[0] != account_id and held[1] > now:
                return None
            token = {
                "guild_id": guild_id,
                "resource": resource,
                "owner": account_id,
                "expires_at": now + ttl,
            }
            guild.locks[resource] = (account_id, now + ttl)
            return token

    def dissolve(self, guild_id, actor):
        with self._mu:
            guild = self._guild(guild_id)
            if actor != guild.leader_id:
                raise ValueError("only the leader can dissolve the guild")
            guild.dissolved = True
            for request in guild.requests.values():
                if request["status"] == "pending":
                    request["status"] = "void"
            guild.locks.clear()
            self._bump(guild)

    def request_join(self, guild_id, account_id):
        with self._mu:
            guild = self._guild(guild_id)
            self._ensure_active(guild)
            guild.request_seq += 1
            request_id = "%s-req-%d" % (guild_id, guild.request_seq)
            request = {
                "request_id": request_id,
                "account_id": account_id,
                "status": "pending",
            }
            guild.requests[request_id] = request
            return dict(request)

    def approve(self, guild_id, request_id, actor):
        with self._mu:
            guild = self._guild(guild_id)
            self._ensure_active(guild)
            request = guild.requests.get(request_id)
            if request is None or request["status"] != "pending":
                raise ValueError("join request is not pending")
            if (request["account_id"] not in guild.members
                    and len(guild.members) >= guild.member_cap):
                raise ValueError("guild member cap reached")
            request["status"] = "approved"
            guild.members.add(request["account_id"])
            self._bump(guild)
            return dict(request)

    def rename_role(self, guild_id, role_id, new_name, actor):
        with self._mu:
            guild = self._guild(guild_id)
            self._ensure_active(guild)
            role = guild.roles.get(role_id)
            if role is None:
                raise ValueError("role does not exist")
            role.name = new_name
            guild.audit_log.append({
                "op": "rename_role", "role_id": role_id,
                "new_name": new_name, "actor": actor,
            })
            self._bump(guild)

    def audit(self, guild_id):
        with self._mu:
            return list(self._guild(guild_id).audit_log)

    def version(self, guild_id):
        with self._mu:
            return self._guild(guild_id).version

    def act(self, guild_id, account_id, permission, action, expected_version,
            channel=None, zone="z1"):
        with self._mu:
            guild = self._guild(guild_id)
            if expected_version != guild.version:
                raise ValueError("stale version: permission state changed")
            if not self.check(guild_id, account_id, permission,
                              channel=channel, zone=zone):
                raise PermissionError(permission)
            return {
                "allowed": True,
                "action": action,
                "version": guild.version,
            }

    def transfer_leader(self, guild_id, from_account, to_account, now=0.0):
        with self._mu:
            guild = self._guild(guild_id)
            self._ensure_active(guild)
            if from_account != guild.leader_id:
                raise ValueError("only the current leader can transfer")
            if to_account not in guild.members:
                raise ValueError("successor must be a guild member")
            if (guild.last_transfer is not None
                    and now - guild.last_transfer < self.TRANSFER_COOLDOWN):
                raise ValueError("leader transfer is on cooldown")
            guild.leader_id = to_account
            guild.last_transfer = now
            self._bump(guild)
            return {"leader": to_account}

    def leader(self, guild_id):
        with self._mu:
            return self._guild(guild_id).leader_id
