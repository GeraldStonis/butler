"""
SQLite database layer for Nixi.

Uses aiosqlite for fully async operations. Manages schema creation,
message storage, summaries, long-term memory, owner instructions,
and emoji ownership caching.
"""

from __future__ import annotations

import aiosqlite
import config

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    """Return the singleton database connection, initialising if needed."""
    global _db
    if _db is None:
        _db = await aiosqlite.connect(config.DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        await _create_tables(_db)
    return _db


async def close() -> None:
    """Gracefully close the database connection."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# ── Schema ───────────────────────────────────────────────────────────────────

async def _create_tables(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id  INTEGER NOT NULL,
            guild_id    INTEGER,
            author_id   INTEGER NOT NULL,
            author_name TEXT    NOT NULL,
            is_bot      INTEGER DEFAULT 0,
            content     TEXT    NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_messages_channel_time
            ON messages (channel_id, created_at);

        CREATE TABLE IF NOT EXISTS channel_summaries (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id       INTEGER NOT NULL,
            guild_id         INTEGER,
            summary          TEXT    NOT NULL,
            messages_covered INTEGER NOT NULL,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_summaries_channel
            ON channel_summaries (channel_id, created_at);

        CREATE TABLE IF NOT EXISTS long_term_memory (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            category   TEXT NOT NULL,
            subject    TEXT,
            content    TEXT NOT NULL,
            source     TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS owner_instructions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            instruction TEXT    NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            active      INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS emoji_owners (
            emoji_id    INTEGER PRIMARY KEY,
            emoji_name  TEXT    NOT NULL,
            uploader_id INTEGER NOT NULL,
            cached_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_activity (
            guild_id         INTEGER NOT NULL,
            user_id          INTEGER NOT NULL,
            channel_id       INTEGER NOT NULL,
            display_name     TEXT    NOT NULL,
            last_message_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_user_activity_guild_time
            ON user_activity (guild_id, last_message_at);

        CREATE TABLE IF NOT EXISTS channel_activity (
            channel_id       INTEGER PRIMARY KEY,
            guild_id         INTEGER,
            last_user_id     INTEGER,
            last_message_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_dead_ping_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bot_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await db.commit()


# ── Messages ─────────────────────────────────────────────────────────────────

async def save_message(
    channel_id: int,
    guild_id: int | None,
    author_id: int,
    author_name: str,
    content: str,
    is_bot: bool = False,
) -> None:
    """Store a single message in the rolling buffer."""
    db = await get_db()
    await db.execute(
        """INSERT INTO messages
           (channel_id, guild_id, author_id, author_name, is_bot, content)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (channel_id, guild_id, author_id, author_name, int(is_bot), content),
    )
    await db.commit()


async def record_user_activity(
    guild_id: int | None,
    channel_id: int,
    user_id: int,
    display_name: str,
) -> None:
    """Record the most recent non-bot activity for a user and channel."""
    if guild_id is None:
        return
    db = await get_db()
    await db.execute(
        """INSERT INTO user_activity
           (guild_id, user_id, channel_id, display_name, last_message_at)
           VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(guild_id, user_id) DO UPDATE SET
               channel_id = excluded.channel_id,
               display_name = excluded.display_name,
               last_message_at = CURRENT_TIMESTAMP""",
        (guild_id, user_id, channel_id, display_name),
    )
    await db.execute(
        """INSERT INTO channel_activity
           (channel_id, guild_id, last_user_id, last_message_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(channel_id) DO UPDATE SET
               guild_id = excluded.guild_id,
               last_user_id = excluded.last_user_id,
               last_message_at = CURRENT_TIMESTAMP""",
        (channel_id, guild_id, user_id),
    )
    await db.commit()


