"""
database/manager.py
───────────────────
Capa de abstracción de base de datos.

Bases de datos soportadas:
  • SQLite    (por defecto, sin configuración extra)
  • PostgreSQL
  • MariaDB / MySQL

Variables de entorno (.env):
  DB_TYPE=sqlite | postgresql | mariadb
  DATABASE_URL=  (requerido si DB_TYPE != sqlite)
    PostgreSQL → postgresql://user:pass@host:5432/dbname
  Alternativa por variables separadas (MariaDB):
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
"""

import os
import json
import logging
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Database")

VALID_CONFIG_COLUMNS = frozenset({
    "mute_role_id", "log_channel_id",
    "warn_mute_threshold", "warn_kick_threshold", "warn_ban_threshold",
    "warn_mute_enabled", "warn_kick_enabled", "warn_ban_enabled",
    "warn_mute_duration", "warn_embed_config",
    "staff_role_id",
})

VALID_USER_COLUMNS = frozenset({
    "warns", "mute_start", "mute_duration",
})

VALID_CHANNEL_CONFIG_COLUMNS = frozenset({
    "guild_id", "locked", "media_only", "media_config", "auto_react", "slowmode",
})

VALID_SERVER_CONFIG_COLUMNS = frozenset({
    "staff_role_id", "mod_role_id", "modlog_channel", "serverlog_channel", "log_events",
    "embed_role_id", "channels_role_id", "users_role_id",
    "modlog_enabled", "serverlog_enabled",
})

VALID_AI_CONFIG_COLUMNS = frozenset({
    "guild_id", "ai_channel_id", "ai_role_id", "ai_model", 
    "ai_system_prompt", "ai_limit_requests", "ai_limit_hours",
})


# ── Schema por tipo de base de datos ─────────────────────────────────────────

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id     INTEGER PRIMARY KEY,
    mute_role_id INTEGER,
    log_channel_id INTEGER,
    warn_mute_threshold INTEGER DEFAULT 3,
    warn_kick_threshold INTEGER DEFAULT 5,
    warn_ban_threshold  INTEGER DEFAULT 7,
    warn_mute_enabled   INTEGER DEFAULT 1,
    warn_kick_enabled   INTEGER DEFAULT 0,
    warn_ban_enabled    INTEGER DEFAULT 0,
    warn_mute_duration  INTEGER DEFAULT 3600,
    warn_embed_config   TEXT,
    staff_role_id INTEGER
);

