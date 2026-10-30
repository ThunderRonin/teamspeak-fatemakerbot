import unittest

from dicebot import QueryError, TS3QueryClient
from tests.fakes import FakeClock, FakeTransport
from tests.test_query_client import TS3_BANNER


class KeepaliveTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(1000.0)  # step 0: time moves only via advance()
        self.t = FakeTransport(banner=TS3_BANNER, responses={"whoami": "client_id=7\nerror id=0 msg=ok\n"})
        self.c = TS3QueryClient("h", 1, transport_factory=lambda: self.t, clock=self.clock,
                              sleep=lambda seconds: None)
        self.c.connect()
        self.c.send_command("use sid=1")
        self.t.sent.clear()

    def test_no_keepalive_before_60_seconds(self):
        self.clock.advance(59.9)
        self.assertFalse(self.c.keepalive_if_idle())
        self.assertEqual(self.t.sent, [])

    def test_keepalive_sends_whoami_at_60_seconds_idle(self):
        self.clock.advance(60)
        self.assertTrue(self.c.keepalive_if_idle())
        self.assertEqual(self.t.sent, [b"whoami\r\n"])

    def test_any_command_resets_the_idle_timer(self):
        self.clock.advance(50)
        self.c.send_command("whoami")
        self.t.sent.clear()
        self.clock.advance(50)
        self.assertFalse(self.c.keepalive_if_idle())

    def test_failed_keepalive_raises_query_error(self):
        self.clock.advance(60)
        self.t.eof = True
        with self.assertRaises(QueryError):
            self.c.keepalive_if_idle()

    def test_event_arriving_during_keepalive_is_queued(self):  # Review Focus 5
        self.t.responses["whoami"] = "notifytextprivate invokerid=3 msg=hi\nclient_id=7\nerror id=0 msg=ok\n"
        self.clock.advance(60)
        self.c.keepalive_if_idle()
        self.assertEqual(self.c.recv_event(timeout=0)["msg"], "hi")


if __name__ == "__main__":
    unittest.main()
