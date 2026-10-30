#!/usr/bin/env python3
"""TeamSpeak dice bot for an air-gapped VPS.

This bot connects via localhost ServerQuery, listens for private messages and
channel messages in one configured public channel, and supports D&D dice rolls
plus lightweight initiative tracking.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import random
import re
import shlex
import socket
import sys
import time
import json
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Protocol


WELCOME_LINE = "Welcome to the TeamSpeak"
BANNER_DEADLINE_SECONDS = 15
BANNER_SETTLE_SECONDS = 1.0
DEFAULT_PORTS = {"raw": 10011, "ssh": 10022}
MAX_DICE_COUNT = 100
MAX_DIE_SIDES = 1000
MAX_MESSAGE_LEN = 400
RECONNECT_SECONDS = 5
KEEPALIVE_SECONDS = 60
COMMAND_INTERVAL_SECONDS = 0.4  # TS6 floods at 10 commands/3 s; stay well under

TARGETMODE_CLIENT = 1
TARGETMODE_CHANNEL = 2

HELP_TEXT = (
    "[b]DiceBot[/b] commands: "
    "!roll <expr>, !adv <expr>, !dis <expr>, "
    "!init add <name> <expr>, !init list, "
    "!init next, !init remove <name>, "
    "!init clear, !help. "
    "[i]Shorthand[/i]: d20+2, 2d6+3, "
    "adv d20+5, dis d20+1, "
    "wd20+2, ld20-6. "
    "[i]Keep/drop[/i]: 4d6k3, 4d6kh3, 4d6kl3, 4d6d1, 4d6dh1, 4d6dl1"
)

EXPR_RE = re.compile(
    r"^\s*(\d*)d(\d+)((?:k|kh|kl|d|dh|dl)(\d+))?([+-]\d+)?\s*$",
    re.IGNORECASE,
)
SHORT_ADV_RE = re.compile(r"^\s*(adv|dis)\s+(.+?)\s*$", re.IGNORECASE)
SHORT_WL_RE = re.compile(r"^\s*([wl])\s*((?:\d*)d\d+(?:[+-]\d+)?)\s*$", re.IGNORECASE)


def ts3_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("/", "\\/")
        .replace(" ", "\\s")
        .replace("|", "\\p")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def ts3_unescape(value: str) -> str:
    chars: List[str] = []
    i = 0
    mapping = {
        "s": " ",
        "p": "|",
        "/": "/",
        "\\": "\\",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            chars.append(mapping.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            chars.append(value[i])
            i += 1
    return "".join(chars)


def bb_bold(text: str) -> str:
    return f"[b]{text}[/b]"


def bb_italic(text: str) -> str:
    return f"[i]{text}[/i]"


def bb_code(text: str) -> str:
    return f"[code]{text}[/code]"


def bb_color(text: str, color: str) -> str:
    return f"[color={color}]{text}[/color]"


def parse_records(payload: str) -> List[Dict[str, str]]:
    payload = payload.strip()
    if not payload:
        return []
    records: List[Dict[str, str]] = []
    for raw_record in payload.split("|"):
        record: Dict[str, str] = {}
        for field in raw_record.split(" "):
            if "=" not in field:
                continue
            key, value = field.split("=", 1)
            record[key] = ts3_unescape(value)
        if record:
            records.append(record)
    return records


@dataclass
class DiceResult:
    total: int
    label: str
    breakdown: str


@dataclass
class InitiativeEntry:
    name: str
    total: int
    detail: str
    added_by: str


@dataclass
class InitiativeState:
    entries: List[InitiativeEntry] = field(default_factory=list)
    cursor: int = -1

    def add(self, entry: InitiativeEntry) -> None:
        self.entries.append(entry)
        self.entries.sort(key=lambda item: (-item.total, item.name.lower()))
        if self.cursor >= len(self.entries):
            self.cursor = len(self.entries) - 1

    def remove(self, name: str) -> bool:
        lowered = name.lower()
        new_entries = [entry for entry in self.entries if entry.name.lower() != lowered]
        removed = len(new_entries) != len(self.entries)
        self.entries = new_entries
        if not self.entries:
            self.cursor = -1
        elif self.cursor >= len(self.entries):
            self.cursor = 0
        return removed

    def clear(self) -> None:
        self.entries.clear()
        self.cursor = -1

    def next(self) -> Optional[InitiativeEntry]:
        if not self.entries:
            return None
        self.cursor = (self.cursor + 1) % len(self.entries)
        return self.entries[self.cursor]

    def list_text(self) -> str:
        if not self.entries:
            return f"{bb_bold('Initiative')}: {bb_italic('empty')}"
        parts = []
        for idx, entry in enumerate(self.entries, start=1):
            marker = f" {bb_color('<= current', 'orange')}" if self.cursor == idx - 1 else ""
            parts.append(
                f"{idx}. {bb_bold(entry.name)} "
                f"{bb_color(str(entry.total), 'lime')} {bb_italic(f'({entry.detail})')}{marker}"
            )
        return f"{bb_bold('Initiative')}: " + " | ".join(parts)

    def to_dict(self) -> Dict[str, object]:
        return {
            "cursor": self.cursor,
            "entries": [
                {
                    "name": entry.name,
                    "total": entry.total,
                    "detail": entry.detail,
                    "added_by": entry.added_by,
                }
                for entry in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "InitiativeState":
        entries = [
            InitiativeEntry(
                name=str(item["name"]),
                total=int(item["total"]),
                detail=str(item["detail"]),
                added_by=str(item.get("added_by", "unknown")),
            )
            for item in data.get("entries", [])
            if isinstance(item, dict)
        ]
        cursor = int(data.get("cursor", -1))
        if entries and cursor >= len(entries):
            cursor = 0
        if not entries:
            cursor = -1
        return cls(entries=entries, cursor=cursor)


class QueryError(RuntimeError):
    pass


class Transport(Protocol):
    def sendall(self, data: bytes) -> None: ...

    def recv(self, n: int) -> bytes: ...

    def settimeout(self, t: Optional[float]) -> None: ...

    def close(self) -> None: ...


class RawTransport:
    @staticmethod
    def open(host: str, port: int) -> Transport:
        return socket.create_connection((host, port), timeout=10)


PARAMIKO_MISSING = "DICEBOT_QUERY_PROTOCOL=ssh needs paramiko: pip install -r requirements.txt"


def format_fingerprint(key_bytes: bytes) -> str:
    digest = hashlib.sha256(key_bytes).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def fingerprint_matches(expected: str, key_bytes: bytes) -> bool:
    normalized = expected.strip()
    if normalized.startswith("SHA256:"):
        normalized = normalized[len("SHA256:"):]
    normalized = normalized.rstrip("=")
    return "SHA256:" + normalized == format_fingerprint(key_bytes)


def _log_stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class SshTransport:
    """ServerQuery over SSH: password auth, then a shell channel without a PTY."""

    def __init__(self, client: object, channel: object) -> None:
        self._client = client
        self._channel = channel

    @classmethod
    def open(
        cls,
        host: str,
        port: int,
        username: str,
        password: str,
        expected_fingerprint: str,
        log: Optional[Callable[[str], None]] = None,
    ) -> "SshTransport":
        try:
            import paramiko
        except ImportError:
            raise ValueError(PARAMIKO_MISSING) from None
        emit = log if log is not None else _log_stderr

        class HostKeyPolicy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                actual = format_fingerprint(key.asbytes())
                if expected_fingerprint:
                    if not fingerprint_matches(expected_fingerprint, key.asbytes()):
                        raise QueryError(f"SSH host key mismatch: expected {expected_fingerprint}, got {actual}")
                    return
                emit(f"[dicebot] SSH host key fingerprint (set DICEBOT_SSH_HOST_KEY_SHA256 to pin): {actual}")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(HostKeyPolicy())
        try:
            client.connect(
                host,
                port=port,
                username=username,
                password=password,
                look_for_keys=False,
                allow_agent=False,
                timeout=10,
            )
            # SSHClient.invoke_shell() always requests a PTY; TS6 expects a plain shell.
            channel = client.get_transport().open_session()
            channel.invoke_shell()
        except BaseException:
            client.close()
            raise
        return cls(client, channel)

    def sendall(self, data: bytes) -> None:
        self._channel.sendall(data)

    def recv(self, n: int) -> bytes:
        return self._channel.recv(n)

    def settimeout(self, t: Optional[float]) -> None:
        self._channel.settimeout(t)

    def close(self) -> None:
        try:
            self._channel.close()
        finally:
            self._client.close()


class TS3QueryClient:
    def __init__(
        self,
        host: str,
        port: int,
        protocol: str = "raw",
        transport_factory: Optional[Callable[[], Transport]] = None,
        clock: Callable[[], float] = time.monotonic,
        ssh_credentials: Optional[tuple[str, str, str]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.host = host
        self.port = port
        self.protocol = protocol
        self.ssh_credentials = ssh_credentials
        if transport_factory is None and protocol == "ssh" and ssh_credentials is not None:
            user, password, fingerprint = ssh_credentials
            transport_factory = lambda: SshTransport.open(self.host, self.port, user, password, fingerprint)
        self.transport_factory = transport_factory
        self.clock = clock
        self.sleep = sleep
        self.transport: Optional[Transport] = None
        self.buffer = ""
        self.event_queue: List[Dict[str, str]] = []
        self.last_command_at = self.clock()

    def _open_transport(self) -> Transport:
        if self.transport_factory is not None:
            return self.transport_factory()
        if self.protocol == "raw":
            return RawTransport.open(self.host, self.port)
        raise QueryError("ssh transport needs ssh_credentials")

    def connect(self) -> None:
        self.close()
        transport = self._open_transport()
        transport.settimeout(10)
        self.transport = transport
        deadline = self.clock() + BANNER_DEADLINE_SECONDS
        while True:
            greeting = self._wait_for_line(timeout=max(0.1, deadline - self.clock()))
            if WELCOME_LINE in greeting:
                break
            if self.clock() >= deadline:
                raise QueryError(f"unexpected greeting: {greeting!r}")
        self._consume_banner_trailer()
        self.last_command_at = self.clock()

    def _consume_banner_trailer(self) -> None:
        # TS6 SSH follows the welcome with status lines; TS3 raw sends nothing more.
        while True:
            line = self._wait_for_line(timeout=BANNER_SETTLE_SECONDS, allow_timeout=True)
            if line is None:
                return
            if line == "" or line.startswith("virtualserver_status=") or line.startswith("error id=0"):
                continue
            self.buffer = line + "\n" + self.buffer  # not ours; put it back
            return

    def close(self) -> None:
        if self.transport is not None:
            try:
                self.transport.sendall(b"quit\r\n")
            except Exception:
                pass
            try:
                self.transport.close()
            except Exception:
                pass
        self.transport = None
        self.buffer = ""
        self.event_queue.clear()

    def send_command(self, command: str, timeout: int = 10) -> List[Dict[str, str]]:
        if self.transport is None:
            raise QueryError("not connected")
        remaining = COMMAND_INTERVAL_SECONDS - (self.clock() - self.last_command_at)
        if remaining > 0:
            self.sleep(remaining)
        self.last_command_at = self.clock()
        self.transport.sendall((command + "\r\n").encode("utf-8"))
        return self._drain_until_error(timeout=timeout)

    def keepalive_if_idle(self) -> bool:
        if self.clock() - self.last_command_at < KEEPALIVE_SECONDS:
            return False
        self.send_command("whoami")
        return True

    def recv_event(self, timeout: int = 1) -> Optional[Dict[str, str]]:
        if self.event_queue:
            return self.event_queue.pop(0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._wait_for_line(timeout=max(0.1, deadline - time.time()), allow_timeout=True)
            if line is None:
                return None
            if line.startswith("notify"):
                event_name, _, payload = line.partition(" ")
                record = {"event": event_name}
                records = parse_records(payload)
                if records:
                    record.update(records[0])
                return record
        return None

    def _wait_for_line(self, timeout: float, allow_timeout: bool = False) -> Optional[str]:
        while True:
            if "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                return line.strip("\r")
            try:
                assert self.transport is not None
                self.transport.settimeout(timeout)
                chunk = self.transport.recv(65535)
            except socket.timeout:
                if allow_timeout:
                    return None
                raise QueryError("timed out waiting for TeamSpeak response")
            if not chunk:
                raise QueryError("connection closed by TeamSpeak")
            self.buffer += chunk.decode("utf-8", errors="replace")

    def _drain_until_error(self, timeout: int) -> List[Dict[str, str]]:
        records: List[Dict[str, str]] = []
        deadline = time.time() + timeout
        while True:
            line = self._wait_for_line(timeout=max(0.1, deadline - time.time()))
            if line.startswith("notify"):
                event_name, _, payload = line.partition(" ")
                event = {"event": event_name}
                parsed = parse_records(payload)
                if parsed:
                    event.update(parsed[0])
                self.event_queue.append(event)
                continue
            if line.startswith("error "):
                error_record = parse_records(line)[0]
                if error_record.get("id") != "0":
                    raise QueryError(f"TeamSpeak error on command: {error_record}")
                return records
            records.extend(parse_records(line))


class DiceBot:
    def __init__(
        self,
        environ: Optional[Mapping[str, str]] = None,
        query: "Optional[TS3QueryClient]" = None,
    ) -> None:
        env = os.environ if environ is None else environ
        self.query_protocol = normalize_protocol(env.get("DICEBOT_QUERY_PROTOCOL"))
        self.query_host = env.get("DICEBOT_QUERY_HOST", "127.0.0.1")
        self.query_port = resolve_query_port(self.query_protocol, env.get("DICEBOT_QUERY_PORT"))
        self.query_user = require_env("DICEBOT_QUERY_USER", env)
        self.query_pass = require_env("DICEBOT_QUERY_PASS", env)
        self.virtual_server_id = env.get("DICEBOT_VIRTUAL_SERVER_ID", "1")
        self.public_channel_id = env.get("DICEBOT_PUBLIC_CHANNEL_ID", "2")
        self.nickname = env.get("DICEBOT_NICKNAME", "DiceBot (query)")
        self.gm_group_name = env.get("DICEBOT_GM_GROUP_NAME", "GM")
        self.state_path = env.get("DICEBOT_STATE_PATH", "/home/amirali/teamspeak-dicebot/state.json")
        self.ssh_host_key_sha256 = env.get("DICEBOT_SSH_HOST_KEY_SHA256", "").strip()
        self._public_channel_name_override = env.get("DICEBOT_PUBLIC_CHANNEL_NAME", "").strip()
        self.public_channel_name = self._public_channel_name_override
        self.initiative_by_channel: Dict[str, InitiativeState] = {}
        self.group_name_by_id: Dict[str, str] = {}
        if query is None:
            ssh_credentials = None
            if self.query_protocol == "ssh":
                # Fail at startup (exit 2), not inside the reconnect loop; find_spec does not import it.
                if importlib.util.find_spec("paramiko") is None:
                    raise ValueError(PARAMIKO_MISSING)
                ssh_credentials = (self.query_user, self.query_pass, self.ssh_host_key_sha256)
            query = TS3QueryClient(
                self.query_host, self.query_port, protocol=self.query_protocol, ssh_credentials=ssh_credentials
            )
        self.query = query
        self.self_clid: Optional[str] = None
        self._load_state()

    def run_forever(self) -> None:
        backoff = RECONNECT_SECONDS
        while True:
            try:
                self._connect_and_loop()
                backoff = RECONNECT_SECONDS
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[dicebot] connection loop failed: {exc}", file=sys.stderr, flush=True)
                self.query.close()
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _connect_and_loop(self) -> None:
        self._connect()
        while True:
            self.query.keepalive_if_idle()
            event = self.query.recv_event(timeout=5)
            if event is None:
                continue
            self._handle_event(event)

    def _connect(self) -> None:
        self.query.connect()
        if self.query_protocol == "raw":
            self.query.send_command(
                f"login client_login_name={ts3_escape(self.query_user)} "
                f"client_login_password={ts3_escape(self.query_pass)}"
            )
        self.query.send_command(f"use sid={self.virtual_server_id}")
        self.public_channel_name = self._resolve_public_channel_name()
        self.query.send_command(f"clientupdate client_nickname={ts3_escape(self.nickname)}")
        self.group_name_by_id = self._load_group_names()
        self.self_clid = self._whoami()["client_id"]
        self.query.send_command(f"clientmove clid={self.self_clid} cid={self.public_channel_id}")
        self.query.send_command("servernotifyregister event=textprivate")
        self.query.send_command(f"servernotifyregister event=textchannel id={self.public_channel_id}")
        print("[dicebot] connected and ready", flush=True)

    def _resolve_public_channel_name(self) -> str:
        if self._public_channel_name_override:
            return self._public_channel_name_override
        try:
            records = self.query.send_command(f"channelinfo cid={self.public_channel_id}")
            name = records[0].get("channel_name", "").strip() if records else ""
        except QueryError:
            name = ""
        return name or f"channel {self.public_channel_id}"

    def _load_group_names(self) -> Dict[str, str]:
        groups = self.query.send_command("servergrouplist")
        return {record["sgid"]: record["name"] for record in groups if "sgid" in record and "name" in record}

    def _whoami(self) -> Dict[str, str]:
        records = self.query.send_command("whoami")
        if not records:
            raise QueryError("whoami returned no records")
        return records[0]

    def _client_info(self, clid: str) -> Dict[str, str]:
        records = self.query.send_command(f"clientinfo clid={clid}")
        if not records:
            raise QueryError(f"clientinfo returned no data for clid={clid}")
        return records[0]

    def _send_private(self, clid: str, message: str) -> None:
        message = message[:MAX_MESSAGE_LEN]
        self.query.send_command(
            f"sendtextmessage targetmode={TARGETMODE_CLIENT} target={clid} msg={ts3_escape(message)}"
        )

    def _send_channel(self, cid: str, message: str) -> None:
        message = message[:MAX_MESSAGE_LEN]
        self.query.send_command(
            f"sendtextmessage targetmode={TARGETMODE_CHANNEL} target={cid} msg={ts3_escape(message)}"
        )

    def _handle_event(self, event: Dict[str, str]) -> None:
        event_name = event.get("event", "")
        if event_name not in {"notifytextmessage", "notifytextprivate", "notifytextchannel"}:
            return
        invoker_id = event.get("invokerid")
        invoker_name = event.get("invokername", "unknown")
        msg = event.get("msg", "").strip()
        if not invoker_id or not msg:
            return
        if invoker_id == self.self_clid:
            return
        info = self._client_info(invoker_id)
        reply_mode = TARGETMODE_CLIENT
        if event_name == "notifytextchannel" or event.get("targetmode") == str(TARGETMODE_CHANNEL):
            reply_mode = TARGETMODE_CHANNEL

        if reply_mode == TARGETMODE_CLIENT:
            reply_target = invoker_id
        else:
            # Prefer the exact event target so replies go back to the same channel
            # the command arrived from, even if the sender moves immediately after.
            reply_target = (
                event.get("target")
                or event.get("ctid")
                or event.get("cid")
                or info.get("cid")
                or self.public_channel_id
            )
        if reply_mode == TARGETMODE_CHANNEL and reply_target != self.public_channel_id:
            self._send_private(
                invoker_id, f"Use private chat with {self.nickname} outside {self.public_channel_name}."
            )
            return
        try:
            response = self._dispatch_message(msg, info, invoker_name)
        except ValueError as exc:
            response = str(exc)
        if not response:
            return
        if reply_mode == TARGETMODE_CLIENT:
            self._send_private(invoker_id, response)
        else:
            self._send_channel(reply_target, response)

    def _dispatch_message(self, msg: str, client_info: Dict[str, str], invoker_name: str) -> Optional[str]:
        lowered = msg.lower()
        if lowered == "!help":
            return HELP_TEXT
        if lowered.startswith("!init"):
            return self._handle_init(msg, client_info, invoker_name)
        if lowered.startswith("!roll "):
            return self._format_roll(msg[6:].strip(), mode="normal")
        if lowered.startswith("!adv "):
            return self._format_roll(msg[5:].strip(), mode="adv")
        if lowered.startswith("!dis "):
            return self._format_roll(msg[5:].strip(), mode="dis")

        shorthand = SHORT_ADV_RE.match(msg)
        if shorthand:
            mode = shorthand.group(1).lower()
            return self._format_roll(shorthand.group(2), mode=mode)
        shorthand_wl = SHORT_WL_RE.match(msg)
        if shorthand_wl:
            mode = "adv" if shorthand_wl.group(1).lower() == "w" else "dis"
            return self._format_roll(shorthand_wl.group(2), mode=mode)
        if EXPR_RE.match(msg):
            return self._format_roll(msg, mode="normal")
        return None

    def _handle_init(self, msg: str, client_info: Dict[str, str], invoker_name: str) -> str:
        parts = shlex.split(msg)
        if len(parts) < 2:
            return (
                f"{bb_bold('Usage')}: !init add <name> <expr> | "
                f"list | next | remove <name> | clear"
            )
        channel_id = client_info.get("cid") or self.public_channel_id
        state = self.initiative_by_channel.setdefault(channel_id, InitiativeState())
        subcommand = parts[1].lower()

        if subcommand == "list":
            return state.list_text()
        if subcommand == "add":
            if len(parts) < 4:
                return f"{bb_bold('Usage')}: !init add <name> <expr>"
            name = parts[2]
            expr = " ".join(parts[3:])
            result = roll_expression(expr)
            state.add(InitiativeEntry(name=name, total=result.total, detail=result.breakdown, added_by=invoker_name))
            self._save_state()
            return (
                f"{bb_bold('Added')} {bb_bold(name)} at {bb_color(str(result.total), 'lime')} "
                f"{bb_italic(f'({result.breakdown})')}. {state.list_text()}"
            )
        if subcommand == "next":
            if not self._is_gm(client_info):
                return f"{bb_bold('GM only')}: advance initiative."
            entry = state.next()
            self._save_state()
            if entry is None:
                return f"{bb_bold('Initiative')}: {bb_italic('empty')}"
            return (
                f"{bb_bold('Current turn')}: {bb_bold(entry.name)} at "
                f"{bb_color(str(entry.total), 'lime')} {bb_italic(f'({entry.detail})')}"
            )
        if subcommand == "remove":
            if not self._is_gm(client_info):
                return f"{bb_bold('GM only')}: remove initiative entries."
            if len(parts) < 3:
                return f"{bb_bold('Usage')}: !init remove <name>"
            if state.remove(parts[2]):
                self._save_state()
                return state.list_text()
            return f"{bb_bold('No entry')}: {bb_bold(parts[2])}"
        if subcommand == "clear":
            if not self._is_gm(client_info):
                return f"{bb_bold('GM only')}: clear initiative."
            state.clear()
            self._save_state()
            return f"{bb_bold('Initiative cleared')}"
        return f"{bb_bold('Unknown !init command')}. Use !help."

    def _is_gm(self, client_info: Dict[str, str]) -> bool:
        groups = client_info.get("client_servergroups", "")
        for group_id in groups.split(","):
            if self.group_name_by_id.get(group_id) == self.gm_group_name:
                return True
        return False

    def _format_roll(self, expr: str, mode: str) -> str:
        if mode == "adv":
            return format_advantage(expr, take_highest=True)
        if mode == "dis":
            return format_advantage(expr, take_highest=False)
        result = roll_expression(expr)
        return (
            f"{result.label} = {bb_color(str(result.total), 'lime')} "
            f"{bb_italic(f'({result.breakdown})')}"
        )

    def _load_state(self) -> None:
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return
        except Exception as exc:
            print(f"[dicebot] failed to load state: {exc}", file=sys.stderr, flush=True)
            return
        channels = payload.get("channels", {})
        if not isinstance(channels, dict):
            return
        loaded: Dict[str, InitiativeState] = {}
        for channel_id, state_data in channels.items():
            if isinstance(state_data, dict):
                loaded[str(channel_id)] = InitiativeState.from_dict(state_data)
        self.initiative_by_channel = loaded

    def _save_state(self) -> None:
        payload = {
            "channels": {
                channel_id: state.to_dict()
                for channel_id, state in self.initiative_by_channel.items()
                if state.entries
            }
        }
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp_path = f"{self.state_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
        os.replace(tmp_path, self.state_path)


def normalize_protocol(value: Optional[str]) -> str:
    protocol = (value or "raw").strip().lower() or "raw"
    if protocol not in DEFAULT_PORTS:
        raise ValueError(f"DICEBOT_QUERY_PROTOCOL must be raw or ssh, got {value!r}")
    return protocol


def resolve_query_port(protocol: str, port: Optional[str]) -> int:
    if port is None or not port.strip():
        return DEFAULT_PORTS[protocol]
    return int(port)


def require_env(name: str, environ: Optional[Mapping[str, str]] = None) -> str:
    value = (os.environ if environ is None else environ).get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def parse_expression(expr: str) -> tuple[int, int, Optional[str], Optional[int], int]:
    match = EXPR_RE.match(expr)
    if not match:
        raise ValueError(
            f"{bb_bold('Invalid dice expression')}; use XdY, XdY+Z, or 4d6k3"
        )
    count = int(match.group(1) or "1")
    sides = int(match.group(2))
    selector_raw = match.group(3)
    selector_count_raw = match.group(4)
    modifier = int(match.group(5) or "0")
    selector = selector_raw[:-len(selector_count_raw)].lower() if selector_raw and selector_count_raw else None
    selector_count = int(selector_count_raw) if selector_count_raw else None
    return count, sides, selector, selector_count, modifier


def apply_selector(
    rolls: List[int], selector: Optional[str], selector_count: Optional[int]
) -> tuple[List[int], str]:
    if selector is None:
        return list(rolls), ""

    count = len(rolls)
    assert selector_count is not None
    indexed_rolls = list(enumerate(rolls))
    highest_first = sorted(indexed_rolls, key=lambda item: (-item[1], item[0]))
    lowest_first = sorted(indexed_rolls, key=lambda item: (item[1], item[0]))

    if selector in {"k", "kh", "kl"}:
        if selector_count < 1 or selector_count > count:
            raise ValueError(f"{bb_bold('Keep count')} must be between 1 and {count}")
        chosen = highest_first[:selector_count] if selector in {"k", "kh"} else lowest_first[:selector_count]
        chosen_ids = {idx for idx, _ in chosen}
        kept = [roll for idx, roll in indexed_rolls if idx in chosen_ids]
        mode = f"kept highest {selector_count}" if selector in {"k", "kh"} else f"kept lowest {selector_count}"
        return kept, f" -> {mode}: " + ", ".join(str(roll) for roll in kept)

    if selector in {"d", "dh", "dl"}:
        if selector_count < 1 or selector_count >= count:
            raise ValueError(f"{bb_bold('Drop count')} must be between 1 and {count - 1}")
        dropped = lowest_first[:selector_count] if selector in {"d", "dl"} else highest_first[:selector_count]
        dropped_ids = {idx for idx, _ in dropped}
        kept = [roll for idx, roll in indexed_rolls if idx not in dropped_ids]
        dropped_rolls = [roll for _, roll in dropped]
        mode = f"dropped lowest {selector_count}" if selector in {"d", "dl"} else f"dropped highest {selector_count}"
        return kept, f" -> {mode}: " + ", ".join(str(roll) for roll in dropped_rolls)

    raise ValueError(f"{bb_bold('Unsupported selector')}: {selector}")


def roll_expression(expr: str) -> DiceResult:
    count, sides, selector, selector_count, modifier = parse_expression(expr)
    if count < 1 or count > MAX_DICE_COUNT:
        raise ValueError(f"{bb_bold('Dice count')} must be between 1 and {MAX_DICE_COUNT}")
    if sides < 2 or sides > MAX_DIE_SIDES:
        raise ValueError(f"{bb_bold('Die sides')} must be between 2 and {MAX_DIE_SIDES}")
    rolls = [random.randint(1, sides) for _ in range(count)]
    kept_rolls, selector_suffix = apply_selector(rolls, selector, selector_count)
    total = sum(kept_rolls) + modifier
    modifier_text = f"{modifier:+d}" if modifier else ""
    selector_text = selector if selector else ""
    selector_count_text = str(selector_count) if selector_count is not None else ""
    label = f"{count}d{sides}{selector_text}{selector_count_text}{modifier_text}"
    breakdown = ",".join(str(roll) for roll in rolls)
    if selector_suffix:
        breakdown += selector_suffix
    if modifier:
        breakdown += f"{modifier:+d}"
    return DiceResult(total=total, label=label, breakdown=breakdown)


def format_advantage(expr: str, take_highest: bool) -> str:
    count, sides, selector, selector_count, modifier = parse_expression(expr)
    if count != 1 or sides != 20:
        raise ValueError(f"{bb_bold('adv/dis')} only supports 1d20+modifier")
    if selector or selector_count is not None:
        raise ValueError(f"{bb_bold('adv/dis')} does not support keep/drop selectors")
    first = random.randint(1, 20)
    second = random.randint(1, 20)
    chosen = max(first, second) if take_highest else min(first, second)
    total = chosen + modifier
    mode = "adv" if take_highest else "dis"
    modifier_text = f"{modifier:+d}" if modifier else ""
    title = bb_bold("ADV") if take_highest else bb_bold("DIS")
    return (
        f"{title} 1d20{modifier_text} = {bb_color(str(total), 'lime')} "
        f"{bb_italic(f'({first}, {second} -> {chosen}{modifier_text})')}"
    )


def main() -> int:
    try:
        bot = DiceBot()
    except ValueError as exc:
        print(f"[dicebot] configuration error: {exc}", file=sys.stderr, flush=True)
        return 2
    while True:
        try:
            bot.run_forever()
        except ValueError as exc:
            print(f"[dicebot] configuration error: {exc}", file=sys.stderr, flush=True)
            return 2
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"[dicebot] fatal error: {exc}", file=sys.stderr, flush=True)
            time.sleep(RECONNECT_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
