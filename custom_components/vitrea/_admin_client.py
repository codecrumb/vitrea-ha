"""Minimal blocking client for the V-Box admin protocol (TCP 11501).

Used once, to read the box's own device list: every key of every node with
its real key number, class (255 = physical key, 0 = multi-way alias), name,
room and category, plus rooms and scenarios. That list is what lets the
integration name keys correctly, because the simple protocol (11502) numbers
keys the way the admin protocol does and has no names of its own.

The admin protocol allows one session. A login while another client (for
example Control4) holds it is answered IN_USE and raises AdminInUseError: we
never kick anyone, the caller falls back to other sources of names.

Frame (see docs/protocol.md): "VTU>" opcode len(2) msgNo payload checksum.
Strings are UTF-16 LE, length-prefixed in bytes.
"""
from __future__ import annotations

import socket
import struct
import time

MAGIC_REQUEST = b"VTU>"
MAGIC_RESPONSE = b"VTU<"

OP_ACK = 0
OP_LOGIN = 1
OP_HEARTBEAT = 7
OP_GET_NODE_GENERAL_PARAMS = 31
OP_GET_NODE_NUMBERS = 36
OP_GET_KEY_PARAMS = 43
OP_GET_ROOM_PARAMS = 26
OP_GET_ROOM_NUMBERS = 29
OP_GET_SCENARIO_PARAMS = 51
OP_GET_SCENARIO_NUMBERS = 53
OP_GET_KEY_CATEGORY = 101

ACK_OK = 0
HEARTBEAT_AFTER = 5.0
ACK_NAMES = {0: "ACK", 1: "NACK", 2: "ERROR", 3: "IN_USE", 4: "NOT_AVAILABLE"}

CLASS_PHYSICAL = 255
CLASS_ALIAS = 0

# KeyCategory.Icons from the vendor's client: what the box calls a key.
ICON_NAMES = {
    0: "Undefined", 1: "Light", 2: "Dimmer", 3: "Blind", 4: "Heating", 5: "Fan",
    6: "AirConditioning", 7: "Boiler", 8: "Audio", 9: "TV", 10: "Curtain",
    11: "Scenario", 12: "FloorHeating", 13: "Alarm",
}


class AdminError(RuntimeError):
    pass


class AdminAuthError(AdminError):
    """The box rejected the admin user or password."""


class AdminInUseError(AdminError):
    """Another client holds the single admin session."""


def _checksum(buf: bytes) -> int:
    return sum(buf) & 0xFF


def _frame(opcode: int, msg_no: int, payload: bytes) -> bytes:
    body = MAGIC_REQUEST + bytes([opcode]) + struct.pack(">H", 1 + len(payload) + 1) + bytes([msg_no]) + payload
    return body + bytes([_checksum(body)])


def _str_param(s: str) -> bytes:
    encoded = s.encode("utf-16-le")
    return bytes([len(encoded)]) + encoded


