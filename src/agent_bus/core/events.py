"""Bounded, durable delivery events with signed, scope-bound resume cursors.

Event retention never deletes inbox deliveries. The global floor describes a
pruned prefix: consumers behind it must take a fresh checkpoint and rescan the
pending inbox. Agent scope ``'all'`` is distinct from the global scope ``None``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING

from agent_bus.types import Envelope

if TYPE_CHECKING:
    from agent_bus.reputation.database import Database

DEFAULT_RETENTION_SECONDS = 604800
DEFAULT_MAX_EVENTS = 10000


class CursorInvalid(ValueError):
    """Malformed, incorrectly scoped, forged or future event cursor."""


class CursorExpired(ValueError):
    """The event history needed to resume this cursor has been pruned."""


def _prune(connection, retention_seconds, max_events):
    ttl = connection.execute(
        "SELECT COALESCE(MAX(event_id), 0) FROM event_log WHERE created_at <= ?",
        (time.time() - retention_seconds,),
    ).fetchone()[0]
    excess = connection.execute(
        "SELECT event_id FROM event_log ORDER BY event_id DESC LIMIT 1 OFFSET ?", (max_events,),
    ).fetchone()
    cutoff = max(ttl, excess[0] if excess else 0)
    if cutoff:
        # Prune a prefix even if the wall clock moved backwards. This makes the
        # floor an honest, monotonic statement about the resumable history.
        connection.execute("DELETE FROM event_log WHERE event_id <= ?", (cutoff,))
        connection.execute("UPDATE event_state SET floor = MAX(floor, ?) WHERE singleton = 1", (cutoff,))


def append_event(connection, envelope: Envelope, *, retention_seconds=DEFAULT_RETENTION_SECONDS,
                 max_events=DEFAULT_MAX_EVENTS):
    """Append inside the caller's delivery transaction; never commit here.

    The delivery tombstone remembers the event even after event/inbox retention,
    so redelivery or a future initialization cannot recreate a purged event.
    """
    claimed = connection.execute(
        "UPDATE inbox_delivery_state SET event_recorded = 1 "
        "WHERE message_id = ? AND to_agent = ? AND event_recorded = 0",
        (envelope.message_id, envelope.to_agent),
    )
    if claimed.rowcount != 1:
        return
    connection.execute(
        "INSERT INTO event_log(message_id, to_agent, envelope_json, created_at) VALUES (?, ?, ?, ?)",
        (envelope.message_id, envelope.to_agent, envelope.model_dump_json(), time.time()),
    )
    _prune(connection, retention_seconds, max_events)


def backfill_events(connection):
    """One-time upgrade: append current pending deliveries in sequence order.

    Historical archived deliveries are excluded. Retention starts at migration
    time for this backfill. Its marker and per-delivery tombstones survive purge.
    """
    marker = connection.execute(
        "INSERT INTO message_state(name, value) VALUES ('events_backfill_v1', '1') ON CONFLICT(name) DO NOTHING"
    )
    if marker.rowcount != 1:
        return
    rows = connection.execute(
        "SELECT i.* FROM inbox i JOIN inbox_delivery_state s USING(message_id, to_agent) "
        "WHERE i.archived = 0 ORDER BY s.sequence"
    ).fetchall()
    for row in rows:
        append_event(connection, Envelope(
            message_id=row["message_id"], from_agent=row["from_agent"], to_agent=row["to_agent"],
            message_type=row["message_type"], correlation_id=row["correlation_id"],
            conversation_id=row["conversation_id"], reply_needed=bool(row["reply_needed"]),
            related_task=row["related_task"], body=json.loads(row["body"]) if row["body"] else None,
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            signature=row["signature"], timestamp=row["timestamp"],
        ))


class EventLog:
    def __init__(self, db: Database, retention_seconds=DEFAULT_RETENTION_SECONDS,
                 max_events=DEFAULT_MAX_EVENTS):
        if type(retention_seconds) is not int or retention_seconds < 1:
            raise ValueError("retention_seconds must be a positive integer")
        if type(max_events) is not int or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        self._db = db
        self.retention_seconds = retention_seconds
        self.max_events = max_events

    async def _transaction(self, operation):
        def transaction(connection):
            connection.execute("SAVEPOINT event_operation")
            try:
                # Reserve the writer before inspection: other connections cannot
                # prune/append between reading the floor and selecting the page.
                connection.execute("UPDATE event_state SET floor = floor WHERE singleton = 1")
                _prune(connection, self.retention_seconds, self.max_events)
                result = operation(connection)
                connection.execute("RELEASE event_operation")
                return result
            except BaseException:
                connection.execute("ROLLBACK TO event_operation")
                connection.execute("RELEASE event_operation")
                raise
        result = await self._db.conn._execute(transaction, self._db.conn._conn)
        await self._db.conn.commit()
        return result

    @staticmethod
    def _scope(scope):
        if scope is not None and (not isinstance(scope, str) or not scope):
            raise CursorInvalid("Invalid event scope")

    @staticmethod
    def _watermarks(connection):
        row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name = 'event_log'").fetchone()
        watermark = row[0] if row else 0
        floor = connection.execute("SELECT floor FROM event_state WHERE singleton = 1").fetchone()[0]
        secret = connection.execute("SELECT value FROM message_state WHERE name = 'cursor_secret'").fetchone()[0]
        return floor, watermark, secret

    @staticmethod
    def _encode(scope, after, secret):
        payload = json.dumps({"kind": "events", "v": 1, "scope": scope, "after": after},
                             sort_keys=True, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(bytes.fromhex(secret), payload, hashlib.sha256).hexdigest()
        return encoded + "." + signature

    @staticmethod
    def _decode(cursor, scope, secret):
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ValueError()
            encoded, signature = cursor.split(".")
            raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            expected = hmac.new(bytes.fromhex(secret), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            payload = json.loads(raw)
            if payload["kind"] != "events" or payload["v"] != 1 or payload["scope"] != scope:
                raise ValueError()
            if type(payload["after"]) is not int or payload["after"] < 0:
                raise ValueError()
            return payload["after"]
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise CursorInvalid("Invalid event cursor or cursor scope does not match") from None

    async def checkpoint(self, scope: str | None) -> str:
        self._scope(scope)
        def operation(connection):
            _, watermark, secret = self._watermarks(connection)
            return self._encode(scope, watermark, secret)
        return await self._transaction(operation)

    async def read(self, scope: str | None, cursor: str, limit=50) -> dict:
        self._scope(scope)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        def operation(connection):
            floor, watermark, secret = self._watermarks(connection)
            try:
                after = self._decode(cursor, scope, secret)
                if after > watermark:
                    raise CursorInvalid("Event cursor is ahead of this database")
                if after < floor:
                    raise CursorExpired("Event history expired; take a checkpoint and rescan pending inbox")
            except (CursorInvalid, CursorExpired) as error:
                # Persist pruning/floor advancement even when the supplied
                # cursor has just expired. The exception is raised after commit.
                return error
            query = "SELECT event_id, envelope_json FROM event_log WHERE event_id > ? AND event_id <= ?"
            params = [after, watermark]
            if scope is not None:
                query += " AND to_agent = ?"
                params.append(scope)
            rows = connection.execute(query + " ORDER BY event_id LIMIT ?", params + [limit + 1]).fetchall()
            has_more = len(rows) > limit
            selected = rows[:limit]
            position = selected[-1][0] if has_more else watermark
            return {
                "events": [{"id": self._encode(scope, row[0], secret), "event": "message",
                            "data": json.loads(row[1])} for row in selected],
                "next_cursor": self._encode(scope, position, secret),
                "has_more": has_more,
            }
        result = await self._transaction(operation)
        if isinstance(result, (CursorInvalid, CursorExpired)):
            raise result
        return result
