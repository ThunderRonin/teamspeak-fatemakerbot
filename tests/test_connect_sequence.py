import contextlib
import io
import unittest

from dicebot import DiceBot
from tests.fakes import RecordingQuery
from tests.test_config import BASE_ENV


class ConnectSequenceTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.addCleanup(stack.close)

    def test_raw_mode_sends_login_first(self):
        q = RecordingQuery()
        DiceBot(environ=BASE_ENV, query=q)._connect()
        self.assertTrue(q.commands[0].startswith("login client_login_name=bot client_login_password=pw"))
        self.assertEqual(q.commands[1], "use sid=1")

    def test_ssh_mode_sends_no_login(self):
        q = RecordingQuery()
        DiceBot(environ={**BASE_ENV, "DICEBOT_QUERY_PROTOCOL": "ssh"}, query=q)._connect()
        self.assertFalse(any(c.startswith("login") for c in q.commands))
        self.assertEqual(q.commands[0], "use sid=1")

    def test_connect_registers_both_text_events(self):
        q = RecordingQuery()
        DiceBot(environ=BASE_ENV, query=q)._connect()
        self.assertIn("servernotifyregister event=textprivate", q.commands)
        self.assertIn("servernotifyregister event=textchannel id=2", q.commands)
