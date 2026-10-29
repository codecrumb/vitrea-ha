"""Offline checks for the mobile protocol client against a fake V-Box on loopback.

Run: python3 tests/test_mobile_client.py
Works from either repo layout (private: client/mobile_vbox.py; public:
custom_components/vitrea/_mobile_client.py).
"""
import importlib.util, pathlib, socket, sys, threading, time

root = pathlib.Path(__file__).resolve().parents[1]
src = next(p for p in (root / "client" / "mobile_vbox.py",
                       root / "hass" / "custom_components" / "vitrea" / "_mobile_client.py",
                       root / "custom_components" / "vitrea" / "_mobile_client.py") if p.exists())
spec = importlib.util.spec_from_file_location("mobile_client_under_test", src)
mc = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mc; spec.loader.exec_module(mc)


class FakeBox:
    """Answers like a V-Box. `mute` = opcodes it silently ignores."""

    def __init__(self, mute=()):
        self.mute = set(mute)
        self.ops = []
        self.eof = threading.Event()
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _reply(self, conn, op, payload=b""):
        body = b"VTP<" + bytes([op, len(payload) + 2, 0]) + payload
        conn.sendall(body + bytes([sum(body) & 0xFF]))

    def _run(self):
        conn, _ = self.srv.accept()
        buf = b""
        while True:
            try:
                chunk = conn.recv(256)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 6 and len(buf) >= 6 + buf[5]:
                frame, buf = buf[:6 + buf[5]], buf[6 + buf[5]:]
                op, payload = frame[4], frame[7:-1]
                self.ops.append(op)
                if op in self.mute:
                    continue
                if op == 1:
                    self._reply(conn, 1, b"\x00\x00\x00\x00")
                elif op == 2:
                    self._reply(conn, 0, b"\x00")
                elif op == 3:
                    self._reply(conn, 3, bytes([29, 22, 25, 10, 20, 26]))
                elif op == 4:
                    self._reply(conn, 4, bytes([7, 4, 4]))
                elif op == 5:
                    self._reply(conn, 5, b"\x01")
                elif op == 50:
                    self._reply(conn, 0, b"\x02")  # NACK
                elif op == 62:
                    self._reply(conn, 0, b"\x00")
                elif op == 66:
                    self._reply(conn, 66, bytes([3, 1, 1, 2, 0, 37, 1]))
        self.eof.set()


def client(box, **kw):
    v = mc.MobileVBox(host="127.0.0.1", port=box.port, user="u", password="p", **kw)
    v.connect()
    return v


# login + the app's post-login handshake, in the app's order
box = FakeBox(); v = client(box)
v.login(); v.handshake(timeout=2)
assert box.ops == [1, 4, 5, 3], box.ops
assert v.controller_version == "7.4.4" and v.is_super_user is True, (v.controller_version, v.is_super_user)
# close() logs out BEFORE dropping the socket
v.close(); assert box.eof.wait(2)
assert box.ops == [1, 4, 5, 3, 2], box.ops

# op 66: every scenario with its enabled flag
box = FakeBox(); v = client(box); v.login()
assert v.get_scenario_statuses() == {1: True, 2: False, 37: True}
v.trigger_scenario(5, timeout=2); v.set_scenario_enabled(5, False, timeout=2)
v.close()

# never logged in -> nothing to log out of
box = FakeBox(); v = client(box); v.close(); assert box.eof.wait(2) and box.ops == [], box.ops

# a mute session raises the connection-level error (that is what justifies a re-login) ...
box = FakeBox(mute={4}); v = client(box); v.login()
t0 = time.monotonic()
try:
    v.handshake(timeout=0.3); raise SystemExit("handshake should time out")
except mc.MobileVBoxConnectionError:
    pass
assert time.monotonic() - t0 < 2
v.close()

# ... while a NACK is a plain MobileVBoxError: the box answered, so no re-login
box = FakeBox(); v = client(box); v.login()
try:
    v.set_key_status(3, 0, mc.KEY_ON, timeout=2); raise SystemExit("NACK should raise")
except mc.MobileVBoxConnectionError:
    raise SystemExit("a NACK must not look like a dead session")
except mc.MobileVBoxError:
    pass
v.close()

# closing a session the box has stopped answering costs a bounded wait, never hangs
box = FakeBox(mute={2}); v = client(box); v.login()
t0 = time.monotonic(); v.close(); assert time.monotonic() - t0 < 1.5, time.monotonic() - t0

# the vendored copy in the HA integration must match the source of truth (header aside)
vend = root / "hass" / "custom_components" / "vitrea" / "_mobile_client.py"
if (root / "client" / "mobile_vbox.py").exists() and vend.exists():
    a = (root / "client" / "mobile_vbox.py").read_text().split("Vitrea V-Box *mobile* client", 1)[1]
    b = vend.read_text().split("Vitrea V-Box *mobile* client", 1)[1]
    assert a == b, "hass/.../_mobile_client.py has drifted from client/mobile_vbox.py"

print("all mobile client tests passed")
