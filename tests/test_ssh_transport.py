import base64
import contextlib
import hashlib
import io
import os
import re
import subprocess
import sys
import types
import unittest
from unittest import mock

from dicebot import (
    DiceBot,
    QueryError,
    SshTransport,
    TS3QueryClient,
    fingerprint_matches,
    format_fingerprint,
)
from tests.test_config import BASE_ENV

import dicebot

# DiceBot checks for paramiko at startup in ssh mode; these pretend it is installed.
PARAMIKO_PRESENT = mock.patch("importlib.util.find_spec", return_value=object())
PARAMIKO_ABSENT = mock.patch("importlib.util.find_spec", return_value=None)

KEY = b"\x00\x00\x00\x07ssh-rsa\x00\x00\x00\x01\x03"
FP = "SHA256:" + base64.b64encode(hashlib.sha256(KEY).digest()).decode("ascii").rstrip("=")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FingerprintTests(unittest.TestCase):
    def test_format_is_openssh_sha256_without_padding(self):
        self.assertEqual(format_fingerprint(KEY), FP)
        self.assertTrue(FP.startswith("SHA256:"))
        self.assertFalse(FP.endswith("="))

    def test_exact_match(self):
        self.assertTrue(fingerprint_matches(FP, KEY))

    def test_bare_base64_match(self):
        self.assertTrue(fingerprint_matches(FP[7:], KEY))

    def test_padding_and_whitespace_tolerated(self):  # Review Focus 3
        self.assertTrue(fingerprint_matches("  " + FP + "=\n", KEY))

    def test_mismatch(self):
        self.assertFalse(fingerprint_matches("SHA256:AAAA", KEY))

    def test_comparison_is_case_sensitive(self):
        self.assertFalse(fingerprint_matches(FP.swapcase(), KEY))


class FakeKey:
    def asbytes(self):
        return KEY


class FakeChannel:
    def __init__(self):
        self.calls = []

    def get_pty(self, *args, **kwargs):
        self.calls.append("get_pty")

    def invoke_shell(self):
        self.calls.append("invoke_shell")

    def sendall(self, data):
        self.calls.append(("sendall", data))

    def recv(self, n):
        return b"x"

    def settimeout(self, t):
        self.calls.append(("settimeout", t))

    def close(self):
        self.calls.append("close")


def make_fake_paramiko():
    """A paramiko stand-in: SSHClient.connect runs the host key policy like the real one."""
    events = []
    channel = FakeChannel()

    class MissingHostKeyPolicy:
        def missing_host_key(self, client, hostname, key):
            raise NotImplementedError

    class FakeSshTransportLayer:
        def open_session(self):
            events.append("open_session")
            return channel

    class SSHClient:
        def __init__(self):
            self.policy = None

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, hostname, **kwargs):
            events.append(("connect", hostname, kwargs))
            self.policy.missing_host_key(self, hostname, FakeKey())

        def get_transport(self):
            return FakeSshTransportLayer()

        def invoke_shell(self, *args, **kwargs):
            events.append("client.invoke_shell")  # requests a PTY; must not be used

        def close(self):
            events.append("client.close")

    module = types.ModuleType("paramiko")
    module.SSHClient = SSHClient
    module.MissingHostKeyPolicy = MissingHostKeyPolicy
    return module, events, channel


class SshTransportOpenTests(unittest.TestCase):
    def open_with_fake(self, expected):
        module, events, channel = make_fake_paramiko()
        logs = []
        with mock.patch.dict(sys.modules, {"paramiko": module}):
            transport = SshTransport.open("h", 10022, "u", "secret-pw", expected, log=logs.append)
        return transport, events, channel, logs

    def test_connects_with_password_only_and_opens_shell_without_pty(self):
        transport, events, channel, _ = self.open_with_fake("")
        self.assertEqual(
            events[0],
            ("connect", "h", {"port": 10022, "username": "u", "password": "secret-pw",
                              "look_for_keys": False, "allow_agent": False, "timeout": 10}),
        )
        self.assertIn("open_session", events)
        self.assertNotIn("client.invoke_shell", events)
        self.assertEqual(channel.calls, ["invoke_shell"])

    def test_unpinned_key_is_accepted_and_fingerprint_logged(self):
        _, _, _, logs = self.open_with_fake("")
        self.assertEqual(
            logs, [f"[dicebot] SSH host key fingerprint (set DICEBOT_SSH_HOST_KEY_SHA256 to pin): {FP}"]
        )

    def test_pinned_matching_key_is_accepted(self):
        transport, _, _, logs = self.open_with_fake(" " + FP[7:] + "=")
        self.assertIsInstance(transport, SshTransport)
        self.assertEqual(logs, [])

    def test_pinned_mismatching_key_is_refused_and_client_closed(self):
        module, events, _ = make_fake_paramiko()
        with mock.patch.dict(sys.modules, {"paramiko": module}):
            with self.assertRaisesRegex(QueryError, re.escape("SSH host key mismatch: expected SHA256:AAAA, got " + FP)):
                SshTransport.open("h", 10022, "u", "secret-pw", "SHA256:AAAA", log=lambda _: None)
        self.assertNotIn("open_session", events)
        self.assertIn("client.close", events)

    def test_transport_delegates_io_and_closes_channel_then_client(self):
        transport, events, channel, _ = self.open_with_fake("")
        transport.sendall(b"whoami\r\n")
        transport.settimeout(3)
        self.assertEqual(transport.recv(10), b"x")
        transport.close()
        self.assertEqual(channel.calls[1:], [("sendall", b"whoami\r\n"), ("settimeout", 3), "close"])
        self.assertEqual(events[-1], "client.close")


