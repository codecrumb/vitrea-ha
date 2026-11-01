"""Offline checks for the admin-protocol client against a fake V-Box.

Run: python3 tests/test_admin_client.py   (or pytest)
"""
import importlib.util
import pathlib
import socketserver
import struct
import sys
import threading

_path = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "vitrea" / "_admin_client.py"
_spec = importlib.util.spec_from_file_location("admin_client", _path)
ac = importlib.util.module_from_spec(_spec)
sys.modules["admin_client"] = ac
_spec.loader.exec_module(ac)


def u16(s):
    return s.encode("utf-16-le")


NODES = {
    7: {"room": 2, "keys": [(255, 3, 2, "Blind"), (255, 3, 2, "N7-1 Pair"), (255, 1, 2, "Main"),
                            (0, 1, 0, "Hall MW"), (0, 6, 2, "Fan ")]},
}
ROOMS = {0: "Floor 0 Hallway", 2: "Living"}
CATS = {1: (1, "Light"), 3: (3, "Blind"), 6: (5, "Fan")}  # category no -> (icon, name)
SCENARIOS = {10: "Night light"}


class FakeAdmin(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, in_use=False, drop_after=None, password="secret"):
        self.in_use, self.drop_after, self.password = in_use, drop_after, password
        self.requests = 0
        self.logins = 0
        super().__init__(("127.0.0.1", 0), _Handler)
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()


def reply(op, msg_no, payload):
    body = b"VTU<" + bytes([op]) + struct.pack(">H", len(payload) + 2) + bytes([msg_no]) + payload
    return body + bytes([sum(body) & 0xFF])


def read_str(p, i):
    n = p[i]
    return p[i + 1:i + 1 + n].decode("utf-16-le"), i + 1 + n


class _Handler(socketserver.BaseRequestHandler):
    def recv(self, n):
        buf = b""
        while len(buf) < n:
            d = self.request.recv(n - len(buf))
            if not d:
                raise EOFError
            buf += d
        return buf

    def handle(self):
        srv = self.server
        try:
            while True:
                head = self.recv(8)
                op, length, msg_no = head[4], (head[5] << 8) | head[6], head[7]
                payload = self.recv(length - 1)[:-1]
                if op == ac.OP_LOGIN:
                    srv.logins += 1
                    user, i = read_str(payload, 0)
                    pw, _ = read_str(payload, i)
                    if srv.in_use:
                        code = 3
                    elif user == "admin" and pw == srv.password:
                        code = 0
                    else:
                        code = 1
                    self.request.sendall(reply(0, msg_no, bytes([code])))
                    continue
                srv.requests += 1
                if srv.drop_after and srv.requests == srv.drop_after:
                    self.request.close()  # the box drops the connection mid-read
                    return
                self.request.sendall(reply(op, msg_no, self.answer(op, payload)))
        except (EOFError, OSError):
            return

    def answer(self, op, p):
        if op == ac.OP_HEARTBEAT:
            return b"\x00"
        if op == ac.OP_GET_NODE_NUMBERS:
            return bytes([len(NODES)]) + bytes(NODES)
        if op == ac.OP_GET_NODE_GENERAL_PARAMS:
            n = NODES[p[0]]
            keys = len(n["keys"])
            return (bytes([p[0]]) + bytes(8) + bytes([0, 24, keys]) + bytes(keys)
                    + bytes([0, 0, 0, 7, 9, 1, 0, n["room"], 0, 0]))
        if op == ac.OP_GET_KEY_PARAMS:
            cls, cat, room, name = NODES[p[0]]["keys"][p[1]]
            return bytes([p[0], p[1], cls, cat, room]) + bytes(5) + bytes(2) + bytes([len(u16(name))]) + u16(name)
        if op == ac.OP_GET_KEY_CATEGORY:
            icon, name = CATS.get(p[0], (0, ""))
            return bytes([p[0], icon, len(u16(name))]) + u16(name)
        if op == ac.OP_GET_ROOM_NUMBERS:
            return bytes([len(ROOMS)]) + bytes(ROOMS)
        if op == ac.OP_GET_ROOM_PARAMS:
            return bytes([p[0], 0, len(u16(ROOMS[p[0]]))]) + u16(ROOMS[p[0]])
        if op == ac.OP_GET_SCENARIO_NUMBERS:
            return bytes([len(SCENARIOS)]) + bytes(SCENARIOS)
        if op == ac.OP_GET_SCENARIO_PARAMS:
            name = SCENARIOS[p[0]]
            # 1 event (6 bytes), 2 actions (7 bytes each), then the name
            return (bytes([p[0], 1]) + bytes(6) + bytes([2]) + bytes(14)
                    + bytes([len(u16(name))]) + u16(name) + bytes(3))
        return b""


def client(srv, password="secret", **kw):
    return ac.AdminVBox("127.0.0.1", port=srv.port, user="admin", password=password, timeout=2.0, **kw)


def test_reads_the_whole_device_list():
    srv = FakeAdmin()
    with client(srv) as a:
        data = a.read_device_list(pace=0)
    srv.shutdown()
    assert data["rooms"] == ROOMS
    assert data["categories"][1] == "Light" and data["categories"][6] == "Fan" and data["categories"][3] == "Blind"
    node = data["nodes"][7]
    assert node["num_keys"] == 5 and node["room_no"] == 2 and node["firmware"] == "7.91"
    assert [(k["key"], k["class"], k["name"]) for k in node["keys"]] == [
        (0, 255, "Blind"), (1, 255, "N7-1 Pair"), (2, 255, "Main"), (3, 0, "Hall MW"), (4, 0, "Fan ")]
    assert data["scenarios"] == [{"id": 10, "name": "Night light"}]


def test_survives_a_dropped_connection_mid_read():
    srv = FakeAdmin(drop_after=12)
    with client(srv) as a:
        data = a.read_device_list(pace=0)
    srv.shutdown()
    assert len(data["nodes"][7]["keys"]) == 5
    assert srv.logins >= 2  # it logged in again and carried on


def test_busy_session_and_wrong_password_are_distinct_errors():
    srv = FakeAdmin(in_use=True)
    try:
        client(srv).connect()
    except ac.AdminInUseError:
        pass
    else:
        raise AssertionError("expected AdminInUseError")
    srv.shutdown()
    srv = FakeAdmin()
    try:
        client(srv, password="wrong").connect()
    except ac.AdminAuthError:
        pass
    else:
        raise AssertionError("expected AdminAuthError")
    srv.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