async def get_user_idle_seconds(guild_id: int | None, user_id: int) -> float | None:
    """Return seconds since a user's last non-bot message in this guild."""
    if guild_id is None:
        return None
    db = await get_db()
    cursor = await db.execute(
        """SELECT (julianday('now') - julianday(last_message_at)) * 86400.0 AS idle
           FROM user_activity
           WHERE guild_id = ? AND user_id = ?""",
        (guild_id, user_id),
    )
    row = await cursor.fetchone()
    return float(row["idle"]) if row and row["idle"] is not None else None


async def get_channel_idle_candidates(
    channel_ids: list[int] | None = None,
) -> list[dict]:
    """Return channel activity rows for dead-chat checks."""
    db = await get_db()
    if channel_ids:
        placeholders = ",".join("?" for _ in channel_ids)
        cursor = await db.execute(
            f"""SELECT channel_id, guild_id, last_user_id, last_message_at,
                       last_dead_ping_at,
                       (julianday('now') - julianday(last_message_at)) * 86400.0 AS idle_seconds,
                       CASE
                           WHEN last_dead_ping_at IS NULL THEN NULL
                           ELSE (julianday('now') - julianday(last_dead_ping_at)) * 86400.0
                       END AS cooldown_seconds
                FROM channel_activity
                WHERE channel_id IN ({placeholders})""",
            channel_ids,
        )
    else:
        cursor = await db.execute(
            """SELECT channel_id, guild_id, last_user_id, last_message_at,
                      last_dead_ping_at,
                      (julianday('now') - julianday(last_message_at)) * 86400.0 AS idle_seconds,
                      CASE
                          WHEN last_dead_ping_at IS NULL THEN NULL
                          ELSE (julianday('now') - julianday(last_dead_ping_at)) * 86400.0
                      END AS cooldown_seconds
               FROM channel_activity"""
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def mark_dead_chat_ping(channel_id: int) -> None:
    """Record that Nixi sent a dead-chat nudge in this channel."""
    db = await get_db()
    await db.execute(
        """INSERT INTO channel_activity
           (channel_id, last_dead_ping_at)
           VALUES (?, CURRENT_TIMESTAMP)
           ON CONFLICT(channel_id) DO UPDATE SET
               last_dead_ping_at = CURRENT_TIMESTAMP""",
        (channel_id,),
    )
    await db.commit()


async def get_state(key: str) -> str | None:
    """Fetch a small persistent bot state value."""
    db = await get_db()
    cursor = await db.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else None


async def set_state(key: str, value: str) -> None:
    """Set a small persistent bot state value."""
    db = await get_db()
    await db.execute(
        """INSERT INTO bot_state (key, value, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET
               value = excluded.value,
               updated_at = CURRENT_TIMESTAMP""",
        (key, value),
    )
    await db.commit()


async def get_recent_messages(
    channel_id: int, limit: int | None = None
) -> list[dict]:
    """Fetch the most recent messages for a channel, oldest-first."""
    limit = limit or config.CONTEXT_WINDOW_SIZE
    db = await get_db()
    cursor = await db.execute(
        """SELECT author_name, is_bot, content, created_at
           FROM messages
           WHERE channel_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (channel_id, limit),
    )
    rows = await cursor.fetchall()
    return [
        {
            "author": row["author_name"],
            "is_bot": bool(row["is_bot"]),
            "content": row["content"],
            "created_at": row["created_at"],
        }
        for row in reversed(rows)  # oldest first
    ]


async def count_messages(channel_id: int) -> int:
    """Count total stored messages for a channel."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) as cnt FROM messages WHERE channel_id = ?",
        (channel_id,),
    )
    row = await cursor.fetchone()
    return row["cnt"]


async def get_oldest_messages(channel_id: int, limit: int) -> list[dict]:
    """Fetch the oldest N messages for a channel (for summarisation)."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT id, author_name, is_bot, content, created_at
           FROM messages
           WHERE channel_id = ?
           ORDER BY created_at ASC
           LIMIT ?""",
        (channel_id, limit),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def delete_messages_by_ids(ids: list[int]) -> None:
    """Delete messages by their database IDs (after summarisation)."""
    if not ids:
        return
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"DELETE FROM messages WHERE id IN ({placeholders})", ids
    )
    await db.commit()


