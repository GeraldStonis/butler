"""
SQLite database layer for Nixi.

Uses aiosqlite for fully async operations. Manages schema creation,
message storage, summaries, long-term memory, owner instructions,
and emoji ownership caching.
"""

from __future__ import annotations

import aiosqlite
import asyncio
import config

_db: aiosqlite.Connection | None = None
_init_lock = asyncio.Lock()


async def get_db() -> aiosqlite.Connection:
    """Return the singleton database connection, initialising if needed."""
    global _db
    async with _init_lock:
        if _db is None:
            connection = await aiosqlite.connect(config.DB_PATH)
            try:
                connection.row_factory = aiosqlite.Row
                await connection.execute("PRAGMA journal_mode=WAL")
                await connection.execute("PRAGMA foreign_keys=ON")
                await _create_tables(connection)
            except BaseException:
                await connection.close()
                raise
            _db = connection
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
        CREATE TABLE IF NOT EXISTS activity_scores (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            game TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, game)
        );
        CREATE TABLE IF NOT EXISTS activity_history (
            item_key TEXT PRIMARY KEY,
            selected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS member_preferences (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            roasting INTEGER NOT NULL DEFAULT 1,
            media INTEGER NOT NULL DEFAULT 1,
            pings INTEGER NOT NULL DEFAULT 1,
            avatar_edits INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS reaction_gif_history (
            gif_key TEXT PRIMARY KEY,
            selected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS meme_history (
            content_hash TEXT PRIMARY KEY,
            image_url TEXT NOT NULL UNIQUE,
            post_url TEXT UNIQUE,
            selected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );


        CREATE TABLE IF NOT EXISTS member_profiles (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            username TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS member_facts (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            fact_key TEXT NOT NULL,
            content TEXT NOT NULL,
            evidence TEXT NOT NULL,
            source TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id, fact_key)
        );
    """)
    cursor = await db.execute("PRAGMA table_info(channel_activity)")
    columns = {row["name"] for row in await cursor.fetchall()}
    for name, definition in (
        ("dead_ping_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_message_is_bot", "INTEGER NOT NULL DEFAULT 0"),
        ("last_channel_message_at", "TIMESTAMP"),
    ):
        if name not in columns:
            await db.execute(f"ALTER TABLE channel_activity ADD COLUMN {name} {definition}")
    cursor = await db.execute("PRAGMA table_info(member_preferences)")
    if "avatar_edits" not in {row["name"] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE member_preferences ADD COLUMN avatar_edits INTEGER NOT NULL DEFAULT 1")
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
           (channel_id, guild_id, last_user_id, last_message_at, last_channel_message_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
           ON CONFLICT(channel_id) DO UPDATE SET
               guild_id = excluded.guild_id,
               last_user_id = excluded.last_user_id,
               last_message_at = CURRENT_TIMESTAMP,
               last_channel_message_at = CURRENT_TIMESTAMP,
               last_message_is_bot = 0,
               dead_ping_count = 0,
               last_dead_ping_at = NULL""",
        (channel_id, guild_id, user_id),
    )
    await db.commit()


async def record_bot_activity(guild_id: int | None, channel_id: int) -> None:
    if guild_id is None:
        return
    db = await get_db()
    await db.execute(
        """INSERT INTO channel_activity
           (channel_id, guild_id, last_message_is_bot, last_channel_message_at)
           VALUES (?, ?, 1, CURRENT_TIMESTAMP)
           ON CONFLICT(channel_id) DO UPDATE SET
           last_message_is_bot=1, last_channel_message_at=CURRENT_TIMESTAMP""",
        (channel_id, guild_id),
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
                       last_dead_ping_at, dead_ping_count, last_message_is_bot,
                       (julianday('now') - julianday(last_message_at)) * 86400.0 AS human_idle_seconds,
                       (julianday('now') - julianday(COALESCE(last_channel_message_at, last_message_at))) * 86400.0 AS idle_seconds,
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
                      last_dead_ping_at, dead_ping_count, last_message_is_bot,
                      (julianday('now') - julianday(last_message_at)) * 86400.0 AS human_idle_seconds,
                      (julianday('now') - julianday(COALESCE(last_channel_message_at, last_message_at))) * 86400.0 AS idle_seconds,
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
           (channel_id, last_dead_ping_at, dead_ping_count, last_message_is_bot, last_channel_message_at)
           VALUES (?, CURRENT_TIMESTAMP, 1, 1, CURRENT_TIMESTAMP)
           ON CONFLICT(channel_id) DO UPDATE SET
               last_dead_ping_at = CURRENT_TIMESTAMP,
               dead_ping_count = channel_activity.dead_ping_count + 1,
               last_message_is_bot = 1,
               last_channel_message_at = CURRENT_TIMESTAMP""",
        (channel_id,),
    )
    await db.commit()


async def get_state(key: str) -> str | None:
    """Fetch a small persistent bot state value."""
    db = await get_db()
    cursor = await db.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else None








async def claim_reaction_gif(gif_key: str) -> bool:
    db = await get_db()
    cursor = await db.execute("INSERT OR IGNORE INTO reaction_gif_history (gif_key) VALUES (?)", (gif_key,))
    await db.commit()
    return cursor.rowcount == 1


async def meme_is_known(image_url: str, post_url: str | None) -> bool:
    db = await get_db()
    cursor = await db.execute(
        """SELECT 1 FROM meme_history WHERE image_url=? OR post_url=?
           UNION ALL SELECT 1 FROM bot_state
           WHERE key LIKE 'meme_drop_last:%' AND value=? LIMIT 1""",
        (image_url, post_url, image_url),
    )
    return await cursor.fetchone() is not None


async def claim_meme(content_hash: str, image_url: str, post_url: str | None) -> bool:
    """Atomically reserve a new image across all meme delivery paths."""
    db = await get_db()
    cursor = await db.execute(
        "INSERT OR IGNORE INTO meme_history (content_hash, image_url, post_url) VALUES (?, ?, ?)",
        (content_hash, image_url, post_url),
    )
    await db.commit()
    return cursor.rowcount == 1


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


async def claim_activity(item_key: str) -> bool:
    connection = await get_db()
    cursor = await connection.execute("INSERT OR IGNORE INTO activity_history (item_key) VALUES (?)", (item_key,))
    await connection.commit()
    return cursor.rowcount == 1


async def add_activity_score(guild_id: int, user_id: int, game: str, points: int = 1) -> None:
    connection = await get_db()
    await connection.execute(
        """INSERT INTO activity_scores VALUES (?, ?, ?, ?)
           ON CONFLICT(guild_id, user_id, game) DO UPDATE SET score=score+excluded.score""",
        (guild_id, user_id, game, points),
    )
    await connection.commit()


async def activity_leaderboard(guild_id: int) -> list[dict]:
    connection = await get_db()
    cursor = await connection.execute(
        """SELECT user_id, SUM(score) AS score FROM activity_scores
           WHERE guild_id=? GROUP BY user_id ORDER BY score DESC, user_id LIMIT 10""", (guild_id,)
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_member_preferences(guild_id: int, user_id: int) -> dict[str, bool]:
    connection = await get_db()
    cursor = await connection.execute("SELECT roasting, media, pings, avatar_edits FROM member_preferences WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = await cursor.fetchone()
    return {key: bool(row[key]) if row else True for key in ("roasting", "media", "pings", "avatar_edits")}


async def set_member_preferences(guild_id: int, user_id: int, updates: dict[str, bool]) -> None:
    connection = await get_db()
    # Specify the default explicitly: older databases may still declare DEFAULT 0.
    await connection.execute("INSERT OR IGNORE INTO member_preferences (guild_id,user_id,avatar_edits) VALUES (?,?,1)", (guild_id, user_id))
    for key, value in updates.items():
        if key in {"roasting", "media", "pings", "avatar_edits"} and isinstance(value, bool):
            await connection.execute(f"UPDATE member_preferences SET {key}=? WHERE guild_id=? AND user_id=?", (int(value), guild_id, user_id))
    await connection.commit()


async def get_dead_chat_opt_outs(guild_id: int) -> set[int]:
    connection = await get_db()
    cursor = await connection.execute(
        """SELECT user_id FROM member_preferences
           WHERE guild_id=? AND (pings=0 OR roasting=0)""", (guild_id,)
    )
    return {row['user_id'] for row in await cursor.fetchall()}


async def get_recent_messages(
    channel_id: int, limit: int | None = None
) -> list[dict]:
    """Fetch the most recent messages for a channel, oldest-first."""
    limit = limit or config.CONTEXT_WINDOW_SIZE
    db = await get_db()
    cursor = await db.execute(
        """SELECT author_id, author_name, is_bot, content, created_at
           FROM messages
           WHERE channel_id = ?
           ORDER BY id DESC
           LIMIT ?""",
        (channel_id, limit),
    )
    rows = await cursor.fetchall()
    return [
        {
            "author_id": row["author_id"],
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
           ORDER BY id ASC
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








async def update_member_profile(guild_id: int, user_id: int, display_name: str, username: str) -> None:
    db = await get_db()
    await db.execute(
        """INSERT INTO member_profiles VALUES (?, ?, ?, ?)
           ON CONFLICT(guild_id, user_id) DO UPDATE SET
           display_name=excluded.display_name, username=excluded.username""",
        (guild_id, user_id, display_name, username),
    )
    await db.commit()


async def save_member_fact(guild_id: int, user_id: int, fact_key: str, content: str, evidence: str, source: str = "self_report") -> None:
    db = await get_db()
    # An owner-curated note cannot be overwritten by automatic extraction.
    await db.execute(
        """INSERT INTO member_facts (guild_id, user_id, fact_key, content, evidence, source)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(guild_id, user_id, fact_key) DO UPDATE SET
           content=excluded.content, evidence=excluded.evidence, source=excluded.source,
           updated_at=CURRENT_TIMESTAMP
           WHERE member_facts.source != 'owner_note' OR excluded.source = 'owner_note'""",
        (guild_id, user_id, fact_key, content[:400], evidence[:1000], source),
    )
    await db.commit()


async def get_member_facts(guild_id: int, user_id: int, limit: int = 12) -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        """SELECT fact_key, content, evidence, source FROM member_facts
           WHERE guild_id=? AND user_id=? ORDER BY updated_at DESC LIMIT ?""",
        (guild_id, user_id, limit),
    )
    return [dict(row) for row in await cursor.fetchall()]


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
