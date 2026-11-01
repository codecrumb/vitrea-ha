"""Offline checks for the simple-protocol client against a fake V-Box.

The fake replays behaviour verified live against a real box (see the
_simple_client module docstring).

Run: python3 tests/test_simple_client.py   (or pytest)
"""
import asyncio
import importlib.util
import pathlib
import sys

_path = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "vitrea" / "_simple_client.py"
_spec = importlib.util.spec_from_file_location("simple_client", _path)
sc = importlib.util.module_from_spec(_spec)
sys.modules["simple_client"] = sc
_spec.loader.exec_module(sc)


class FakeBox:
    """Just enough V-Box: nodes 1 (switch+blind) and 2 (switch)."""

    def __init__(self):
        self.state = {(1, 1): "F", (1, 2): "B:100", (2, 1): "O"}
        self.log = []
        self.mute = False  # swallow everything, like a wedged box
        self.writers = []
        self.server = None
        self.port = None

    async def start(self, port=0):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        for w in self.writers:
            w.close()
        await self.server.wait_closed()

    def push(self, w, line):
        w.write((line + "\r\n").encode())

    async def _client(self, r, w):
        self.writers.append(w)
        while True:
            raw = await r.readline()
            if not raw:
                return
            if self.mute:
                continue
            line = raw.decode().strip()
            self.log.append(line)
            if line == "P:VITREA":
                self.push(w, "S:PSW:OK")
            elif line.startswith("H:R"):
                self.push(w, f"S:R{line[3:7]}:OK")
            elif line == "H:NALL:G":
                self.push(w, "OK")
                for (n, k), v in sorted(self.state.items()):
                    self.push(w, f"S:N{n:03d}:{k}:{v}")
                    await w.drain()
                    await asyncio.sleep(0.02)
            elif line.startswith("H:N") and line.endswith(":G"):
                self.push(w, "OK")
            elif line.startswith("H:N"):
                p = line.split(":")
                n, k, act = int(p[1][1:]), int(p[2]), p[3]
                if n not in {1, 2}:
                    self.push(w, f"E:N{n % 256:03d}:1:005")
                elif act == "B" and self.state.get((n, k), "")[:1] != "B":
                    self.push(w, f"E:N{n:03d}:{k}:004")
                else:
                    self.push(w, "OK")
                    if act in ("O", "F"):
                        self.state[(n, k)] = act
                        self.push(w, f"S:N{n:03d}:{k}:{act}")
                    elif act == "B" and p[4] != "255":
                        self.state[(n, k)] = f"B:{p[4]}"
                        self.push(w, f"S:N{n:03d}:{k}:B:{p[4]}")
            await w.drain()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


def test_parse_line():
    assert sc.parse_line("OK") == "OK"
    assert sc.parse_line("S:PSW:OK") == "PSW"
    s = sc.parse_line("S:N041:1:B:071")
    assert (s.node, s.key, s.code, s.data) == (41, 1, "B", "071")
    assert s.kind == sc.KIND_BLIND and s.position == 71 and not s.is_on
    s = sc.parse_line("S:N002:8:O")
    assert s.is_on and s.kind == sc.KIND_SWITCH and s.position is None
    assert sc.parse_line("S:R0010:OK") == sc.ScenarioAck(10, True)
    assert sc.parse_line("E:N041:7:004") == sc.Error(41, 7, "004")
    assert sc.parse_line("garbage") is None
    assert sc.parse_line("S:Nxx:1:O") is None
    assert sc.parse_line("") is None


def test_commands_and_push():
    async def go():
        box = FakeBox()
        await box.start()
        c = sc.SimpleVBox("127.0.0.1", box.port)
        seen = []
        c.add_status_listener(seen.append)
        await c.connect()
        await c.key_on(1, 1)
        await c.key_off(1, 1)
        await c.key_on(2, 1, timer=5)
        await c.blind_position(1, 2, 90)
        await c.blind_stop(1, 2)
        await c.scenario(10)
        await asyncio.sleep(0.1)
        assert "H:N001:1:O:" in box.log
        assert "H:N001:1:F:000" in box.log
        assert "H:N002:1:O:005" in box.log
        assert "H:N001:2:B:090" in box.log
        assert "H:N001:2:B:255" in box.log
        assert "H:R0010" in box.log
        assert [(s.node, s.key, s.code) for s in seen[:2]] == [(1, 1, "O"), (1, 1, "F")]
        await c.close()
        await box.stop()
    run(go())


def test_rejected_command_raises():
    async def go():
        box = FakeBox()
        await box.start()
        c = sc.SimpleVBox("127.0.0.1", box.port)
        await c.connect()
        try:
            await c.blind_position(1, 1, 50)  # key 1 is a switch, not a blind
        except sc.VitreaError as err:
            assert "004" in str(err)
        else:
            raise AssertionError("expected VitreaError")
        # the link still works afterwards
        await c.key_on(1, 1)
        await c.close()
        await box.stop()
    run(go())


def test_discover():
    async def go():
        box = FakeBox()
        await box.start()
        c = sc.SimpleVBox("127.0.0.1", box.port)
        await c.connect()
        sc.DISCOVERY_QUIET = 0.5
        found = await c.discover()
        assert set(found) == {(1, 1), (1, 2), (2, 1)}
        assert found[(1, 2)].kind == sc.KIND_BLIND and found[(1, 2)].position == 100
        assert found[(2, 1)].is_on
        await c.close()
        await box.stop()
    run(go())


def test_reconnects_after_drop():
    async def go():
        sc.KEEPALIVE_INTERVAL = 0.2
        sc.RECONNECT_DELAYS = (0.1,)
        sc.COMMAND_TIMEOUT = 0.3
        box = FakeBox()
        await box.start()
        c = sc.SimpleVBox("127.0.0.1", box.port)
        events = []
        c.add_connection_listener(events.append)
        await c.connect()
        assert events == [True]
        box.mute = True  # wedged: keepalive stops being answered
        await asyncio.sleep(1.0)
        assert False in events
        box.mute = False
        await asyncio.sleep(1.0)
        assert events[-1] is True and c.connected
        await c.key_on(1, 1)
        await c.close()
        await box.stop()
    run(go())


def test_command_fails_fast_when_disconnected():
    async def go():
        box = FakeBox()
        await box.start()
        c = sc.SimpleVBox("127.0.0.1", box.port)
        await c.connect()
        await box.stop()
        await asyncio.sleep(0.2)
        try:
            await c.key_on(1, 1)
        except sc.VitreaError:
            pass
        else:
            raise AssertionError("expected VitreaError")
        await c.close()
    run(go())


def test_probe_and_scan():
    async def go():
        box = FakeBox()
        await box.start()
        assert await sc.probe("127.0.0.1", box.port)
        assert await sc.scan("127.0.0.1/32", box.port) == ["127.0.0.1"]
        await box.stop()
        assert not await sc.probe("127.0.0.1", box.port)
        # a listener that is not a V-Box must not be reported
        srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        assert not await sc.probe("127.0.0.1", port, timeout=0.3)
        srv.close()
    run(go())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