# ── Channel Summaries ────────────────────────────────────────────────────────

async def save_summary(
    channel_id: int,
    guild_id: int | None,
    summary: str,
    messages_covered: int,
) -> None:
    """Store a conversation summary."""
    db = await get_db()
    await db.execute(
        """INSERT INTO channel_summaries
           (channel_id, guild_id, summary, messages_covered)
           VALUES (?, ?, ?, ?)""",
        (channel_id, guild_id, summary, messages_covered),
    )
    await db.commit()


async def get_latest_summary(channel_id: int) -> str | None:
    """Get the most recent summary for a channel."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT summary FROM channel_summaries
           WHERE channel_id = ?
           ORDER BY created_at DESC
           LIMIT 1""",
        (channel_id,),
    )
    row = await cursor.fetchone()
    return row["summary"] if row else None


async def clear_channel_memory(channel_id: int) -> None:
    """Delete all rolling messages and summaries for a specific channel."""
    db = await get_db()
    await db.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))
    await db.execute("DELETE FROM channel_summaries WHERE channel_id = ?", (channel_id,))
    await db.commit()


# ── Long-Term Memory ────────────────────────────────────────────────────────

async def save_memory(
    category: str,
    content: str,
    subject: str | None = None,
    source: str = "observed",
) -> None:
    """Store a long-term memory fact."""
    db = await get_db()
    await db.execute(
        """INSERT INTO long_term_memory (category, subject, content, source)
           VALUES (?, ?, ?, ?)""",
        (category, subject, content, source),
    )
    await db.commit()


async def search_memories(keywords: list[str], limit: int = 10) -> list[dict]:
    """Search long-term memories by keyword overlap."""
    if not keywords:
        return []
    db = await get_db()
    # Build a simple OR query across content and subject
    conditions = " OR ".join(
        "content LIKE ? OR subject LIKE ?" for _ in keywords
    )
    params = []
    for kw in keywords:
        pattern = f"%{kw}%"
        params.extend([pattern, pattern])

    cursor = await db.execute(
        f"""SELECT category, subject, content, source, created_at
            FROM long_term_memory
            WHERE {conditions}
            ORDER BY updated_at DESC
            LIMIT ?""",
        (*params, limit),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_all_memories(limit: int = 50) -> list[dict]:
    """Get the most recent long-term memories."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT category, subject, content, source, created_at
           FROM long_term_memory
           ORDER BY updated_at DESC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ── Owner Instructions ───────────────────────────────────────────────────────

async def save_instruction(instruction: str) -> int:
    """Store a new owner standing instruction. Returns the ID."""
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO owner_instructions (instruction) VALUES (?)",
        (instruction,),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def get_active_instructions() -> list[str]:
    """Fetch all active owner instructions."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT instruction FROM owner_instructions
           WHERE active = 1
           ORDER BY created_at ASC""",
    )
    rows = await cursor.fetchall()
    return [row["instruction"] for row in rows]


async def clear_instructions() -> None:
    """Deactivate all owner instructions."""
    db = await get_db()
    await db.execute("UPDATE owner_instructions SET active = 0")
    await db.commit()


# ── Emoji Owners ─────────────────────────────────────────────────────────────

async def cache_emoji_owner(
    emoji_id: int, emoji_name: str, uploader_id: int
) -> None:
    """Cache an emoji's uploader."""
    db = await get_db()
    await db.execute(
        """INSERT OR REPLACE INTO emoji_owners (emoji_id, emoji_name, uploader_id)
           VALUES (?, ?, ?)""",
        (emoji_id, emoji_name, uploader_id),
    )
    await db.commit()


async def get_owner_emoji_ids(owner_id: int) -> set[int]:
    """Get all emoji IDs uploaded by the owner."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT emoji_id FROM emoji_owners WHERE uploader_id = ?",
        (owner_id,),
    )
    rows = await cursor.fetchall()
    return {row["emoji_id"] for row in rows}
