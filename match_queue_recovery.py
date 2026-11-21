"""Durable matchmaking queue with snapshots, journal and leases."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager


class MatchQueueRecovery:
    """Durable matchmaking queue with snapshots, journal and leases.

    ``tickets`` and ``matches`` are materialised state. Every state change is
    also appended to a journal in the same transaction, so a snapshot plus the
    journal entries after it describe exactly the same state. Writes use
    optimistic versions where the caller supplies one (``update``), and all
    queue ownership changes happen under an immediate SQLite transaction.
    """

    _ACTIVE_STATUSES = ("queued", "claimed")

    def __init__(self, path, snapshot_every=60.0):
        self.path = path
        self.snapshot_every = snapshot_every
        self._db_path = str(path)
        self._tickets = {}
        self._matches = {}
        self._last_snapshot_at = None
        self._memory_conn = None
        if self._db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self._db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        else:
            self._memory_conn = sqlite3.connect(
                ":memory:", timeout=30.0, check_same_thread=False
            )
            self._memory_conn.row_factory = sqlite3.Row
            self._memory_conn.execute("PRAGMA busy_timeout = 30000")
        self._init_schema()

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------
    def _connect(self):
        if self._memory_conn is not None:
            return self._memory_conn
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _release(self, conn):
        if conn is not self._memory_conn:
            conn.close()

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._release(conn)

    @contextmanager
    def _reader(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            self._release(conn)

    def _init_schema(self):
        with self._transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    players TEXT NOT NULL,
                    priority NUMERIC NOT NULL DEFAULT 0,
                    group_id TEXT,
                    status TEXT NOT NULL,
                    enqueued_at REAL NOT NULL,
                    expires_at REAL,
                    version INTEGER NOT NULL DEFAULT 1,
                    claimed_by TEXT,
                    lease_until REAL,
                    match_id TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS matches (
                    match_id TEXT PRIMARY KEY,
                    ticket_ids TEXT NOT NULL,
                    players TEXT NOT NULL,
                    node_id TEXT,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    compensated_at REAL,
                    version INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS journal (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seq INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    state TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _ticket_from_row(row):
        ticket = {
            "ticket_id": row["ticket_id"],
            "players": json.loads(row["players"]),
            "priority": row["priority"],
            "group": row["group_id"],
            "status": row["status"],
            "enqueued_at": row["enqueued_at"],
            "expires_at": row["expires_at"],
            "version": row["version"],
            "claimed_by": row["claimed_by"],
            "lease_until": row["lease_until"],
            "match_id": row["match_id"],
            "updated_at": row["updated_at"],
        }
        # Storage calls this claimed_by; callers of claim() know it as the
        # lease owner, so expose both names.
        ticket["node_id"] = ticket["claimed_by"]
        return ticket

    @staticmethod
    def _match_from_row(row):
        return {
            "match_id": row["match_id"],
            "ticket_ids": json.loads(row["ticket_ids"]),
            "players": json.loads(row["players"]),
            "node_id": row["node_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "compensated_at": row["compensated_at"],
            "version": row["version"],
        }

    def _append_journal(self, conn, kind, payload, now):
        conn.execute(
            "INSERT INTO journal (ts, kind, payload) VALUES (?, ?, ?)",
            (float(now), kind, json.dumps(payload, sort_keys=True)),
        )

    def _write_ticket(self, conn, ticket, now):
        conn.execute(
            """
            INSERT OR REPLACE INTO tickets
                (ticket_id, players, priority, group_id, status, enqueued_at,
                 expires_at, version, claimed_by, lease_until, match_id,
                 updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket["ticket_id"],
                json.dumps(ticket["players"]),
                ticket["priority"],
                ticket.get("group"),
                ticket["status"],
                ticket["enqueued_at"],
                ticket.get("expires_at"),
                ticket["version"],
                ticket.get("claimed_by"),
                ticket.get("lease_until"),
                ticket.get("match_id"),
                ticket.get("updated_at", now),
            ),
        )
        self._append_journal(conn, "ticket", ticket, now)
        self._maybe_snapshot(conn, now)

    def _write_match(self, conn, match, now):
        conn.execute(
            """
            INSERT OR REPLACE INTO matches
                (match_id, ticket_ids, players, node_id, status, created_at,
                 compensated_at, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match["match_id"],
                json.dumps(match["ticket_ids"]),
                json.dumps(match["players"]),
                match.get("node_id"),
                match["status"],
                match["created_at"],
                match.get("compensated_at"),
                match["version"],
            ),
        )
        self._append_journal(conn, "match", match, now)
        self._maybe_snapshot(conn, now)

    def _get_ticket(self, conn, ticket_id):
        row = conn.execute(
            "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        return None if row is None else self._ticket_from_row(row)

    def _get_match(self, conn, match_id):
        row = conn.execute(
            "SELECT * FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()
        return None if row is None else self._match_from_row(row)

    # ------------------------------------------------------------------
    # Snapshots and journal replay
    # ------------------------------------------------------------------
    def _snapshot_state(self, conn):
        tickets = {
            row["ticket_id"]: self._ticket_from_row(row)
            for row in conn.execute("SELECT * FROM tickets")
        }
        matches = {
            row["match_id"]: self._match_from_row(row)
            for row in conn.execute("SELECT * FROM matches")
        }
        return {"tickets": tickets, "matches": matches}

    def _write_snapshot(self, conn, now):
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM journal"
        ).fetchone()
        seq = int(seq_row["seq"])
        state = self._snapshot_state(conn)
        conn.execute(
            "INSERT INTO snapshots (seq, created_at, state) VALUES (?, ?, ?)",
            (seq, float(now), json.dumps(state, sort_keys=True)),
        )
        self._last_snapshot_at = float(now)
        return seq

    def _maybe_snapshot(self, conn, now):
        if self.snapshot_every is None:
            return
        now = float(now)
        if self._last_snapshot_at is None:
            # The journal already protects this state; start the interval now.
            self._last_snapshot_at = now
            return
        if (
            self.snapshot_every <= 0
            or now - self._last_snapshot_at >= self.snapshot_every
        ):
            self._write_snapshot(conn, now)

    def _load_replayed_state(self):
        with self._reader() as conn:
            conn.execute("BEGIN")
            try:
                snapshot = conn.execute(
                    "SELECT seq, created_at, state FROM snapshots "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if snapshot is None:
                    snapshot_seq = 0
                    state = {"tickets": {}, "matches": {}}
                    snapshot_at = None
                else:
                    snapshot_seq = int(snapshot["seq"])
                    state = json.loads(snapshot["state"])
                    snapshot_at = snapshot["created_at"]

                rows = conn.execute(
                    "SELECT kind, payload FROM journal WHERE seq > ? ORDER BY seq",
                    (snapshot_seq,),
                ).fetchall()
                for row in rows:
                    payload = json.loads(row["payload"])
                    if row["kind"] == "ticket":
                        state.setdefault("tickets", {})[payload["ticket_id"]] = payload
                    elif row["kind"] == "match":
                        state.setdefault("matches", {})[payload["match_id"]] = payload
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return state, snapshot_at

    @staticmethod
    def _is_expired(ticket, now):
        expires_at = ticket.get("expires_at")
        return (
            expires_at is not None
            and float(expires_at) <= float(now)
            and ticket.get("status") in MatchQueueRecovery._ACTIVE_STATUSES
        )

    def _expire_ticket(self, conn, ticket, now):
        ticket = dict(ticket)
        ticket["status"] = "expired"
        ticket["claimed_by"] = None
        ticket["node_id"] = None
        ticket["lease_until"] = None
        ticket["updated_at"] = float(now)
        ticket["version"] = int(ticket.get("version", 1)) + 1
        self._write_ticket(conn, ticket, now)
        self._tickets[ticket["ticket_id"]] = ticket
        return ticket

    def restore(self, now=0.0):
        state, snapshot_at = self._load_replayed_state()
        self._tickets = dict(state.get("tickets", {}))
        self._matches = dict(state.get("matches", {}))
        self._last_snapshot_at = snapshot_at

        expired = []
        with self._transaction() as conn:
            for ticket_id in sorted(self._tickets):
                ticket = self._tickets[ticket_id]
                if not self._is_expired(ticket, now):
                    continue
                self._expire_ticket(conn, ticket, now)
                expired.append(ticket_id)

        restored = sum(
            1
            for ticket in self._tickets.values()
            if ticket.get("status") == "queued"
        )
        return {
            "restored": restored,
            "expired": expired,
            "tickets": len(self._tickets),
            "matches": len(self._matches),
        }

    def persist(self, now=0.0):
        with self._transaction() as conn:
            seq = self._write_snapshot(conn, now)
        return {"snapshot_seq": seq, "path": self._db_path}

    # ------------------------------------------------------------------
    # Queue operations
    # ------------------------------------------------------------------
    def enqueue(
        self,
        ticket_id,
        players,
        priority=0,
        group=None,
        expires_at=None,
        now=0.0,
    ):
        ticket = {
            "ticket_id": str(ticket_id),
            "players": list(players),
            "priority": priority,
            "group": group,
            "status": "queued",
            "enqueued_at": float(now),
            "expires_at": None if expires_at is None else float(expires_at),
            "version": 1,
            "claimed_by": None,
            "node_id": None,
            "lease_until": None,
            "match_id": None,
            "updated_at": float(now),
        }
        with self._transaction() as conn:
            if self._get_ticket(conn, ticket["ticket_id"]) is not None:
                raise ValueError("ticket already exists: %s" % ticket_id)
            self._write_ticket(conn, ticket, now)
        self._tickets[ticket["ticket_id"]] = ticket
        return dict(ticket)

    def cancel(self, ticket_id, now=0.0):
        with self._transaction() as conn:
            ticket = self._get_ticket(conn, ticket_id)
            if ticket is None or ticket["status"] == "cancelled":
                return {"ticket_id": ticket_id, "cancelled": False}
            if ticket["status"] not in self._ACTIVE_STATUSES:
                return {"ticket_id": ticket_id, "cancelled": False}

            ticket = dict(ticket)
            ticket["status"] = "cancelled"
            ticket["claimed_by"] = None
            ticket["node_id"] = None
            ticket["lease_until"] = None
            ticket["updated_at"] = float(now)
            ticket["version"] = int(ticket["version"]) + 1
            self._write_ticket(conn, ticket, now)
        self._tickets[ticket_id] = ticket
        return {
            "ticket_id": ticket_id,
            "cancelled": True,
            "status": "cancelled",
            "version": ticket["version"],
        }

    def claim(self, ticket_id, node_id, now=0.0, lease=30.0):
        with self._transaction() as conn:
            ticket = self._get_ticket(conn, ticket_id)
            if ticket is None or ticket["status"] not in self._ACTIVE_STATUSES:
                return None
            if self._is_expired(ticket, now):
                self._expire_ticket(conn, ticket, now)
                return None

            if ticket["status"] == "claimed":
                holder = ticket.get("claimed_by")
                lease_until = ticket.get("lease_until")
                if (
                    holder is not None
                    and holder != node_id
                    and lease_until is not None
                    and float(lease_until) > float(now)
                ):
                    return None

            ticket = dict(ticket)
            ticket["status"] = "claimed"
            ticket["claimed_by"] = node_id
            ticket["node_id"] = node_id
            ticket["lease_until"] = float(now) + float(lease)
            ticket["updated_at"] = float(now)
            ticket["version"] = int(ticket["version"]) + 1
            self._write_ticket(conn, ticket, now)
        self._tickets[ticket_id] = ticket
        return dict(ticket)

    def match(self, ticket_ids, match_id, node_id, now=0.0, fail_create=False):
        ticket_ids = list(dict.fromkeys(ticket_ids))
        if not ticket_ids:
            raise ValueError("at least one ticket is required")

        with self._transaction() as conn:
            existing = self._get_match(conn, match_id)
            if existing is not None:
                return existing

            tickets = []
            for ticket_id in ticket_ids:
                ticket = self._get_ticket(conn, ticket_id)
                if ticket is None:
                    raise ValueError("unknown ticket: %s" % ticket_id)
                if ticket["status"] not in self._ACTIVE_STATUSES:
                    raise ValueError(
                        "ticket %s cannot be matched from status %s"
                        % (ticket_id, ticket["status"])
                    )
                if self._is_expired(ticket, now):
                    raise ValueError("ticket %s has expired" % ticket_id)
                if ticket["status"] == "claimed":
                    holder = ticket.get("claimed_by")
                    lease_until = ticket.get("lease_until")
                    if (
                        holder is not None
                        and holder != node_id
                        and lease_until is not None
                        and float(lease_until) > float(now)
                    ):
                        raise ValueError(
                            "ticket %s is leased by %s" % (ticket_id, holder)
                        )
                tickets.append(ticket)

            if fail_create:
                raise RuntimeError("failed to create match %s" % match_id)

            players = []
            for ticket in tickets:
                for player in ticket["players"]:
                    if player not in players:
                        players.append(player)

            match = {
                "match_id": str(match_id),
                "ticket_ids": ticket_ids,
                "players": players,
                "node_id": node_id,
                "status": "active",
                "created_at": float(now),
                "compensated_at": None,
                "version": 1,
            }

            for ticket in tickets:
                ticket = dict(ticket)
                ticket["status"] = "matched"
                ticket["match_id"] = match["match_id"]
                ticket["claimed_by"] = node_id
                ticket["node_id"] = node_id
                ticket["lease_until"] = None
                ticket["updated_at"] = float(now)
                ticket["version"] = int(ticket["version"]) + 1
                self._write_ticket(conn, ticket, now)
                self._tickets[ticket["ticket_id"]] = ticket

            self._write_match(conn, match, now)
            self._matches[match["match_id"]] = match
        return dict(match)

    def compensate(self, match_id, now=0.0):
        with self._transaction() as conn:
            match = self._get_match(conn, match_id)
            if match is None:
                return {
                    "match_id": match_id,
                    "refunded": [],
                    "already_compensated": False,
                    "not_found": True,
                }
            if match["status"] == "compensated":
                return {
                    "match_id": match_id,
                    "refunded": [],
                    "already_compensated": True,
                }

            match = dict(match)
            match["status"] = "compensated"
            match["compensated_at"] = float(now)
            match["version"] = int(match["version"]) + 1

            refunded = []
            for ticket_id in match["ticket_ids"]:
                ticket = self._get_ticket(conn, ticket_id)
                if (
                    ticket is None
                    or ticket["status"] != "matched"
                    or ticket.get("match_id") != match["match_id"]
                ):
                    continue
                ticket = dict(ticket)
                if (
                    ticket.get("expires_at") is not None
                    and float(ticket["expires_at"]) <= float(now)
                ):
                    ticket["status"] = "expired"
                else:
                    ticket["status"] = "queued"
                    refunded.append(ticket_id)
                ticket["match_id"] = None
                ticket["claimed_by"] = None
                ticket["node_id"] = None
                ticket["lease_until"] = None
                ticket["updated_at"] = float(now)
                ticket["version"] = int(ticket["version"]) + 1
                self._write_ticket(conn, ticket, now)
                self._tickets[ticket_id] = ticket

            self._write_match(conn, match, now)
            self._matches[match["match_id"]] = match
        return {
            "match_id": match_id,
            "refunded": refunded,
            "already_compensated": False,
        }

    def reconnect(self, player_id, ticket_id=None, now=0.0):
        with self._transaction() as conn:
            if ticket_id is not None:
                ticket = self._get_ticket(conn, ticket_id)
                if (
                    ticket is not None
                    and ticket["status"] in self._ACTIVE_STATUSES
                    and not self._is_expired(ticket, now)
                ):
                    return ticket
                if ticket is not None and self._is_expired(ticket, now):
                    self._expire_ticket(conn, ticket, now)

            rows = conn.execute(
                "SELECT * FROM tickets WHERE status IN ('queued', 'claimed') "
                "ORDER BY enqueued_at, ticket_id"
            ).fetchall()
            for row in rows:
                ticket = self._ticket_from_row(row)
                if player_id not in ticket["players"]:
                    continue
                if self._is_expired(ticket, now):
                    self._expire_ticket(conn, ticket, now)
                    continue
                return ticket

            replacement_id = self._new_ticket_id(conn, player_id)
            ticket = {
                "ticket_id": replacement_id,
                "players": [player_id],
                "priority": 0,
                "group": None,
                "status": "queued",
                "enqueued_at": float(now),
                "expires_at": None,
                "version": 1,
                "claimed_by": None,
                "node_id": None,
                "lease_until": None,
                "match_id": None,
                "updated_at": float(now),
            }
            self._write_ticket(conn, ticket, now)
        self._tickets[ticket["ticket_id"]] = ticket
        return dict(ticket)

    @staticmethod
    def _new_ticket_id(conn, player_id):
        while True:
            candidate = "reconnect-%s-%s" % (player_id, uuid.uuid4().hex[:12])
            row = conn.execute(
                "SELECT 1 FROM tickets WHERE ticket_id = ?", (candidate,)
            ).fetchone()
            if row is None:
                return candidate

    def update(self, ticket_id, changes, expected_version, now=0.0):
        reserved = {
            "ticket_id",
            "version",
            "claimed_by",
            "node_id",
            "lease_until",
            "match_id",
            "updated_at",
        }
        with self._transaction() as conn:
            ticket = self._get_ticket(conn, ticket_id)
            if ticket is None:
                raise KeyError("unknown ticket: %s" % ticket_id)
            if int(ticket["version"]) != int(expected_version):
                raise ValueError(
                    "stale ticket version for %s: expected %s, found %s"
                    % (ticket_id, expected_version, ticket["version"])
                )

            ticket = dict(ticket)
            for key, value in changes.items():
                if key in reserved:
                    continue
                if key == "group":
                    ticket["group"] = value
                elif key == "players":
                    ticket["players"] = list(value)
                else:
                    ticket[key] = value
            ticket["updated_at"] = float(now)
            ticket["version"] = int(ticket["version"]) + 1
            self._write_ticket(conn, ticket, now)
        self._tickets[ticket_id] = ticket
        return dict(ticket)

    # ------------------------------------------------------------------
    # Read models
    # ------------------------------------------------------------------
    def ticket(self, ticket_id):
        with self._reader() as conn:
            return self._get_ticket(conn, ticket_id)

    def tickets(self):
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM tickets ORDER BY enqueued_at, ticket_id"
            ).fetchall()
            return [self._ticket_from_row(row) for row in rows]

    def matches(self):
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM matches ORDER BY created_at, match_id"
            ).fetchall()
            return [self._match_from_row(row) for row in rows]

    def peek(self, now=None):
        with self._reader() as conn:
            if now is None:
                row = conn.execute(
                    "SELECT * FROM tickets WHERE status = 'queued' "
                    "ORDER BY priority DESC, enqueued_at, ticket_id LIMIT 1"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tickets WHERE status = 'queued' "
                    "AND (expires_at IS NULL OR expires_at > ?) "
                    "ORDER BY priority DESC, enqueued_at, ticket_id LIMIT 1",
                    (float(now),),
                ).fetchone()
            return None if row is None else self._ticket_from_row(row)
