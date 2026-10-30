import os
import tempfile
import unittest
from unittest import mock

import dicebot
from dicebot import DiceBot, normalize_protocol, resolve_query_port

_TMP = tempfile.mkdtemp()
BASE_ENV = {
    "DICEBOT_QUERY_USER": "bot",
    "DICEBOT_QUERY_PASS": "pw",
    "DICEBOT_STATE_PATH": os.path.join(_TMP, "state.json"),
}


class ProtocolConfigTests(unittest.TestCase):
    def test_default_protocol_is_raw_on_10011(self):
        bot = DiceBot(environ=BASE_ENV, query=object())
        self.assertEqual((bot.query_protocol, bot.query_port), ("raw", 10011))

    def test_ssh_protocol_defaults_to_10022(self):
        bot = DiceBot(environ={**BASE_ENV, "DICEBOT_QUERY_PROTOCOL": "ssh"}, query=object())
        self.assertEqual((bot.query_protocol, bot.query_port), ("ssh", 10022))

    def test_explicit_port_wins_over_protocol_default(self):
        bot = DiceBot(environ={**BASE_ENV, "DICEBOT_QUERY_PROTOCOL": "ssh", "DICEBOT_QUERY_PORT": "2222"}, query=object())
        self.assertEqual(bot.query_port, 2222)

    def test_protocol_is_case_and_whitespace_insensitive(self):
        self.assertEqual(normalize_protocol(" SSH "), "ssh")

    def test_unknown_protocol_is_configuration_error(self):
        with self.assertRaisesRegex(ValueError, "DICEBOT_QUERY_PROTOCOL"):
            normalize_protocol("telnet")

    def test_resolve_query_port(self):
        self.assertEqual(resolve_query_port("raw", None), 10011)
        self.assertEqual(resolve_query_port("ssh", None), 10022)
        self.assertEqual(resolve_query_port("ssh", "10011"), 10011)

    def test_optional_settings_default_to_empty(self):
        bot = DiceBot(environ=BASE_ENV, query=object())
        self.assertEqual((bot.ssh_host_key_sha256, bot.public_channel_name), ("", ""))

    def test_optional_settings_are_stripped(self):
        env = {**BASE_ENV, "DICEBOT_SSH_HOST_KEY_SHA256": " SHA256:abc ", "DICEBOT_PUBLIC_CHANNEL_NAME": " Lobby "}
        bot = DiceBot(environ=env, query=object())
        self.assertEqual((bot.ssh_host_key_sha256, bot.public_channel_name), ("SHA256:abc", "Lobby"))

    def test_main_exits_2_on_unknown_protocol(self):
        env = {**BASE_ENV, "DICEBOT_QUERY_PROTOCOL": "telnet"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stderr"):
            self.assertEqual(dicebot.main(), 2)


if __name__ == "__main__":
    unittest.main()
