import logging
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from api.routes.radio import RadioConfigUpdate, patch_radio_config
from cogs.radio import Radio
from database.manager import DatabaseManager


class RadioConfigApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = DatabaseManager.__new__(DatabaseManager)
        self.db.db_type = "sqlite"
        self.db._sqlite_lock = threading.Lock()
        self.db._sqlite_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.db._sqlite_conn.row_factory = sqlite3.Row
        self.db._sqlite_conn.execute("PRAGMA foreign_keys = ON")
        self.db._sqlite_conn.execute("PRAGMA journal_mode = WAL")
        db_logger = logging.getLogger("Database")
        previous_disabled = db_logger.disabled
        db_logger.disabled = True
        try:
            self.db._init_schema()
        finally:
            db_logger.disabled = previous_disabled

    async def asyncTearDown(self):
        self.db._sqlite_conn.close()

    async def test_patch_radio_returns_persisted_config(self):
        body = RadioConfigUpdate(
            enabled=1,
            channel_id="123456789012345678",
            stream_url="https://example.com/radio.mp3",
            station_name="Smoke Radio",
            volume=42,
            auto_reconnect=1,
            pause_on_empty=0,
        )

        result = await patch_radio_config(
            123,
            body,
            SimpleNamespace(),
            self.db,
            None,
            {"user_id": 42},
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["radio_config"]["enabled"], 1)
        self.assertEqual(result["radio_config"]["channel_id"], "123456789012345678")
        self.assertEqual(result["radio_config"]["stream_url"], "https://example.com/radio.mp3")
        self.assertEqual(self.db.get_lofi_config(123)["channel_id"], 123456789012345678)
        self.assertEqual(self.db.get_lofi_config(123)["station_name"], "Smoke Radio")

    async def test_patch_radio_forces_active_stream_restart(self):
        class FakeFuture:
            def result(self, timeout=None):
                self.timeout = timeout
                return None

        class FakeRadioCog:
            def __init__(self):
                self.calls = []

            def restart_guild(self, guild_id):
                self.calls.append((guild_id, "restart"))
                return SimpleNamespace(name="restart_guild_coroutine")

        fake_radio = FakeRadioCog()
        fake_bot = SimpleNamespace(cogs={"Radio": fake_radio}, loop=SimpleNamespace())

        with patch("asyncio.run_coroutine_threadsafe", return_value=FakeFuture()) as run_threadsafe:
            result = await patch_radio_config(
                123,
                RadioConfigUpdate(stream_url="https://example.com/new.mp3", station_name="New Radio"),
                SimpleNamespace(),
                self.db,
                fake_bot,
                {"user_id": 42},
            )

        self.assertEqual(fake_radio.calls, [(123, "restart")])
        self.assertEqual(result["radio_restart"], {"triggered": True, "completed": True})
        run_threadsafe.assert_called_once()

    async def test_radio_channel_id_coercion_accepts_string_snowflakes(self):
        self.assertEqual(Radio._coerce_channel_id("123456789012345678"), 123456789012345678)
        self.assertIsNone(Radio._coerce_channel_id(""))
        self.assertIsNone(Radio._coerce_channel_id("not-a-channel"))

    async def test_voice_status_text_is_short_and_clean(self):
        self.assertEqual(Radio._voice_status_text("  My   Station  "), "🎶 My Station")
        self.assertLessEqual(len(Radio._voice_status_text("x" * 500)), 120)

    async def test_radio_reuses_cached_voice_client_when_guild_voice_client_is_missing(self):
        class FakeDB:
            def get_lofi_config(self, guild_id):
                return {
                    "enabled": 1,
                    "channel_id": 1497055415400136701,
                    "stream_url": "https://example.com/radio.mp3",
                    "station_name": "Cached VC Radio",
                    "volume": 100,
                }

        class FakeChannel:
            is_voice = True

            def __init__(self, channel_id, guild):
                self.id = channel_id
                self.guild = guild
                self.name = "Radio"
                self.connect_calls = 0

            async def connect(self, reconnect=True):
                self.connect_calls += 1
                raise AssertionError("No debe llamar connect() si ya existe voice client en bot.voice_clients")

        class FakeVoiceClient:
            def __init__(self, guild, channel):
                self.guild = guild
                self.channel = channel
                self.moved_to = []

            def is_connected(self):
                return True

            def is_playing(self):
                return False

            async def move_to(self, channel):
                self.moved_to.append(channel.id)
                self.channel = channel

        class FakeGuild:
            def __init__(self):
                self.id = 123
                self.name = "Guild"
                self.voice_client = None
                self.channels = []

            def get_channel(self, channel_id):
                return next((ch for ch in self.channels if ch.id == channel_id), None)

        guild = FakeGuild()
        stale_channel = FakeChannel(111, guild)
        target_channel = FakeChannel(1497055415400136701, guild)
        guild.channels = [stale_channel, target_channel]
        voice_client = FakeVoiceClient(guild, stale_channel)

        cog = Radio.__new__(Radio)
        cog.bot = SimpleNamespace(voice_clients=[voice_client])
        cog.db = FakeDB()
        cog._playback_wait = 0
        cog._restart_in_progress = set()
        cog._guild_locks = {}
        cog._is_voice_channel = lambda ch: getattr(ch, "is_voice", False)
        cog._set_voice_channel_status = AsyncMock()
        starts = []
        cog.start_playing = lambda vc, channel, cfg: starts.append((vc, channel.id, cfg["station_name"]))

        await cog._check_and_connect_guild(guild)

        self.assertEqual(target_channel.connect_calls, 0)
        self.assertEqual(voice_client.moved_to, [target_channel.id])
        self.assertEqual(starts, [(voice_client, target_channel.id, "Cached VC Radio")])


if __name__ == "__main__":
    unittest.main()