class SshModeTests(unittest.TestCase):
    def test_missing_paramiko_is_configuration_error(self):
        with mock.patch.dict(sys.modules, {"paramiko": None}):
            with self.assertRaisesRegex(ValueError, "paramiko"):
                SshTransport.open("h", 10022, "u", "secret-pw", "")

    def test_raw_mode_never_imports_paramiko(self):
        code = (
            "import sys, dicebot; "
            f"dicebot.DiceBot(environ={{'DICEBOT_QUERY_USER': 'bot', 'DICEBOT_QUERY_PASS': 'pw', "
            f"'DICEBOT_STATE_PATH': {BASE_ENV['DICEBOT_STATE_PATH']!r}}}); "
            "print('paramiko' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_default_ssh_factory_looks_up_open_at_call_time(self):
        client = TS3QueryClient("h", 10022, protocol="ssh", ssh_credentials=("u", "pw", "SHA256:x"))
        sentinel = object()
        with mock.patch.object(SshTransport, "open", return_value=sentinel) as opened:
            self.assertIs(client._open_transport(), sentinel)
        opened.assert_called_once_with("h", 10022, "u", "pw", "SHA256:x")

    def test_ssh_mode_without_paramiko_fails_at_startup(self):
        with PARAMIKO_ABSENT as find_spec:
            with self.assertRaisesRegex(ValueError, "DICEBOT_QUERY_PROTOCOL=ssh needs paramiko"):
                DiceBot(environ={**BASE_ENV, "DICEBOT_QUERY_PROTOCOL": "ssh"})
        find_spec.assert_called_once_with("paramiko")

    def test_main_exits_2_when_ssh_mode_lacks_paramiko(self):
        err = io.StringIO()
        env = {**BASE_ENV, "DICEBOT_QUERY_PROTOCOL": "ssh"}
        with mock.patch.dict(os.environ, env, clear=True), PARAMIKO_ABSENT, \
             mock.patch("dicebot.time.sleep", side_effect=AssertionError("must not retry")), \
             contextlib.redirect_stderr(err):
            self.assertEqual(dicebot.main(), 2)
        self.assertIn("configuration error: DICEBOT_QUERY_PROTOCOL=ssh needs paramiko", err.getvalue())

    def test_raw_mode_skips_paramiko_check(self):
        with PARAMIKO_ABSENT as find_spec:
            DiceBot(environ=BASE_ENV)
        find_spec.assert_not_called()

    def test_dicebot_passes_ssh_credentials(self):
        with PARAMIKO_PRESENT:
            bot = DiceBot(environ={**BASE_ENV, "DICEBOT_QUERY_PROTOCOL": "ssh",
                                   "DICEBOT_SSH_HOST_KEY_SHA256": "SHA256:x"})
        self.assertEqual(bot.query.ssh_credentials, ("bot", "pw", "SHA256:x"))

    def test_password_absent_from_log_output_on_connect_failure(self):
        buf = io.StringIO()
        with PARAMIKO_PRESENT:
            bot = DiceBot(environ={**BASE_ENV, "DICEBOT_QUERY_PROTOCOL": "ssh", "DICEBOT_QUERY_PASS": "secret-pw"})
        with mock.patch.object(SshTransport, "open", side_effect=OSError("refused by h:10022 for user bot")), \
             mock.patch("dicebot.time.sleep", side_effect=KeyboardInterrupt), \
             contextlib.redirect_stderr(buf), self.assertRaises(KeyboardInterrupt):
            bot.run_forever()  # one failed connect, logged, then the patched sleep breaks the loop
        self.assertIn("connection loop failed", buf.getvalue())
        self.assertNotIn("secret-pw", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
