from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

from agent_bus.reputation.database import Database
from agent_bus.types import Envelope, MessageType


class IdempotencyConflict(ValueError):
    """A sender reused an idempotency key for a different operation."""


class MessageNotFound(ValueError):
    """The requested delivery does not belong to this agent or does not exist."""


class InvalidCursor(ValueError):
    """Invalid, altered or incorrectly scoped pagination cursor."""


@dataclass(frozen=True)
class SendResult:
    envelope: Envelope
    recipients: list[str]
    replayed: bool


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InboxManager:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def _transaction(self, operation, *, write=True):
        """Queue the complete transaction so sibling coroutines cannot commit it.

        The first write acquires SQLite's writer reservation before any reads;
        independent connections cannot race between lookup and mutation.
        """
        def transaction(connection):
            connection.execute("SAVEPOINT inbox_operation")
            try:
                if write:
                    connection.execute("UPDATE message_state SET value = value WHERE name = 'cursor_secret'")
                result = operation(connection)
                connection.execute("RELEASE inbox_operation")
                return result
            except BaseException:
                connection.execute("ROLLBACK TO inbox_operation")
                connection.execute("RELEASE inbox_operation")
                raise

        # aiosqlite has no public multi-statement callback. This is the same
        # confined transaction boundary used by task transfer + audit.
        result = await self._db.conn._execute(transaction, self._db.conn._conn)
        if write:
            await self._db.conn.commit()
        return result

    @staticmethod
    def _insert_delivery(connection, envelope: Envelope):
        connection.execute(
            """INSERT OR IGNORE INTO inbox
            (message_id, from_agent, to_agent, message_type, correlation_id,
             reply_needed, related_task, body, metadata, signature, timestamp, conversation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (envelope.message_id, envelope.from_agent, envelope.to_agent,
             envelope.message_type.value, envelope.correlation_id, int(envelope.reply_needed),
             envelope.related_task, _json(envelope.body) if envelope.body is not None else None,
             _json(envelope.metadata), envelope.signature, envelope.timestamp.isoformat(),
             envelope.conversation_id or envelope.message_id),
        )
        connection.execute(
            "INSERT OR IGNORE INTO inbox_delivery_state(message_id, to_agent) VALUES (?, ?)",
            (envelope.message_id, envelope.to_agent),
        )

    @staticmethod
    def _check_key(key):
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 128):
            raise ValueError("idempotency_key must contain 1 to 128 characters")

    def _send(self, connection, envelope, recipients, key, *, acknowledgement=None, reply_source=None):
        # Recipients of a broadcast are frozen on the first send, not included
        # from a potentially changed live registry on replay.
        content = envelope.model_dump(mode="json", exclude={"message_id", "timestamp"})
        content["acknowledgement"] = acknowledgement
        content["reply_source"] = reply_source
        fingerprint = hashlib.sha256(_json(content).encode()).hexdigest()
        outgoing = envelope.model_copy(update={"conversation_id": envelope.conversation_id or envelope.message_id})
        if key is not None:
            cursor = connection.execute(
                "INSERT INTO message_idempotency "
                "(from_agent, idempotency_key, fingerprint, envelope_json, recipients_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(from_agent, idempotency_key) DO NOTHING",
                (envelope.from_agent, key, fingerprint, outgoing.model_dump_json(), _json(recipients)),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT fingerprint, envelope_json, recipients_json FROM message_idempotency "
                    "WHERE from_agent = ? AND idempotency_key = ?", (envelope.from_agent, key),
                ).fetchone()
                if row["fingerprint"] != fingerprint:
                    raise IdempotencyConflict("Idempotency key already used with different content")
                return SendResult(Envelope.model_validate_json(row["envelope_json"]),
                                  json.loads(row["recipients_json"]), True)
        # WebSocket/internal callers can supply their own message ID. A
        # collision must not turn INSERT OR IGNORE into a false delivery success.
        previous = connection.execute(
            "SELECT * FROM inbox WHERE message_id = ? LIMIT 1", (outgoing.message_id,),
        ).fetchone()
        if previous is not None:
            stored = self._row_to_envelope(previous)
            excluded = {"message_id", "timestamp", "to_agent"}
            if stored.model_dump(mode="json", exclude=excluded) != outgoing.model_dump(mode="json", exclude=excluded):
                raise IdempotencyConflict("Message ID already exists with different content or author")
        for recipient in recipients:
            self._insert_delivery(connection, outgoing.model_copy(update={"to_agent": recipient}))
        return SendResult(outgoing, recipients, False)

    async def send(self, envelope: Envelope, recipients: list[str], *, idempotency_key: str | None = None) -> SendResult:
        self._check_key(idempotency_key)
        if not isinstance(recipients, list) or any(not isinstance(agent, str) or not agent for agent in recipients):
            raise ValueError("recipients must be a list of nonempty agent identifiers")
        recipients = list(dict.fromkeys(recipients))
        return await self._transaction(lambda conn: self._send(conn, envelope, recipients, idempotency_key))

    async def deliver(self, envelope: Envelope) -> str:
        """Compatibility entry point for internal callers using stable message IDs."""
        if not envelope.to_agent:
            raise ValueError("Cannot deliver message without to_agent")
        await self.send(envelope, [envelope.to_agent])
        return envelope.message_id

    @staticmethod
    def _acknowledge(connection, agent_id, message_ids):
        for message_id in message_ids:
            if not connection.execute(
                "SELECT 1 FROM inbox WHERE to_agent = ? AND message_id = ?", (agent_id, message_id),
            ).fetchone():
                raise MessageNotFound("Message delivery not found")
        timestamp = _now()
        for message_id in message_ids:
            connection.execute(
                "UPDATE inbox SET archived = 1, archived_at = COALESCE(archived_at, ?) "
                "WHERE to_agent = ? AND message_id = ? AND archived = 0",
                (timestamp, agent_id, message_id),
            )
        return {"acknowledged": message_ids}

    async def acknowledgments(self, agent_id: str, message_ids: list[str]) -> dict:
        if not isinstance(message_ids, list) or not 1 <= len(message_ids) <= 100 or any(
            not isinstance(value, str) or not value for value in message_ids
        ):
            raise ValueError("message_ids must contain 1 to 100 nonempty identifiers")
        ids = list(dict.fromkeys(message_ids))
        return await self._transaction(lambda conn: self._acknowledge(conn, agent_id, ids))

    async def archive(self, agent_id: str, msg_id: str) -> None:
        await self.acknowledgments(agent_id, [msg_id])

    async def reply(self, agent_id: str, message_id: str, body: dict, *, idempotency_key: str,
                    reply_needed=False, acknowledge=False, actor_id: str | None = None) -> SendResult:
        self._check_key(idempotency_key)
        if idempotency_key is None:
            raise ValueError("Reply requires an idempotency key")
        if not isinstance(body, dict):
            raise ValueError("Reply body must be an object")

        def operation(connection):
            row = connection.execute(
                "SELECT * FROM inbox WHERE to_agent = ? AND message_id = ?", (agent_id, message_id),
            ).fetchone()
            if row is None:
                raise MessageNotFound("Message delivery not found")
            parent = self._row_to_envelope(row)
            outgoing = Envelope(
                from_agent=actor_id or agent_id, to_agent=parent.from_agent, message_type=MessageType.INBOX,
                body=body, reply_needed=reply_needed, related_task=parent.related_task,
                correlation_id=parent.message_id,
                conversation_id=parent.conversation_id or parent.correlation_id or parent.message_id,
            )
            result = self._send(connection, outgoing, [parent.from_agent], idempotency_key,
                                acknowledgement=message_id if acknowledge else None,
                                reply_source=[agent_id, message_id])
            if acknowledge:
                self._acknowledge(connection, agent_id, [message_id])
            return result

        return await self._transaction(operation)

    async def get_inbox(self, agent_id: str) -> list[Envelope]:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM inbox WHERE to_agent = ? AND archived = 0 ORDER BY timestamp", (agent_id,),
        )
        return [self._row_to_envelope(row) for row in rows]

    async def get_message(self, agent_id: str, msg_id: str) -> Envelope | None:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM inbox WHERE to_agent = ? AND message_id = ?", (agent_id, msg_id),
        )
        return self._row_to_envelope(rows[0]) if rows else None

    async def get_archived(self, agent_id: str) -> list[Envelope]:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM inbox WHERE to_agent = ? AND archived = 1 ORDER BY timestamp DESC", (agent_id,),
        )
        return [self._row_to_envelope(row) for row in rows]

    async def pending_count(self, agent_id: str) -> int:
        rows = await self._db.conn.execute_fetchall(
            "SELECT COUNT(*) FROM inbox WHERE to_agent = ? AND archived = 0", (agent_id,),
        )
        return rows[0][0]

    async def pending_summary(self, agent_id: str) -> dict:
        def operation(connection):
            totals = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(reply_needed), 0) FROM inbox WHERE to_agent = ? AND archived = 0",
                (agent_id,),
            ).fetchone()
            latest = connection.execute(
                "SELECT from_agent, body FROM inbox WHERE to_agent = ? AND archived = 0 ORDER BY timestamp DESC LIMIT 5",
                (agent_id,),
            ).fetchall()
            return {
                "count": totals[0], "reply_needed": totals[1],
                "latest_senders": list(dict.fromkeys(row[0] for row in latest)),
                "latest_summary": [{"from": row[0], "text": str(json.loads(row[1]) if row[1] else None)[:60]}
                                   for row in reversed(latest[:3])],
            }
        return await self._transaction(operation, write=False)

    async def delivery_state(self, agent_id: str, message_id: str) -> dict | None:
        rows = await self._db.conn.execute_fetchall(
            "SELECT i.archived, i.archived_at, s.attempts, s.last_error, s.failed_at, s.sequence "
            "FROM inbox i JOIN inbox_delivery_state s USING(message_id, to_agent) "
            "WHERE i.to_agent = ? AND i.message_id = ?", (agent_id, message_id),
        )
        if not rows:
            return None
        row = rows[0]
        return {"acknowledged": bool(row[0]), "acknowledged_at": row[1], "attempts": row[2],
                "last_error": row[3], "failed_at": row[4], "sequence": row[5]}

    async def record_failure(self, agent_id: str, message_id: str, error: str) -> dict:
        if not isinstance(error, str) or not 1 <= len(error) <= 2000:
            raise ValueError("error must contain 1 to 2000 characters")
        def operation(connection):
            row = connection.execute(
                "SELECT archived FROM inbox WHERE to_agent = ? AND message_id = ?", (agent_id, message_id),
            ).fetchone()
            if row is None:
                raise MessageNotFound("Message delivery not found")
            if row[0]:
                raise ValueError("An acknowledged delivery cannot record a failure")
            connection.execute(
                "UPDATE inbox_delivery_state SET attempts = attempts + 1, last_error = ?, failed_at = ? "
                "WHERE to_agent = ? AND message_id = ?", (error, _now(), agent_id, message_id),
            )
        await self._transaction(operation)
        return await self.delivery_state(agent_id, message_id)

    @staticmethod
    def _encode_cursor(payload, secret):
        raw = _json(payload).encode()
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        signature = hmac.new(bytes.fromhex(secret), raw, hashlib.sha256).hexdigest()
        return encoded + "." + signature

    @staticmethod
    def _decode_cursor(cursor, secret, agent_id, reply_needed):
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ValueError()
            encoded, signature = cursor.split(".")
            raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            expected = hmac.new(bytes.fromhex(secret), raw, hashlib.sha256).hexdigest()
            payload = json.loads(raw)
            if not hmac.compare_digest(signature, expected) or payload["v"] != 1:
                raise ValueError()
            if payload["agent"] != agent_id or payload["reply_needed"] is not reply_needed:
                raise ValueError()
            if any(type(payload[field]) is not int or payload[field] < 0 for field in ("after", "upper")):
                raise ValueError()
            if payload["after"] > payload["upper"]:
                raise ValueError()
            return payload
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise InvalidCursor("Invalid cursor or cursor scope does not match") from None

    async def page(self, agent_id: str, *, cursor=None, limit=50, reply_needed: bool | None = None) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if reply_needed is not None and type(reply_needed) is not bool:
            raise ValueError("reply_needed must be a boolean")

        def operation(connection):
            secret = connection.execute("SELECT value FROM message_state WHERE name = 'cursor_secret'").fetchone()[0]
            if cursor is not None:
                position = self._decode_cursor(cursor, secret, agent_id, reply_needed)
            else:
                upper = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM inbox_delivery_state WHERE to_agent = ?", (agent_id,),
                ).fetchone()[0]
                position = {"v": 1, "agent": agent_id, "reply_needed": reply_needed, "after": 0, "upper": upper}
            query = (
                "SELECT i.*, s.sequence FROM inbox i JOIN inbox_delivery_state s USING(message_id, to_agent) "
                "WHERE i.to_agent = ? AND i.archived = 0 AND s.sequence > ? AND s.sequence <= ?"
            )
            params = [agent_id, position["after"], position["upper"]]
            if reply_needed is not None:
                query += " AND i.reply_needed = ?"
                params.append(int(reply_needed))
            rows = connection.execute(query + " ORDER BY s.sequence LIMIT ?", params + [limit + 1]).fetchall()
            messages = [self._row_to_envelope(row).model_dump(mode="json") for row in rows[:limit]]
            next_cursor = None
            if len(rows) > limit:
                position["after"] = rows[limit - 1]["sequence"]
                next_cursor = self._encode_cursor(position, secret)
            return {"messages": messages, "next_cursor": next_cursor}

        return await self._transaction(operation, write=False)

    async def cleanup_expired(self, max_age_days: int = 30) -> int:
        def operation(connection):
            cursor = connection.execute(
                "DELETE FROM inbox WHERE archived = 1 AND julianday(archived_at) < julianday('now', ?)",
                (f"-{max_age_days} days",),
            )
            # Sequence tombstones and idempotency records deliberately survive
            # retention, so old cursors and retried sends cannot target new rows.
            return cursor.rowcount
        return await self._transaction(operation)

    @staticmethod
    def _row_to_envelope(row: aiosqlite.Row | tuple) -> Envelope:
        values = tuple(row)
        return Envelope(
            message_id=values[0], from_agent=values[1], to_agent=values[2],
            message_type=MessageType(values[3]), correlation_id=values[4],
            reply_needed=bool(values[5]), related_task=values[6],
            body=json.loads(values[7]) if values[7] else None,
            metadata=json.loads(values[8]) if values[8] else {}, signature=values[9],
            timestamp=datetime.fromisoformat(values[10]),
            conversation_id=values[13] if len(values) > 13 else values[4] or values[0],
        )