class AdminVBox:
    def __init__(self, host: str, port: int = 11501, user: str = "admin",
                 password: str = "admin", timeout: float = 5.0) -> None:
        self.host, self.port, self.user, self.password = host, port, user, password
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._msg_no = 0
        self._last_io = 0.0

    def __enter__(self) -> "AdminVBox":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        self._last_io = time.monotonic()
        try:
            opcode, payload = self._send(OP_LOGIN, _str_param(self.user) + _str_param(self.password))
        except Exception:
            self.close()
            raise
        if opcode != OP_ACK or not payload:
            self.close()
            raise AdminError(f"unexpected login reply: opcode {opcode}")
        if payload[0] != ACK_OK:
            self.close()
            name = ACK_NAMES.get(payload[0], f"unknown({payload[0]})")
            if payload[0] == 3:
                raise AdminInUseError("the admin session is in use by another client")
            raise AdminAuthError(f"login: server replied {name}")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise AdminError("connection closed")
            buf.extend(chunk)
        return bytes(buf)

    def _send(self, opcode: int, payload: bytes = b"") -> tuple[int, bytes]:
        if self._sock is None:
            raise AdminError("not connected")
        self._msg_no = (self._msg_no + 1) & 0xFF
        msg_no = self._msg_no
        self._last_io = time.monotonic()
        self._sock.sendall(_frame(opcode, msg_no, payload))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            header = self._recv_exact(8)
            if header[:4] != MAGIC_RESPONSE:
                raise AdminError(f"bad magic in response: {header[:4]!r}")
            length = (header[5] << 8) | header[6]
            rest = self._recv_exact(length - 1)
            frame = header + rest
            if _checksum(frame[:-1]) != frame[-1]:
                raise AdminError("bad checksum in response")
            if header[7] == msg_no:
                return header[4], rest[:-1]
            # anything else is an unsolicited push for another message: skip
        raise AdminError(f"timeout waiting for response to opcode {opcode}")

    def _ask(self, opcode: int, payload: bytes = b"") -> bytes:
        """One request, with a reconnect and retry if the box dropped the
        connection (it does that under load; seen live mid-way through a read)."""
        last: Exception | None = None
        for attempt in range(4):
            try:
                if self._sock is None:
                    self._reconnect(attempt)
                if time.monotonic() - self._last_io > HEARTBEAT_AFTER:
                    self._send(OP_HEARTBEAT)
                got, data = self._send(opcode, payload)
                if got != opcode:
                    raise AdminError(f"opcode {opcode}: unexpected reply opcode {got}")
                return data
            except AdminAuthError:
                raise
            except (AdminError, OSError) as err:
                last = err
                self.close()
                time.sleep(0.5 * (attempt + 1))
        raise AdminError(f"opcode {opcode} failed after retries: {last}")

    def _reconnect(self, attempt: int) -> None:
        # The box may still hold the dropped session for a moment (IN_USE).
        for _ in range(5):
            try:
                self.connect()
                return
            except AdminInUseError:
                time.sleep(1.0)
        raise AdminInUseError("the admin session stayed in use")

    # -- device list ----------------------------------------------------
    def get_node_numbers(self) -> list[int]:
        p = self._ask(OP_GET_NODE_NUMBERS)
        return list(p[1:1 + p[0]])

    def get_node_info(self, node: int) -> dict:
        p = self._ask(OP_GET_NODE_GENERAL_PARAMS, bytes([node]))
        n = p[11]
        base = 12 + n
        return {
            "node": p[0],
            "node_type": p[10],
            "num_keys": n,
            "firmware": f"{p[base + 3]}.{p[base + 4]}{p[base + 5]}",
            "room_no": p[base + 7],
        }

    def get_key_params(self, node: int, key: int) -> dict:
        """Key `key` (0-based admin index) of `node`. The simple protocol's
        key number for it is key + 1."""
        p = self._ask(OP_GET_KEY_PARAMS, bytes([node, key]))
        name_len = p[12]
        return {
            "node": p[0],
            "key": p[1],
            "class": p[2],
            "category": p[3],
            "room_no": p[4],
            "name": p[13:13 + name_len].decode("utf-16-le", errors="replace"),
        }

    def get_key_category(self, number: int) -> dict:
        p = self._ask(OP_GET_KEY_CATEGORY, bytes([number]))
        return {
            "number": p[0],
            "icon": ICON_NAMES.get(p[1], "Undefined"),
            "name": p[3:3 + p[2]].decode("utf-16-le", errors="replace"),
        }

    def get_room_numbers(self) -> list[int]:
        p = self._ask(OP_GET_ROOM_NUMBERS)
        return list(p[1:1 + p[0]])

    def get_room_name(self, room: int) -> str:
        p = self._ask(OP_GET_ROOM_PARAMS, bytes([room]))
        return p[3:3 + p[2]].decode("utf-16-le", errors="replace")

    def get_scenario_numbers(self) -> list[int]:
        p = self._ask(OP_GET_SCENARIO_NUMBERS)
        return list(p[1:1 + p[0]])

    def get_scenario_name(self, number: int) -> str:
        p = self._ask(OP_GET_SCENARIO_PARAMS, bytes([number]))
        cursor = 2 + p[1] * 6          # events
        cursor += 1 + p[cursor] * 7    # actions
        n = p[cursor]
        return p[cursor + 1:cursor + 1 + n].decode("utf-16-le", errors="replace")

    def read_device_list(self, pace: float = 0.03) -> dict:
        """Everything the integration needs, in one pass:
        {"rooms": {no: name}, "categories": {no: icon}, "nodes": {no: {...,
         "keys": [key params...]}}, "scenarios": [{"id", "name"}]}.
        """
        rooms: dict[int, str] = {}
        for r in self.get_room_numbers():
            try:
                rooms[r] = self.get_room_name(r)
            except AdminError:
                rooms[r] = f"Room {r}"
            time.sleep(pace)
        categories: dict[int, str] = {}
        for c in range(20):
            try:
                categories[c] = self.get_key_category(c)["icon"]
            except (AdminError, IndexError):
                pass
            time.sleep(pace)
        nodes: dict[int, dict] = {}
        for n in self.get_node_numbers():
            try:
                info = self.get_node_info(n)
            except (AdminError, IndexError):
                continue
            keys = []
            for k in range(info["num_keys"]):
                try:
                    keys.append(self.get_key_params(n, k))
                except (AdminError, IndexError):
                    continue
                time.sleep(pace)
            nodes[n] = {**info, "keys": keys}
        scenarios = []
        for s in self.get_scenario_numbers():
            try:
                scenarios.append({"id": s, "name": self.get_scenario_name(s)})
            except (AdminError, IndexError):
                continue
            time.sleep(pace)
        return {"rooms": rooms, "categories": categories, "nodes": nodes, "scenarios": scenarios}