CREATE TABLE IF NOT EXISTS user_records (
    user_id      INTEGER NOT NULL,
    guild_id     INTEGER NOT NULL,
    warns        INTEGER DEFAULT 0,
    mute_start   TEXT,
    mute_duration INTEGER,
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS mod_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    target_id    INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    action_type  TEXT    NOT NULL,
    reason       TEXT    DEFAULT 'Sin razón especificada',
    extra_data   TEXT,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_config (
    channel_id   INTEGER PRIMARY KEY,
    guild_id     INTEGER NOT NULL,
    locked       INTEGER DEFAULT 0,
    media_only   INTEGER DEFAULT 0,
    media_config TEXT,
    auto_react   TEXT,
    slowmode     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS server_config (
    guild_id          INTEGER PRIMARY KEY,
    staff_role_id     INTEGER,
    modlog_channel    INTEGER,
    serverlog_channel INTEGER,
    log_events        TEXT,
    embed_role_id     INTEGER,
    channels_role_id  INTEGER,
    users_role_id     INTEGER,
    modlog_enabled    INTEGER DEFAULT 1,
    serverlog_enabled INTEGER DEFAULT 1,
    mod_role_id       INTEGER
);

CREATE TABLE IF NOT EXISTS ai_config (
    guild_id          INTEGER PRIMARY KEY,
    ai_channel_id     INTEGER,
    ai_role_id        INTEGER,
    ai_model          TEXT DEFAULT 'gemini-2.5-flash',
    ai_system_prompt  TEXT,
    ai_limit_requests INTEGER DEFAULT 50,
    ai_limit_hours    INTEGER DEFAULT 12
);

CREATE TABLE IF NOT EXISTS appeals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    action_type  TEXT NOT NULL,
    reason       TEXT,
    appeal_text  TEXT,
    status       TEXT DEFAULT 'PENDING',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_embeds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    creator_id   INTEGER NOT NULL,
    name         TEXT,
    embed_data   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ur_guild   ON user_records(guild_id);
CREATE INDEX IF NOT EXISTS idx_ma_target  ON mod_actions(target_id, guild_id);
CREATE INDEX IF NOT EXISTS idx_ma_time    ON mod_actions(guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_mute_active ON user_records(mute_start)
    WHERE mute_start IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cc_guild   ON channel_config(guild_id);
CREATE INDEX IF NOT EXISTS idx_se_guild   ON saved_embeds(guild_id);
"""

_SCHEMA_POSTGRESQL = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id     BIGINT PRIMARY KEY,
    mute_role_id BIGINT,
    log_channel_id BIGINT,
    warn_mute_threshold INTEGER DEFAULT 3,
    warn_kick_threshold INTEGER DEFAULT 5,
    warn_ban_threshold  INTEGER DEFAULT 7,
    warn_mute_enabled   SMALLINT DEFAULT 1,
    warn_kick_enabled   SMALLINT DEFAULT 0,
    warn_ban_enabled    SMALLINT DEFAULT 0,
    warn_mute_duration  INTEGER DEFAULT 3600,
    warn_embed_config   TEXT,
    staff_role_id BIGINT
);

CREATE TABLE IF NOT EXISTS user_records (
    user_id      BIGINT NOT NULL,
    guild_id     BIGINT NOT NULL,
    warns        INTEGER DEFAULT 0,
    mute_start   TEXT,
    mute_duration INTEGER,
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS mod_actions (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    target_id    BIGINT NOT NULL,
    moderator_id BIGINT NOT NULL,
    action_type  TEXT   NOT NULL,
    reason       TEXT   DEFAULT 'Sin razón especificada',
    extra_data   TEXT,
    created_at   TEXT   NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_config (
    channel_id   BIGINT PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    locked       SMALLINT DEFAULT 0,
    media_only   SMALLINT DEFAULT 0,
    media_config TEXT,
    auto_react   TEXT,
    slowmode     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS server_config (
    guild_id          BIGINT PRIMARY KEY,
    staff_role_id     BIGINT,
    modlog_channel    BIGINT,
    serverlog_channel BIGINT,
    log_events        TEXT,
    embed_role_id     BIGINT,
    channels_role_id  BIGINT,
    users_role_id     BIGINT,
    modlog_enabled    SMALLINT DEFAULT 1,
    serverlog_enabled SMALLINT DEFAULT 1,
    mod_role_id       BIGINT
);

CREATE TABLE IF NOT EXISTS ai_config (
    guild_id          BIGINT PRIMARY KEY,
    ai_channel_id     BIGINT,
    ai_role_id        BIGINT,
    ai_model          TEXT DEFAULT 'gemini-2.5-flash',
    ai_system_prompt  TEXT,
    ai_limit_requests INTEGER DEFAULT 50,
    ai_limit_hours    INTEGER DEFAULT 12
);

CREATE TABLE IF NOT EXISTS appeals (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    user_id      BIGINT NOT NULL,
    action_type  TEXT NOT NULL,
    reason       TEXT,
    appeal_text  TEXT,
    status       TEXT DEFAULT 'PENDING',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_embeds (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    creator_id   BIGINT NOT NULL,
    name         TEXT,
    embed_data   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ur_guild  ON user_records(guild_id);
CREATE INDEX IF NOT EXISTS idx_ma_target ON mod_actions(target_id, guild_id);
CREATE INDEX IF NOT EXISTS idx_ma_time   ON mod_actions(guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cc_guild  ON channel_config(guild_id);
CREATE INDEX IF NOT EXISTS idx_se_guild  ON saved_embeds(guild_id);
"""

_SCHEMA_MARIADB = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id     BIGINT PRIMARY KEY,
    mute_role_id BIGINT,
    log_channel_id BIGINT,
    warn_mute_threshold INT DEFAULT 3,
    warn_kick_threshold INT DEFAULT 5,
    warn_ban_threshold  INT DEFAULT 7,
    warn_mute_enabled   TINYINT DEFAULT 1,
    warn_kick_enabled   TINYINT DEFAULT 0,
    warn_ban_enabled    TINYINT DEFAULT 0,
    warn_mute_duration  INT DEFAULT 3600,
    warn_embed_config   TEXT,
    staff_role_id BIGINT
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_records (
    user_id      BIGINT NOT NULL,
    guild_id     BIGINT NOT NULL,
    warns        INT DEFAULT 0,
    mute_start   VARCHAR(50),
    mute_duration INT,
    PRIMARY KEY (user_id, guild_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS mod_actions (
    id           BIGINT NOT NULL AUTO_INCREMENT,
    guild_id     BIGINT NOT NULL,
    target_id    BIGINT NOT NULL,
    moderator_id BIGINT NOT NULL,
    action_type  VARCHAR(30) NOT NULL,
    reason       TEXT,
    extra_data   TEXT,
    created_at   VARCHAR(50) NOT NULL,
    PRIMARY KEY (id),
    INDEX idx_ma_target (target_id, guild_id),
    INDEX idx_ma_time   (guild_id, created_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS channel_config (
    channel_id   BIGINT PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    locked       TINYINT DEFAULT 0,
    media_only   TINYINT DEFAULT 0,
    media_config TEXT,
    auto_react   TEXT,
    slowmode     INT DEFAULT 0,
    INDEX idx_cc_guild (guild_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS server_config (
    guild_id          BIGINT PRIMARY KEY,
    staff_role_id     BIGINT,
    modlog_channel    BIGINT,
    serverlog_channel BIGINT,
    log_events        TEXT,
    embed_role_id     BIGINT,
    channels_role_id  BIGINT,
    users_role_id     BIGINT,
    modlog_enabled    TINYINT DEFAULT 1,
    serverlog_enabled TINYINT DEFAULT 1,
    mod_role_id       BIGINT
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ai_config (
    guild_id          BIGINT PRIMARY KEY,
    ai_channel_id     BIGINT,
    ai_role_id        BIGINT,
    ai_model          VARCHAR(50) DEFAULT 'gemini-2.5-flash',
    ai_system_prompt  TEXT,
    ai_limit_requests INT DEFAULT 50,
    ai_limit_hours    INT DEFAULT 12
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS appeals (
    id           BIGINT NOT NULL AUTO_INCREMENT,
    guild_id     BIGINT NOT NULL,
    user_id      BIGINT NOT NULL,
    action_type  VARCHAR(30) NOT NULL,
    reason       TEXT,
    appeal_text  TEXT,
    status       VARCHAR(20) DEFAULT 'PENDING',
    created_at   VARCHAR(50) NOT NULL,
    PRIMARY KEY (id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS saved_embeds (
    id           BIGINT NOT NULL AUTO_INCREMENT,
    guild_id     BIGINT NOT NULL,
    creator_id   BIGINT NOT NULL,
    name         TEXT,
    embed_data   TEXT NOT NULL,
    created_at   VARCHAR(50) NOT NULL,
    PRIMARY KEY (id),
    INDEX idx_se_guild (guild_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
"""


class DatabaseManager:
    """Gestor de base de datos con soporte multi-proveedor."""

    DEFAULT_CONFIG: Dict[str, Any] = {
        "guild_id": None,
        "mute_role_id": None,
        "log_channel_id": None,
        "warn_mute_threshold": 3,
        "warn_kick_threshold": 5,
        "warn_ban_threshold": 7,
        "warn_mute_enabled": 1,
        "warn_kick_enabled": 0,
        "warn_ban_enabled": 0,
        "warn_mute_duration": 3600,
        "warn_embed_config": None,
        "staff_role_id": None,
    }

    DEFAULT_SERVER_CONFIG: Dict[str, Any] = {
        "guild_id": None,
        "staff_role_id": None,
        "modlog_channel": None,
        "serverlog_channel": None,
        "log_events": None,
        "embed_role_id": None,
        "channels_role_id": None,
        "users_role_id": None,
        "modlog_enabled": 1,
        "serverlog_enabled": 1,
    }

    DEFAULT_CHANNEL_CONFIG: Dict[str, Any] = {
        "channel_id": None,
        "guild_id": None,
        "locked": 0,
        "media_only": 0,
        "media_config": None,
        "auto_react": None,
        "slowmode": 0,
    }

    def __init__(self):
        self.db_type = os.getenv("DB_TYPE", "sqlite").lower()

        if self.db_type == "sqlite":
            data_dir = Path(__file__).parent.parent / "data"
            data_dir.mkdir(exist_ok=True)
            self.db_path = str(data_dir / "bot.db")
            logger.info(f"Base de datos: SQLite → {self.db_path}")

        elif self.db_type in ("postgresql", "mariadb"):
            self.connection_url = os.getenv("DATABASE_URL")
            if not self.connection_url and self.db_type == "postgresql":
                raise ValueError(
                    "DB_TYPE='postgresql' requiere DATABASE_URL en .env\n"
                    "Ejemplo: postgresql://usuario:contraseña@localhost:5432/bot_db"
                )
            logger.info(f"Base de datos: {self.db_type.upper()}")
        else:
            raise ValueError(
                f"DB_TYPE inválido: '{self.db_type}'. "
                "Usa 'sqlite', 'postgresql' o 'mariadb'."
            )

        self._init_schema()

    # ── Utilidades internas ───────────────────────────────────────────────────

    @property
    def ph(self) -> str:
        """Placeholder de parámetro: '?' para SQLite, '%s' para los demás."""
        return "?" if self.db_type == "sqlite" else "%s"

    def _adapt(self, query: str) -> str:
        """Adapta los placeholders '?' según el tipo de DB."""
        if self.db_type == "sqlite":
            return query
        return query.replace("?", "%s")

    # ── Conexión ──────────────────────────────────────────────────────────────

    @contextmanager
    def _conn(self):
        """
        Context manager de conexión.
        Commit automático al salir; rollback en caso de excepción.
        """
        connection = None
        try:
            if self.db_type == "sqlite":
                import sqlite3
                connection = sqlite3.connect(self.db_path)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")

            elif self.db_type == "postgresql":
                import psycopg2
                from psycopg2.extras import RealDictCursor
                connection = psycopg2.connect(self.connection_url)
                connection.cursor_factory = RealDictCursor

            elif self.db_type == "mariadb":
                import pymysql
                import pymysql.cursors
                connection = pymysql.connect(
                    host=os.getenv("DB_HOST", "localhost"),
                    port=int(os.getenv("DB_PORT", "3306")),
                    user=os.getenv("DB_USER"),
                    password=os.getenv("DB_PASSWORD"),
                    database=os.getenv("DB_NAME"),
                    charset="utf8mb4",
                    cursorclass=pymysql.cursors.DictCursor,
                )

            yield connection
            connection.commit()

        except Exception as exc:
            if connection:
                connection.rollback()
            logger.error(f"Error de base de datos: {exc}")
            raise
        finally:
            if connection:
                connection.close()

    # ── Helpers de ejecución ──────────────────────────────────────────────────

    def _execute(self, query: str, params: tuple = ()) -> None:
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor
                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(self._adapt(query), params)

    def _fetchone(self, query: str, params: tuple = ()) -> Optional[Dict]:
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor
                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(self._adapt(query), params)
            row = cur.fetchone()
            return dict(row) if row else None

    def _fetchall(self, query: str, params: tuple = ()) -> List[Dict]:
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor
                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(self._adapt(query), params)
            return [dict(r) for r in cur.fetchall()]

    def _executemany(self, queries_params: List[tuple]) -> None:
        """Ejecuta múltiples queries en una única transacción."""
        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor
                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            for query, params in queries_params:
                cur.execute(self._adapt(query), params)

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self):
        schema_map = {
            "sqlite": _SCHEMA_SQLITE,
            "postgresql": _SCHEMA_POSTGRESQL,
            "mariadb": _SCHEMA_MARIADB,
        }
        schema = schema_map[self.db_type]

        with self._conn() as conn:
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictCursor
                cur = conn.cursor(cursor_factory=RealDictCursor)
            else:
                cur = conn.cursor()
            # SQLite soporta executescript; los demás ejecutan statement a statement
            if self.db_type == "sqlite":
                conn.executescript(schema)
            else:
                for stmt in schema.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        cur.execute(stmt)

        logger.info("Schema de base de datos inicializado correctamente.")

    # ── Guild Config ──────────────────────────────────────────────────────────

    def get_config(self, guild_id: int) -> Dict:
        """Retorna la config del servidor, con valores por defecto si no existe."""
        row = self._fetchone(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        )
        result = dict(self.DEFAULT_CONFIG)
        result["guild_id"] = guild_id
        if row:
            result.update(row)
        return result

    def set_config(self, guild_id: int, **kwargs) -> None:
        """Crea o actualiza campos de configuración de un servidor."""
        invalid = set(kwargs) - VALID_CONFIG_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas: {invalid}")

        ops = []
        # Asegurar que el registro exista
        if self.db_type == "sqlite":
            ops.append((
                "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)",
                (guild_id,),
            ))
        elif self.db_type == "postgresql":
            ops.append((
                "INSERT INTO guild_config (guild_id) VALUES (?) "
                "ON CONFLICT (guild_id) DO NOTHING",
                (guild_id,),
            ))
        else:  # mariadb
            ops.append((
                "INSERT IGNORE INTO guild_config (guild_id) VALUES (?)",
                (guild_id,),
            ))

        for col, val in kwargs.items():
            ops.append((
                f"UPDATE guild_config SET {col} = ? WHERE guild_id = ?",
                (val, guild_id),
            ))

        self._executemany(ops)

    # ── User Records ──────────────────────────────────────────────────────────

    def get_user(self, user_id: int, guild_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM user_records WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )
        if row:
            return row
        return {
            "user_id": user_id, "guild_id": guild_id,
            "warns": 0, "mute_start": None, "mute_duration": None,
        }

    def _upsert_user(self, user_id: int, guild_id: int, **kwargs) -> None:
        invalid = set(kwargs) - VALID_USER_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas: {invalid}")

        ops = []
        if self.db_type == "sqlite":
            ops.append((
                "INSERT OR IGNORE INTO user_records (user_id, guild_id) VALUES (?, ?)",
                (user_id, guild_id),
            ))
        elif self.db_type == "postgresql":
            ops.append((
                "INSERT INTO user_records (user_id, guild_id) VALUES (?, ?) "
                "ON CONFLICT (user_id, guild_id) DO NOTHING",
                (user_id, guild_id),
            ))
        else:
            ops.append((
                "INSERT IGNORE INTO user_records (user_id, guild_id) VALUES (?, ?)",
                (user_id, guild_id),
            ))

        for col, val in kwargs.items():
            ops.append((
                f"UPDATE user_records SET {col} = ? WHERE user_id = ? AND guild_id = ?",
                (val, user_id, guild_id),
            ))

        self._executemany(ops)

    def add_warn(self, user_id: int, guild_id: int) -> int:
        """Incrementa el contador de warns y retorna el nuevo total."""
        current = self.get_user(user_id, guild_id)
        new_count = current["warns"] + 1
        self._upsert_user(user_id, guild_id, warns=new_count)
        return new_count

    def clear_warns(self, user_id: int, guild_id: int) -> None:
        self._upsert_user(user_id, guild_id, warns=0)

    def set_mute(self, user_id: int, guild_id: int, duration_secs: Optional[int]) -> None:
        self._upsert_user(
            user_id, guild_id,
            mute_start=datetime.now(timezone.utc).isoformat(),
            mute_duration=duration_secs,
        )

    def clear_mute(self, user_id: int, guild_id: int) -> None:
        self._upsert_user(user_id, guild_id, mute_start=None, mute_duration=None)

    def get_active_mutes(self) -> List[Dict]:
        """Retorna todos los registros con mutes activos y duración definida."""
        return self._fetchall(
            "SELECT * FROM user_records "
            "WHERE mute_start IS NOT NULL AND mute_duration IS NOT NULL"
        )

    # ── Mod Actions ───────────────────────────────────────────────────────────

    def log_action(
        self,
        guild_id: int,
        target_id: int,
        moderator_id: int,
        action_type: str,
        reason: str = "Sin razón especificada",
        extra: Optional[Dict] = None,
    ) -> None:
        self._execute(
            "INSERT INTO mod_actions "
            "(guild_id, target_id, moderator_id, action_type, reason, extra_data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id, target_id, moderator_id,
                action_type, reason,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def get_user_history(
        self, user_id: int, guild_id: int, limit: int = 10
    ) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM mod_actions "
            "WHERE target_id = ? AND guild_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, guild_id, limit),
        )

    def get_user_action_summary(self, user_id: int, guild_id: int) -> Dict[str, int]:
        """Cuenta warns, kicks, bans, mutes de un usuario para /userinfo."""
        rows = self._fetchall(
            "SELECT action_type, COUNT(*) as cnt FROM mod_actions "
            "WHERE target_id = ? AND guild_id = ? "
            "GROUP BY action_type",
            (user_id, guild_id),
        )
        summary = {"WARN": 0, "KICK": 0, "BAN": 0, "MUTE": 0, "UNMUTE": 0}
        for r in rows:
            if r["action_type"] in summary:
                summary[r["action_type"]] = r["cnt"]
        return summary

    # ── Channel Config ────────────────────────────────────────────────────────

    def get_channel_config(self, channel_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM channel_config WHERE channel_id = ?", (channel_id,)
        )
        result = dict(self.DEFAULT_CHANNEL_CONFIG)
        result["channel_id"] = channel_id
        if row:
            result.update(row)
        return result

    def set_channel_config(self, channel_id: int, guild_id: int, **kwargs) -> None:
        invalid = set(kwargs) - VALID_CHANNEL_CONFIG_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas: {invalid}")

        ops = []
        if self.db_type == "sqlite":
            ops.append((
                "INSERT OR IGNORE INTO channel_config (channel_id, guild_id) VALUES (?, ?)",
                (channel_id, guild_id),
            ))
        elif self.db_type == "postgresql":
            ops.append((
                "INSERT INTO channel_config (channel_id, guild_id) VALUES (?, ?) "
                "ON CONFLICT (channel_id) DO NOTHING",
                (channel_id, guild_id),
            ))
        else:
            ops.append((
                "INSERT IGNORE INTO channel_config (channel_id, guild_id) VALUES (?, ?)",
                (channel_id, guild_id),
            ))

        for col, val in kwargs.items():
            ops.append((
                f"UPDATE channel_config SET {col} = ? WHERE channel_id = ?",
                (val, channel_id),
            ))

        self._executemany(ops)

    def delete_channel_config(self, channel_id: int) -> None:
        self._execute("DELETE FROM channel_config WHERE channel_id = ?", (channel_id,))

    def get_all_channel_configs(self, guild_id: int) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM channel_config WHERE guild_id = ?", (guild_id,)
        )

    # ── Server Config ─────────────────────────────────────────────────────────

    def get_server_config(self, guild_id: int) -> Dict:
        row = self._fetchone(
            "SELECT * FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        result = dict(self.DEFAULT_SERVER_CONFIG)
        result["guild_id"] = guild_id
        if row:
            result.update(row)
        return result

    def set_server_config(self, guild_id: int, **kwargs) -> None:
        invalid = set(kwargs) - VALID_SERVER_CONFIG_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas: {invalid}")

        ops = []
        if self.db_type == "sqlite":
            ops.append((
                "INSERT OR IGNORE INTO server_config (guild_id) VALUES (?)",
                (guild_id,),
            ))
        elif self.db_type == "postgresql":
            ops.append((
                "INSERT INTO server_config (guild_id) VALUES (?) "
                "ON CONFLICT (guild_id) DO NOTHING",
                (guild_id,),
            ))
        else:
            ops.append((
                "INSERT IGNORE INTO server_config (guild_id) VALUES (?)",
                (guild_id,),
            ))

        for col, val in kwargs.items():
            ops.append((
                f"UPDATE server_config SET {col} = ? WHERE guild_id = ?",
                (val, guild_id),
            ))

        self._executemany(ops)

    # ── Saved Embeds ──────────────────────────────────────────────────────────

    def save_embed(
        self, guild_id: int, creator_id: int, name: str, embed_data: str
    ) -> None:
        self._execute(
            "INSERT INTO saved_embeds (guild_id, creator_id, name, embed_data, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, creator_id, name, embed_data,
             datetime.now(timezone.utc).isoformat()),
        )

    def get_saved_embeds(self, guild_id: int) -> List[Dict]:
        return self._fetchall(
            "SELECT * FROM saved_embeds WHERE guild_id = ? ORDER BY created_at DESC",
            (guild_id,),
        )

    def get_saved_embed_by_name(self, guild_id: int, name: str) -> Optional[Dict]:
        return self._fetchone(
            "SELECT * FROM saved_embeds WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )

    def delete_saved_embed(self, embed_id: int) -> None:
        self._execute("DELETE FROM saved_embeds WHERE id = ?", (embed_id,))

    # ── AI Config ─────────────────────────────────────────────────────────────

    DEFAULT_AI_CONFIG: Dict[str, Any] = {
        "guild_id": None,
        "ai_channel_id": None,
        "ai_role_id": None,
        "ai_model": "gemini-2.5-flash",
        "ai_system_prompt": None,
        "ai_limit_requests": 50,
        "ai_limit_hours": 12,
    }

    def get_ai_config(self, guild_id: int) -> Dict:
        row = self._fetchone("SELECT * FROM ai_config WHERE guild_id = ?", (guild_id,))
        result = dict(self.DEFAULT_AI_CONFIG)
        result["guild_id"] = guild_id
        if row:
            result.update(row)
        return result

    def set_ai_config(self, guild_id: int, **kwargs) -> None:
        invalid = set(kwargs) - VALID_AI_CONFIG_COLUMNS
        if invalid:
            raise ValueError(f"Columnas inválidas en ai_config: {invalid}")

        ops = []
        if self.db_type == "sqlite":
            ops.append((
                "INSERT OR IGNORE INTO ai_config (guild_id) VALUES (?)",
                (guild_id,),
            ))
        elif self.db_type == "postgresql":
            ops.append((
                "INSERT INTO ai_config (guild_id) VALUES (?) ON CONFLICT (guild_id) DO NOTHING",
                (guild_id,),
            ))
        else:
            ops.append((
                "INSERT IGNORE INTO ai_config (guild_id) VALUES (?)",
                (guild_id,),
            ))

        for col, val in kwargs.items():
            ops.append((
                f"UPDATE ai_config SET {col} = ? WHERE guild_id = ?",
                (val, guild_id),
            ))

        self._executemany(ops)

    # ── Appeals ───────────────────────────────────────────────────────────────

    def create_appeal(self, guild_id: int, user_id: int, action_type: str, reason: str, appeal_text: str) -> int:
        """Crea una nueva apelación y retorna su ID (aproximado o ejecutado)."""
        ops = [(
            "INSERT INTO appeals (guild_id, user_id, action_type, reason, appeal_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, action_type, reason, appeal_text, datetime.now(timezone.utc).isoformat())
        )]
        self._executemany(ops)
        # Buscar el ID más reciente
        row = self._fetchone(
            "SELECT id FROM appeals WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id, user_id)
        )
        return row["id"] if row else 0

    def get_appeal(self, appeal_id: int) -> Optional[Dict]:
        return self._fetchone("SELECT * FROM appeals WHERE id = ?", (appeal_id,))

    def update_appeal_status(self, appeal_id: int, status: str) -> None:
        self._execute("UPDATE appeals SET status = ? WHERE id = ?", (status, appeal_id))
