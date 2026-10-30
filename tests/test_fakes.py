import socket
import unittest

from tests.fakes import FakeTransport


class FakeTransportTests(unittest.TestCase):
    def test_sendall_queues_scripted_reply_by_first_word(self):
        t = FakeTransport(responses={"whoami": "client_id=7\nerror id=0 msg=ok\n"})
        t.sendall(b"whoami\r\n")
        self.assertEqual(t.sent, [b"whoami\r\n"])
        self.assertEqual(t.recv(65535), b"client_id=7\nerror id=0 msg=ok\n")

    def test_recv_raises_socket_timeout_when_empty(self):
        with self.assertRaises(socket.timeout):
            FakeTransport().recv(65535)

    def test_recv_returns_empty_bytes_after_eof(self):
        t = FakeTransport()
        t.eof = True
        self.assertEqual(t.recv(65535), b"")


if __name__ == "__main__":
    unittest.main()
