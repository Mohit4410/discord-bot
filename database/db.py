import aiosqlite
import os
import time
from typing import Optional, List, Dict, Any

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot_database.sqlite")

class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    async def get_connection(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        return conn

    async def init_tables(self):
        """Initialize all sqlite schema tables."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA foreign_keys = ON")
            
            # Guild settings
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                prefix TEXT DEFAULT NULL,
                welcome_channel_id INTEGER,
                welcome_message TEXT DEFAULT 'Welcome {user_mention} to **{server}**! We are now {member_count} members.',
                welcome_enabled INTEGER DEFAULT 0,
                leave_channel_id INTEGER,
                leave_message TEXT DEFAULT '**{user}** has left the server.',
                leave_enabled INTEGER DEFAULT 0,
                starboard_channel_id INTEGER,
                starboard_threshold INTEGER DEFAULT 3,
                starboard_enabled INTEGER DEFAULT 0,
                tempvoice_category_id INTEGER,
                tempvoice_creator_id INTEGER,
                tempvoice_enabled INTEGER DEFAULT 0,
                leveling_enabled INTEGER DEFAULT 1,
                level_channel_id INTEGER DEFAULT 0,
                restore_role_id INTEGER DEFAULT NULL
            )
            """)

            # Logging channels
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS logging_config (
                guild_id INTEGER,
                log_type TEXT,
                channel_id INTEGER,
                PRIMARY KEY (guild_id, log_type)
            )
            """)

            # Auto-moderation settings
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS automod_config (
                guild_id INTEGER PRIMARY KEY,
                anti_invite INTEGER DEFAULT 0,
                anti_link INTEGER DEFAULT 0,
                anti_spam INTEGER DEFAULT 0,
                anti_caps INTEGER DEFAULT 0,
                caps_threshold INTEGER DEFAULT 70,
                bad_words TEXT DEFAULT '',
                punishment TEXT DEFAULT 'delete'
            )
            """)

            # Moderation warnings
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # Leveling users
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS levels (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 0,
                messages_count INTEGER DEFAULT 0,
                last_xp_time REAL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """)

            # Level rewards
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS level_rewards (
                guild_id INTEGER NOT NULL,
                level INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, level)
            )
            """)

            # Auto-roles (given on join)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS autoroles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            )
            """)

            # Button / Reaction roles
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS button_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                emoji TEXT,
                label TEXT,
                button_style TEXT DEFAULT 'secondary'
            )
            """)

            # Custom commands / auto responders
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS custom_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                trigger TEXT NOT NULL,
                response TEXT NOT NULL,
                match_type TEXT DEFAULT 'exact',
                UNIQUE(guild_id, trigger)
            )
            """)

            # Starboard tracked messages
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS starboard_messages (
                original_message_id INTEGER PRIMARY KEY,
                starboard_message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                stars INTEGER DEFAULT 0
            )
            """)

            # Temp voice channels
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS temp_voice_channels (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # Reminders
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                reminder_text TEXT NOT NULL,
                remind_at REAL NOT NULL
            )
            """)

            # Purged & Deleted messages (for /restore command)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS purged_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                author_name TEXT NOT NULL,
                author_avatar_url TEXT NOT NULL,
                content TEXT,
                attachments TEXT,
                media_files TEXT DEFAULT '',
                embeds TEXT DEFAULT '',
                is_webhook INTEGER DEFAULT 0,
                timestamp REAL NOT NULL
            )
            """)

            # Ensure media_files, embeds, is_webhook columns exist for existing tables
            cursor = await conn.execute("PRAGMA table_info(purged_messages)")
            columns = [row[1] for row in await cursor.fetchall()]
            if "media_files" not in columns:
                await conn.execute("ALTER TABLE purged_messages ADD COLUMN media_files TEXT DEFAULT ''")
            if "embeds" not in columns:
                await conn.execute("ALTER TABLE purged_messages ADD COLUMN embeds TEXT DEFAULT ''")
            if "is_webhook" not in columns:
                await conn.execute("ALTER TABLE purged_messages ADD COLUMN is_webhook INTEGER DEFAULT 0")

            # Table for backing up channel webhooks
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS backed_up_webhooks (
                webhook_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                avatar_url TEXT DEFAULT '',
                timestamp REAL NOT NULL
            )
            """)

            # Ensure restore_role_id exists in guild_settings
            cursor = await conn.execute("PRAGMA table_info(guild_settings)")
            g_cols = [row[1] for row in await cursor.fetchall()]
            if "restore_role_id" not in g_cols:
                await conn.execute("ALTER TABLE guild_settings ADD COLUMN restore_role_id INTEGER DEFAULT NULL")

            await conn.commit()

    # Guild Settings Helpers
    async def get_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
            row = await cursor.fetchone()
            if not row:
                await conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
                await conn.commit()
                cursor = await conn.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
                row = await cursor.fetchone()
            return dict(row) if row else {}

    async def update_guild_setting(self, guild_id: int, key: str, value: Any):
        async with aiosqlite.connect(self.db_path) as conn:
            # Ensure row exists
            await conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
            await conn.execute(f"UPDATE guild_settings SET {key} = ? WHERE guild_id = ?", (value, guild_id))
            await conn.commit()

    async def get_restore_role_id(self, guild_id: int) -> Optional[int]:
        settings = await self.get_guild_settings(guild_id)
        return settings.get("restore_role_id")

    async def set_restore_role_id(self, guild_id: int, role_id: Optional[int]):
        await self.update_guild_setting(guild_id, "restore_role_id", role_id)

    # Logging Helpers
    async def set_log_channel(self, guild_id: int, log_type: str, channel_id: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
            INSERT INTO logging_config (guild_id, log_type, channel_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, log_type) DO UPDATE SET channel_id = excluded.channel_id
            """, (guild_id, log_type, channel_id))
            await conn.commit()

    async def get_log_channel(self, guild_id: int, log_type: str) -> Optional[int]:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT channel_id FROM logging_config WHERE guild_id = ? AND log_type = ?",
                (guild_id, log_type)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    # Auto-Mod Helpers
    async def get_automod_config(self, guild_id: int) -> Dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM automod_config WHERE guild_id = ?", (guild_id,))
            row = await cursor.fetchone()
            if not row:
                await conn.execute("INSERT OR IGNORE INTO automod_config (guild_id) VALUES (?)", (guild_id,))
                await conn.commit()
                cursor = await conn.execute("SELECT * FROM automod_config WHERE guild_id = ?", (guild_id,))
                row = await cursor.fetchone()
            return dict(row) if row else {}

    async def update_automod_config(self, guild_id: int, key: str, value: Any):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("INSERT OR IGNORE INTO automod_config (guild_id) VALUES (?)", (guild_id,))
            await conn.execute(f"UPDATE automod_config SET {key} = ? WHERE guild_id = ?", (value, guild_id))
            await conn.commit()

    # Moderation Helpers
    async def add_warning(self, guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES (?, ?, ?, ?)",
                (guild_id, user_id, moderator_id, reason)
            )
            await conn.commit()
            return cursor.lastrowid

    async def get_warnings(self, guild_id: int, user_id: int) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY id DESC",
                (guild_id, user_id)
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id)
            )
            await conn.commit()
            return cursor.rowcount

    # Leveling Helpers
    async def get_user_level(self, guild_id: int, user_id: int) -> Dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM levels WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            row = await cursor.fetchone()
            if not row:
                return {"guild_id": guild_id, "user_id": user_id, "xp": 0, "level": 0, "messages_count": 0, "last_xp_time": 0.0}
            return dict(row)

    async def add_xp(self, guild_id: int, user_id: int, xp_to_add: int, timestamp: float) -> (int, int, bool):
        """Add XP to user and return (new_level, new_xp, leveled_up)"""
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT xp, level, messages_count FROM levels WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            row = await cursor.fetchone()
            
            if row:
                current_xp = row['xp'] + xp_to_add
                current_level = row['level']
                msgs = row['messages_count'] + 1
            else:
                current_xp = xp_to_add
                current_level = 0
                msgs = 1
                
            # XP Formula: Level N requires 100 * (N^1.5) or 5 * (level^2) + 50 * level + 100
            # Level formula: level = int((xp / 100) ** 0.6)
            new_level = int((current_xp / 100) ** 0.6)
            leveled_up = new_level > current_level
            
            await conn.execute("""
            INSERT INTO levels (guild_id, user_id, xp, level, messages_count, last_xp_time)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                xp = excluded.xp,
                level = excluded.level,
                messages_count = excluded.messages_count,
                last_xp_time = excluded.last_xp_time
            """, (guild_id, user_id, current_xp, new_level, msgs, timestamp))
            await conn.commit()
            return new_level, current_xp, leveled_up

    async def get_leaderboard(self, guild_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM levels WHERE guild_id = ? ORDER BY xp DESC LIMIT ?",
                (guild_id, limit)
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_user_rank(self, guild_id: int, user_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("""
            SELECT COUNT(*) + 1 FROM levels
            WHERE guild_id = ? AND xp > (SELECT COALESCE(xp, 0) FROM levels WHERE guild_id = ? AND user_id = ?)
            """, (guild_id, guild_id, user_id))
            row = await cursor.fetchone()
            return row[0] if row else 1

    # Temp Voice Helpers
    async def add_temp_channel(self, channel_id: int, guild_id: int, owner_id: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO temp_voice_channels (channel_id, guild_id, owner_id) VALUES (?, ?, ?)",
                (channel_id, guild_id, owner_id)
            )
            await conn.commit()

    async def remove_temp_channel(self, channel_id: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("DELETE FROM temp_voice_channels WHERE channel_id = ?", (channel_id,))
            await conn.commit()

    async def get_temp_channel_owner(self, channel_id: int) -> Optional[int]:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT owner_id FROM temp_voice_channels WHERE channel_id = ?", (channel_id,))
            row = await cursor.fetchone()
            return row[0] if row else None

    # Purge & Restore Helpers
    async def save_purged_messages(self, messages_data: List[Dict[str, Any]]):
        if not messages_data:
            return
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.executemany("""
            INSERT INTO purged_messages (guild_id, channel_id, author_id, author_name, author_avatar_url, content, attachments, media_files, embeds, is_webhook, timestamp)
            VALUES (:guild_id, :channel_id, :author_id, :author_name, :author_avatar_url, :content, :attachments, :media_files, :embeds, :is_webhook, :timestamp)
            """, messages_data)
            await conn.commit()

    async def get_purged_messages_to_restore(self, channel_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
            SELECT * FROM purged_messages
            WHERE channel_id = ?
            ORDER BY id DESC
            LIMIT ?
            """, (channel_id, limit))
            rows = await cursor.fetchall()
            # Return in chronological order (oldest first so conversation replays properly)
            result = [dict(r) for r in reversed(rows)]
            return result

    async def delete_restored_messages(self, ids: List[int]):
        if not ids:
            return
        async with aiosqlite.connect(self.db_path) as conn:
            placeholders = ",".join("?" for _ in ids)
            await conn.execute(f"DELETE FROM purged_messages WHERE id IN ({placeholders})", ids)
            await conn.commit()

    # Webhook Backup & Restore Helpers
    async def save_webhook_backup(self, webhook_id: int, guild_id: int, channel_id: int, name: str, avatar_url: str):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
            INSERT INTO backed_up_webhooks (webhook_id, guild_id, channel_id, name, avatar_url, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(webhook_id) DO UPDATE SET name = excluded.name, avatar_url = excluded.avatar_url
            """, (webhook_id, guild_id, channel_id, name, avatar_url, time.time() if 'time' in globals() else 0))
            await conn.commit()

    async def get_backed_up_webhooks(self, guild_id: int, channel_id: Optional[int] = None) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            if channel_id:
                cursor = await conn.execute(
                    "SELECT * FROM backed_up_webhooks WHERE guild_id = ? AND channel_id = ? ORDER BY timestamp DESC",
                    (guild_id, channel_id)
                )
            else:
                cursor = await conn.execute(
                    "SELECT * FROM backed_up_webhooks WHERE guild_id = ? ORDER BY timestamp DESC",
                    (guild_id,)
                )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def remove_backed_up_webhook(self, webhook_id: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("DELETE FROM backed_up_webhooks WHERE webhook_id = ?", (webhook_id,))
            await conn.commit()

# Singleton database instance
db = Database()
