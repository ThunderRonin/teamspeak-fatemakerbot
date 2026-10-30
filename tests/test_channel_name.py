import contextlib
import io
import unittest

from dicebot import DiceBot
from tests.fakes import RecordingQuery
from tests.test_config import BASE_ENV


class ChannelNameTests(unittest.TestCase):
    def setUp(self):
        stdout = contextlib.redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def test_env_override_skips_channelinfo(self):
        q = RecordingQuery()
        bot = DiceBot(environ={**BASE_ENV, "DICEBOT_PUBLIC_CHANNEL_NAME": "Hallucinators"}, query=q)
        bot._connect()
        self.assertEqual(bot.public_channel_name, "Hallucinators")
        self.assertFalse(any(c.startswith("channelinfo") for c in q.commands))

    def test_channelinfo_supplies_name(self):
        q = RecordingQuery()
        bot = DiceBot(environ=BASE_ENV, query=q)
        bot._connect()
        self.assertIn("channelinfo cid=2", q.commands)
        self.assertEqual(bot.public_channel_name, "Hallucinators")

    def test_channelinfo_denied_falls_back_to_id(self):  # Review Focus 4
        q = RecordingQuery(fail={"channelinfo"})
        bot = DiceBot(environ=BASE_ENV, query=q)
        bot._connect()
        self.assertEqual(bot.public_channel_name, "channel 2")

    def test_channelinfo_without_name_falls_back_to_id(self):
        q = RecordingQuery()
        q.CANNED = {**RecordingQuery.CANNED, "channelinfo": [{"channel_topic": "x"}]}
        bot = DiceBot(environ=BASE_ENV, query=q)
        bot._connect()
        self.assertEqual(bot.public_channel_name, "channel 2")

    def test_reply_outside_public_channel_names_bot_and_channel(self):
        q = RecordingQuery()
        bot = DiceBot(
            environ={**BASE_ENV, "DICEBOT_NICKNAME": "The FateMaker", "DICEBOT_PUBLIC_CHANNEL_NAME": "Hallucinators"},
            query=q,
        )
        bot._connect()
        q.commands.clear()
        bot._handle_event(
            {"event": "notifytextchannel", "invokerid": "3", "invokername": "x", "msg": "d20", "target": "5"}
        )
        self.assertEqual(
            q.commands[-1],
            r"sendtextmessage targetmode=1 target=3 msg=Use\sprivate\schat\swith\sThe\sFateMaker\soutside\sHallucinators.",
        )
