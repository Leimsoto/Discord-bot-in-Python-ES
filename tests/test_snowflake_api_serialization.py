import json
import logging
import sqlite3
import threading
import unittest

from api.routes.autoroles import ReactionPanelBody, list_reaction_panels, upsert_reaction_panel
from api.routes.guild import get_ia_config, patch_ia_config
from api.routes.voice_gen import VoiceGenConfigUpdate, get_voice_gen_config, patch_voice_gen_config
from database.manager import DatabaseManager


def make_db():
    db = DatabaseManager.__new__(DatabaseManager)
    db.db_type = "sqlite"
    db._sqlite_lock = threading.Lock()
    db._sqlite_conn = sqlite3.connect(":memory:", check_same_thread=False)
    db._sqlite_conn.row_factory = sqlite3.Row
    db._sqlite_conn.execute("PRAGMA foreign_keys = ON")
    db._sqlite_conn.execute("PRAGMA journal_mode = WAL")
    db_logger = logging.getLogger("Database")
    previous_disabled = db_logger.disabled
    db_logger.disabled = True
    try:
        db._init_schema()
    finally:
        db_logger.disabled = previous_disabled
    return db


class SnowflakeApiSerializationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = make_db()

    async def asyncTearDown(self):
        self.db._sqlite_conn.close()

    async def test_autorole_reaction_ids_are_string_safe(self):
        guild_id = 123
        channel_id = 1497055415400136681
        message_id = 1497055416062971949
        role_id = 1523702726393860373

        result = await upsert_reaction_panel(
            guild_id,
            ReactionPanelBody(
                message_id=str(message_id),
                channel_id=str(channel_id),
                # El frontend envía el JSON crudo para que Python conserve el entero exacto.
                mapping_data='{"👍": %d}' % role_id,
            ),
            self.db,
            {"user_id": 42},
        )

        self.assertEqual(result["message_id"], str(message_id))
        self.assertEqual(result["channel_id"], str(channel_id))
        stored = self.db.get_autorole(message_id)
        self.assertEqual(json.loads(stored["mapping_data"])["👍"], role_id)

        listed = await list_reaction_panels(guild_id, self.db, {"user_id": 42})
        panel = listed["panels"][0]
        self.assertEqual(panel["message_id"], str(message_id))
        self.assertEqual(panel["channel_id"], str(channel_id))
        self.assertEqual(json.loads(panel["mapping_data"])["👍"], str(role_id))

    async def test_voice_gen_config_ids_are_string_safe(self):
        guild_id = 123
        generator_id = 1497055415400136681
        category_id = 1497055415400136702
        panel_id = 1497055416062971949

        result = await patch_voice_gen_config(
            guild_id,
            VoiceGenConfigUpdate(
                generator_channel_id=str(generator_id),
                category_id=str(category_id),
                panel_channel_id=str(panel_id),
            ),
            self.db,
            {"user_id": 42},
        )

        self.assertEqual(result["config"]["generator_channel_id"], str(generator_id))
        self.assertEqual(result["config"]["category_id"], str(category_id))
        self.assertEqual(result["config"]["panel_channel_id"], str(panel_id))

        loaded = await get_voice_gen_config(guild_id, self.db, {"user_id": 42})
        self.assertEqual(loaded["config"]["generator_channel_id"], str(generator_id))

    async def test_ia_config_ids_are_string_safe(self):
        guild_id = 123
        channel_id = 1497055415400136681
        role_id = 1523702726393860373

        result = await patch_ia_config(
            guild_id,
            {"ai_channel_id": str(channel_id), "ai_role_id": str(role_id), "ai_imagine_enabled": 1},
            self.db,
            {"user_id": 42},
        )

        self.assertEqual(result["ai_channel_id"], str(channel_id))
        self.assertEqual(result["ai_role_id"], str(role_id))
        loaded = await get_ia_config(guild_id, self.db, {"user_id": 42})
        self.assertEqual(loaded["ai_channel_id"], str(channel_id))
        self.assertEqual(loaded["ai_role_id"], str(role_id))


if __name__ == "__main__":
    unittest.main()
