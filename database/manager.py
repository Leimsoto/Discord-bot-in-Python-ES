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
})

VALID_USER_COLUMNS = frozenset({
    "warns", "mute_start", "mute_duration",
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
    warn_embed_config   TEXT
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

CREATE INDEX IF NOT EXISTS idx_ur_guild   ON user_records(guild_id);
CREATE INDEX IF NOT EXISTS idx_ma_target  ON mod_actions(target_id, guild_id);
CREATE INDEX IF NOT EXISTS idx_ma_time    ON mod_actions(guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_mute_active ON user_records(mute_start)
    WHERE mute_start IS NOT NULL;
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
    warn_embed_config   TEXT
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

CREATE INDEX IF NOT EXISTS idx_ur_guild  ON user_records(guild_id);
CREATE INDEX IF NOT EXISTS idx_ma_target ON mod_actions(target_id, guild_id);
CREATE INDEX IF NOT EXISTS idx_ma_time   ON mod_actions(guild_id, created_at);
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
    warn_embed_config   TEXT
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
