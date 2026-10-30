import socket


class FakeTransport:
    """Socket duck type for query-protocol tests."""

    def __init__(self, banner="", responses=None):
        self.sent = []
        self.closed = False
        self.incoming = []
        self.eof = False
        self.responses = responses if responses is not None else {}
        if banner:
            self.feed(banner)

    def sendall(self, data):
        self.sent.append(data)
        words = data.decode("utf-8", "replace").split()
        reply = self.responses.get(words[0] if words else "", "error id=0 msg=ok\n")
        self.incoming.append(reply.encode("utf-8"))

    def recv(self, n):
        if self.eof:
            return b""
        if not self.incoming:
            raise socket.timeout("timed out")
        return self.incoming.pop(0)

    def settimeout(self, t):
        pass

    def close(self):
        self.closed = True

    def feed(self, text):
        self.incoming.append(text.encode("utf-8"))
