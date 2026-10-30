import unittest

from dicebot import QueryError, TS3QueryClient
from tests.fakes import FakeClock, FakeTransport

TS3_BANNER = (
    "TS3\n"
    'Welcome to the TeamSpeak 3 ServerQuery interface, type "help" for a list of commands '
    'and "help <command>" for information on a specific command.\n'
)
TS6_BANNER = (
    "TS3\n"
    'Welcome to the TeamSpeak ServerQuery interface, type "help" for a list of commands '
    'and "help <command>" for information on a specific command.\n'
    "virtualserver_status=online\n"
    "error id=0 msg=ok\n"
)


def make_client(banner, responses=None, protocol="raw"):
    t = FakeTransport(banner=banner, responses=responses)
    c = TS3QueryClient("h", 1, protocol=protocol, transport_factory=lambda: t)
    return c, t


class BannerTests(unittest.TestCase):
    def test_accepts_ts3_raw_banner(self):
        c, _ = make_client(TS3_BANNER)
        c.connect()

    def test_accepts_ts6_ssh_banner_and_consumes_trailer(self):
        c, t = make_client(TS6_BANNER, {"use": "error id=0 msg=ok\n"})
        c.connect()
        self.assertEqual(c.send_command("use sid=1"), [])
        self.assertEqual(c.buffer, "")

    def test_first_command_after_ts6_banner_returns_its_own_records(self):
        c, t = make_client(TS6_BANNER, {"whoami": "client_id=7 client_channel_id=2\nerror id=0 msg=ok\n"})
        c.connect()
        self.assertEqual(c.send_command("whoami")[0]["client_id"], "7")

    def test_unexpected_banner_then_silence_raises_query_error(self):
        c, _ = make_client("SSH-2.0-nothing\n")
        with self.assertRaisesRegex(QueryError, "timed out"):
            c.connect()

    def test_eof_before_banner_raises_query_error(self):
        c, t = make_client("")
        t.eof = True
        with self.assertRaises(QueryError):
            c.connect()

    def test_eof_during_command_raises_query_error(self):
        c, t = make_client(TS6_BANNER)
        c.connect()
        t.eof = True
        with self.assertRaises(QueryError):
            c.send_command("whoami")

    def test_close_sends_quit_then_closes_transport(self):
        c, t = make_client(TS3_BANNER)
        c.connect()
        c.close()
        self.assertEqual(t.sent[-1], b"quit\r\n")
        self.assertTrue(t.closed)

    def test_close_without_transport_is_noop(self):
        TS3QueryClient("h", 1).close()

    def test_ssh_without_factory_or_credentials_fails_cleanly(self):
        with self.assertRaisesRegex(QueryError, "ssh transport needs ssh_credentials"):
            TS3QueryClient("h", 1, protocol="ssh").connect()

    def test_no_banner_within_deadline_raises_query_error(self):
        c, _ = make_client("SSH-2.0-x\nfoo\nbar\n")
        c.clock = FakeClock(0.0, step=5.0)
        with self.assertRaisesRegex(QueryError, "unexpected greeting"):
            c.connect()
