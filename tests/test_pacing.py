import unittest

from dicebot import COMMAND_INTERVAL_SECONDS, TS3QueryClient
from tests.fakes import FakeClock, FakeTransport
from tests.test_query_client import TS3_BANNER


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(1000.0)
        self.sleeps = []
        self.t = FakeTransport(banner=TS3_BANNER, responses={"whoami": "client_id=7\nerror id=0 msg=ok\n"})
        self.stamps = []
        original = self.t.sendall

        def stamped(data):
            self.stamps.append(self.clock())
            original(data)

        self.t.sendall = stamped
        self.c = TS3QueryClient(
            "h", 1, transport_factory=lambda: self.t, clock=self.clock, sleep=self._sleep
        )
        self.c.connect()
        self.stamps.clear()

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock.advance(seconds)

    def test_back_to_back_commands_are_spaced_by_interval(self):
        for _ in range(3):
            self.c.send_command("whoami")
        # The first command counts from the banner time, so it waits too.
        self.assertEqual(len(self.sleeps), 3)
        for s in self.sleeps:
            self.assertAlmostEqual(s, COMMAND_INTERVAL_SECONDS, delta=1e-9)
        for a, b in zip(self.stamps, self.stamps[1:]):
            self.assertGreaterEqual(b - a, COMMAND_INTERVAL_SECONDS - 1e-9)

    def test_no_sleep_when_interval_already_elapsed(self):
        for _ in range(3):
            self.clock.advance(1.0)
            self.c.send_command("whoami")
        self.assertEqual(self.sleeps, [])

    def test_burst_stays_under_ts6_limit(self):
        for _ in range(20):
            self.c.send_command("whoami")
        self.assertEqual(len(self.stamps), 20)
        for start in self.stamps:
            in_window = [s for s in self.stamps if start <= s < start + 3.0]
            self.assertLessEqual(len(in_window), 8)

    def test_keepalive_still_fires_after_60s_idle(self):
        self.clock.advance(60)
        self.assertTrue(self.c.keepalive_if_idle())
        self.assertEqual(self.t.sent, [b"whoami\r\n"])
        self.assertEqual(self.sleeps, [])

    def test_close_quit_is_not_paced(self):
        self.c.send_command("whoami")
        self.sleeps.clear()
        self.c.close()
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.t.sent[-1], b"quit\r\n")


if __name__ == "__main__":
    unittest.main()
