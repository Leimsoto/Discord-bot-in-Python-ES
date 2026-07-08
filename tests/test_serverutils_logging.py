import json
import unittest
from unittest.mock import AsyncMock

from cogs.logging import Logging
from cogs.serverutils import ServerUtils


class _DB:
    def __init__(self, log_events):
        self.log_events = log_events
        self.serverlog_channel = None

    def get_server_config(self, guild_id: int):
        return {"guild_id": guild_id, "log_events": self.log_events, "serverlog_channel": self.serverlog_channel}

    def set_server_config(self, guild_id: int, **kwargs):
        if "serverlog_channel" in kwargs:
            self.serverlog_channel = kwargs["serverlog_channel"]


class _Channel:
    def __init__(self, channel_id: int):
        self.id = channel_id

    async def send(self, **kwargs):
        return None


class _Guild:
    def __init__(self, rounded_id: int, real_id: int):
        self.id = 123
        self.name = "Guild"
        self._rounded_id = rounded_id
        self._real_channel = _Channel(real_id)
        self.channels = [self._real_channel]

    def get_channel(self, channel_id: int):
        if channel_id == self._real_channel.id:
            return self._real_channel
        return None

    async def fetch_channel(self, channel_id: int):
        raise AssertionError("rounded channel should be repaired before fetch")


class ServerUtilsLoggingConfigTest(unittest.TestCase):
    def test_dashboard_log_keys_disable_legacy_defaults(self):
        cog = ServerUtils.__new__(ServerUtils)
        cog.db = _DB(json.dumps({
            "message_delete": False,
            "message_edit": False,
            "member_join": False,
            "member_leave": False,
            "voice_join": False,
            "voice_leave": False,
            "role_change": False,
            "channel_create": False,
            "channel_delete": False,
        }))

        events = cog._get_log_events(123)

        self.assertFalse(events["message_delete"])
        self.assertFalse(events["message_edit"])
        self.assertFalse(events["member_join"])
        self.assertFalse(events["member_leave"])
        self.assertFalse(events["voice_join_leave"])
        self.assertFalse(events["role_changes"])
        self.assertFalse(events["channel_updates"])

    def test_legacy_log_keys_still_supported(self):
        cog = ServerUtils.__new__(ServerUtils)
        cog.db = _DB(json.dumps({"voice_join_leave": False, "role_changes": False}))

        events = cog._get_log_events(123)

        self.assertFalse(events["voice_join_leave"])
        self.assertFalse(events["role_changes"])
        self.assertTrue(events["message_delete"])


class ServerUtilsChannelRepairTest(unittest.IsolatedAsyncioTestCase):
    async def test_repair_rounded_serverlog_channel_id(self):
        rounded_id = 1497055416062972000
        real_id = 1497055416062971949
        cog = ServerUtils.__new__(ServerUtils)
        cog.db = _DB(json.dumps({}))
        guild = _Guild(rounded_id, real_id)

        channel, resolved_id, error = await cog._resolve_serverlog_channel(guild, rounded_id)

        self.assertIs(channel, guild.channels[0])
        self.assertEqual(resolved_id, real_id)
        self.assertIsNone(error)
        self.assertEqual(cog.db.serverlog_channel, real_id)


class LoggingCogChannelRepairTest(unittest.IsolatedAsyncioTestCase):
    async def test_repair_rounded_serverlog_channel_id(self):
        rounded_id = 1497055416062972000
        real_id = 1497055416062971949
        cog = Logging.__new__(Logging)
        cog.db = _DB(json.dumps({}))
        guild = _Guild(rounded_id, real_id)

        channel, resolved_id, error = await cog._resolve_serverlog_channel(guild, rounded_id)

        self.assertIs(channel, guild.channels[0])
        self.assertEqual(resolved_id, real_id)
        self.assertIsNone(error)
        self.assertEqual(cog.db.serverlog_channel, real_id)


class ServerUtilsMessageListenerOwnershipTest(unittest.IsolatedAsyncioTestCase):
    async def test_message_edit_and_delete_are_noop_to_avoid_duplicate_logging(self):
        cog = ServerUtils.__new__(ServerUtils)
        cog._send_server_log = AsyncMock()

        author = type("Author", (), {"bot": False})()
        guild = type("Guild", (), {"id": 123})()
        message = type("Message", (), {"guild": guild, "author": author, "content": "before"})()
        after = type("Message", (), {"guild": guild, "author": author, "content": "after"})()

        await cog.on_message_delete(message)
        await cog.on_message_edit(message, after)

        cog._send_server_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
