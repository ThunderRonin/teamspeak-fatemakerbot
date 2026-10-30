import socket

from dicebot import QueryError


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


class FakeClock:
    """Callable clock: returns `now`, then advances it by `step`."""

    def __init__(self, now=0.0, step=0.0):
        self.now = now
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value

    def advance(self, seconds):
        self.now += seconds


class RecordingQuery:
    """TS3QueryClient stand-in for DiceBot tests."""

    CANNED = {
        "whoami": [{"client_id": "9"}],
        "servergrouplist": [{"sgid": "6", "name": "GM"}],
        "channelinfo": [{"channel_name": "Hallucinators"}],
        "clientinfo": [{"cid": "5", "client_servergroups": "8"}],
    }

    def __init__(self, fail=()):
        self.commands = []
        self.fail = set(fail)

    def connect(self):
        pass

    def close(self):
        pass

    def send_command(self, cmd, timeout=10):
        self.commands.append(cmd)
        if cmd.split()[0] in self.fail:
            raise QueryError(f"{cmd.split()[0]} denied")
        return [dict(r) for r in self.CANNED.get(cmd.split()[0], [])]
