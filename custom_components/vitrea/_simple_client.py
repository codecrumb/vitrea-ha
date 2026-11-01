"""Async client for the V-Box "simple protocol" (TCP 11502, no login).

Line based, CRLF terminated. Verified live against a V-Box:

    P:VITREA            -> S:PSW:OK            keepalive
    H:N041:G            -> OK, then S:N041:<key>:<code>[:<data>] per key
    H:NALL:G            -> OK, then the same for every node (0.6 s per line)
    H:N041:5:O:         -> OK, then S:N041:5:O         key on
    H:N041:5:F:000      -> OK, then S:N041:5:F         key off
    H:N041:1:B:090      -> OK, then S:N041:1:B:090...  blind to 90 %
    H:N041:1:B:255      -> OK                          blind stop
    H:R0010             -> S:R0010:OK                  scenario 10

Keys are 1-based here (the mobile protocol's key index + 1), nodes are
zero padded to three digits. A command the box rejects answers with
E:N<node>:<key>:<code> instead of OK (004 = wrong key type, 005 = no such
node). The box paces status lines at ~0.6 s each, so a full dump of a big
install takes minutes: discover once, then rely on push.

No Home Assistant imports: this module is plain asyncio.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 11502
KEEPALIVE_INTERVAL = 15.0
COMMAND_TIMEOUT = 5.0
CONNECT_TIMEOUT = 5.0
RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)
# The dump is paced at ~0.6 s per line; this much silence means it is done.
DISCOVERY_QUIET = 3.0
# Pending-command slot used for scenarios (they are not tied to a node).
SCENARIO_SLOT = -1

KIND_SWITCH = "switch"      # O / F
KIND_BOILER = "boiler"      # o / f
KIND_BLIND = "blind"        # B
KIND_DIMMER = "dimmer"      # D
KIND_BUTTON = "button"      # S / L / R: physical satellite key events

_CODE_KIND = {
    "O": KIND_SWITCH, "F": KIND_SWITCH,
    "o": KIND_BOILER, "f": KIND_BOILER,
    "B": KIND_BLIND,
    "D": KIND_DIMMER,
    "S": KIND_BUTTON, "L": KIND_BUTTON, "R": KIND_BUTTON,
}


class VitreaError(Exception):
    """The box rejected a command, or the connection is unusable."""


@dataclass(frozen=True)
class Status:
    node: int
    key: int
    code: str
    data: str | None = None

    @property
    def kind(self) -> str | None:
        return _CODE_KIND.get(self.code)

    @property
    def is_on(self) -> bool:
        return self.code in ("O", "o")

    @property
    def position(self) -> int | None:
        """Blind position 0-100, or None for other key kinds."""
        if self.code == "B" and self.data is not None:
            try:
                return int(self.data)
            except ValueError:
                return None
        return None


@dataclass(frozen=True)
class Error:
    node: int
    key: int
    code: str


@dataclass(frozen=True)
class ScenarioAck:
    scenario: int
    ok: bool


def parse_line(line: str):
    """One protocol line -> Status | Error | ScenarioAck | "OK" | "PSW" | None."""
    line = line.strip()
    if not line:
        return None
    if line == "OK":
        return "OK"
    if line.startswith("S:PSW:"):
        return "PSW"
    parts = line.split(":")
    try:
        if parts[0] == "S" and parts[1].startswith("N") and len(parts) >= 4:
            return Status(int(parts[1][1:]), int(parts[2]), parts[3], parts[4] if len(parts) > 4 else None)
        if parts[0] == "S" and parts[1].startswith("R") and len(parts) >= 3:
            return ScenarioAck(int(parts[1][1:]), parts[2] == "OK")
        if parts[0] == "E" and parts[1].startswith("N") and len(parts) >= 4:
            return Error(int(parts[1][1:]), int(parts[2]), parts[3])
    except ValueError:
        pass
    return None


class SimpleVBox:
    """Persistent connection to one V-Box on the simple-protocol port."""

    def __init__(self, host: str, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: list[asyncio.Task] = []
        self._write_lock = asyncio.Lock()
        # Every H: command is answered by exactly one OK or E: line, in
        # order, so a FIFO of waiters is enough to match replies.
        self._pending: deque[tuple[tuple[int, int], asyncio.Future]] = deque()
        self._ping_waiters: list[asyncio.Future] = []
        self._status_listeners: list[Callable[[Status], None]] = []
        self._state_listeners: list[Callable[[bool], None]] = []
        self._closing = False
        self.connected = False

    # -- listeners ------------------------------------------------------
    def add_status_listener(self, cb: Callable[[Status], None]) -> Callable[[], None]:
        self._status_listeners.append(cb)
        return lambda: self._status_listeners.remove(cb)

    def add_connection_listener(self, cb: Callable[[bool], None]) -> Callable[[], None]:
        """cb(True) after (re)connect, cb(False) when the link drops."""
        self._state_listeners.append(cb)
        return lambda: self._state_listeners.remove(cb)

    # -- lifecycle ------------------------------------------------------
    async def connect(self) -> None:
        """Open the link and keep it alive, reconnecting in the background."""
        self._closing = False
        await self._open()
        self._tasks.append(asyncio.create_task(self._keepalive_loop()))

    async def _open(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT
        )
        self._tasks = [t for t in self._tasks if not t.done()]
        self._tasks.append(asyncio.create_task(self._read_loop(self._reader)))
        # Prove the link answers before anyone trusts it. Not "connected"
        # (and no listener calls) until it does, so a box that accepts the
        # socket but stays mute doesn't flap availability on every retry.
        try:
            await self._ping()
        except BaseException:
            if self._writer is not None:
                self._writer.close()
            self._reader = self._writer = None
            raise
        self.connected = True
        for cb in list(self._state_listeners):
            cb(True)

    async def close(self) -> None:
        self._closing = True
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        # An intentional close is not a lost link: don't tell listeners.
        self._state_listeners.clear()
        self._drop(VitreaError("closed"))

    def _drop(self, exc: Exception) -> None:
        was = self.connected
        self.connected = False
        if self._writer is not None:
            self._writer.close()
        self._writer = self._reader = None
        while self._pending:
            _, fut = self._pending.popleft()
            if not fut.done():
                fut.set_exception(exc)
        if was:
            for cb in list(self._state_listeners):
                cb(False)

    async def _ping(self) -> None:
        fut = asyncio.get_running_loop().create_future()
        self._ping_waiters.append(fut)
        await self._write("P:VITREA")
        await asyncio.wait_for(fut, COMMAND_TIMEOUT)

    async def _keepalive_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            try:
                if not self.connected:
                    raise VitreaError("down")
                await self._ping()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("Vitrea link lost (%r), reconnecting", err)
                self._drop(VitreaError(str(err)))
                await self._reconnect()

    async def _reconnect(self) -> None:
        attempt = 0
        while not self._closing:
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            await asyncio.sleep(delay)
            try:
                await self._open()
                _LOGGER.info("Vitrea link re-established")
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:
                attempt += 1
                self._drop(VitreaError(str(err)))
                _LOGGER.debug("Vitrea reconnect attempt %d failed: %s", attempt, err)

    # -- io -------------------------------------------------------------
    async def _write(self, line: str) -> None:
        if self._writer is None:
            raise VitreaError("not connected")
        async with self._write_lock:
            self._writer.write((line + "\r\n").encode())
            await self._writer.drain()

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                self._handle(raw.decode(errors="replace"))
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("Vitrea read loop ended: %s", err)
        if reader is self._reader and not self._closing:
            # Let the keepalive loop notice and reconnect; fail waiters now.
            self._drop(VitreaError("connection closed by box"))

    def _handle(self, line: str) -> None:
        msg = parse_line(line)
        if msg is None:
            if line.strip():
                _LOGGER.debug("Vitrea: unparsed line %r", line.strip())
            return
        if msg == "PSW":
            while self._ping_waiters:
                fut = self._ping_waiters.pop(0)
                if not fut.done():
                    fut.set_result(None)
            return
        if msg == "OK":
            self._resolve_oldest(scenario=False)
            return
        if isinstance(msg, ScenarioAck):
            self._resolve_oldest(
                scenario=True,
                exc=None if msg.ok else VitreaError(f"scenario {msg.scenario} failed"),
            )
            return
        if isinstance(msg, Error):
            self._resolve_error(msg)
            return
        for cb in list(self._status_listeners):
            try:
                cb(msg)
            except Exception:
                _LOGGER.exception("Vitrea status listener failed")

    def _resolve_oldest(self, scenario: bool, exc: Exception | None = None) -> None:
        # A scenario is acked by S:R....:OK and never by a bare OK.
        for i, (slot, fut) in enumerate(self._pending):
            if (slot[0] == SCENARIO_SLOT) == scenario:
                del self._pending[i]
                if not fut.done():
                    if exc:
                        fut.set_exception(exc)
                    else:
                        fut.set_result(None)
                return

    def _resolve_error(self, err: Error) -> None:
        for i, (slot, fut) in enumerate(self._pending):
            if slot == (err.node, err.key):
                del self._pending[i]
                if not fut.done():
                    fut.set_exception(VitreaError(f"box rejected command for N{err.node:03d}:{err.key} (error {err.code})"))
                return
        # Node-level queries fail as E:N<node>:1:005 style lines.
        for i, (slot, fut) in enumerate(self._pending):
            if slot == (err.node, 0):
                del self._pending[i]
                if not fut.done():
                    fut.set_exception(VitreaError(f"box rejected query for N{err.node:03d} (error {err.code})"))
                return
        _LOGGER.debug("Vitrea: unmatched error N%03d:%d code %s", err.node, err.key, err.code)

    async def _command(self, line: str, slot: tuple[int, int], timeout: float = COMMAND_TIMEOUT) -> None:
        fut = asyncio.get_running_loop().create_future()
        entry = (slot, fut)
        self._pending.append(entry)
        try:
            await self._write(line)
            await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as err:
            raise VitreaError(f"no reply to {line!r}") from err
        finally:
            if entry in self._pending:
                self._pending.remove(entry)

    # -- commands -------------------------------------------------------
    async def key_on(self, node: int, key: int, timer: int = 0) -> None:
        tail = f"{timer:03d}" if timer else ""
        await self._command(f"H:N{node:03d}:{key}:O:{tail}", (node, key))

    async def key_off(self, node: int, key: int) -> None:
        await self._command(f"H:N{node:03d}:{key}:F:000", (node, key))

    async def blind_position(self, node: int, key: int, percent: int) -> None:
        percent = max(0, min(100, percent))
        await self._command(f"H:N{node:03d}:{key}:B:{percent:03d}", (node, key))

    async def blind_stop(self, node: int, key: int) -> None:
        await self._command(f"H:N{node:03d}:{key}:B:255", (node, key))

    async def dimmer(self, node: int, key: int, percent: int) -> None:
        """Set a dimmer level. UNVERIFIED on hardware: the format comes from
        the third-party `vitreaclient` package (`D:<intensity>:<minutes>`),
        and the one key tried live was a relay light, which answered 004.
        """
        percent = max(0, min(100, percent))
        await self._command(f"H:N{node:03d}:{key}:D:{percent:03d}:000", (node, key))

    async def scenario(self, number: int) -> None:
        """Run scenario `number`. The ID is `R` + 4 digits on the wire."""
        await self._command(f"H:R{number:04d}", (SCENARIO_SLOT, number))

    async def request_node(self, node: int) -> None:
        await self._command(f"H:N{node:03d}:G", (node, 0), timeout=15.0)

    async def request_all(self) -> None:
        await self._command("H:NALL:G", (0, 0), timeout=15.0)

    # -- discovery ------------------------------------------------------
    async def discover(self, progress: Callable[[int], None] | None = None) -> dict[tuple[int, int], Status]:
        """Dump every node and key. Slow by design (see module docstring)."""
        found: dict[tuple[int, int], Status] = {}
        last = asyncio.get_running_loop().time()

        def on_status(s: Status) -> None:
            nonlocal last
            last = asyncio.get_running_loop().time()
            if s.kind is not None:
                found[(s.node, s.key)] = s
                if progress:
                    progress(len(found))

        remove = self.add_status_listener(on_status)
        try:
            await self.request_all()
            while asyncio.get_running_loop().time() - last < DISCOVERY_QUIET:
                await asyncio.sleep(0.5)
        finally:
            remove()
        return found


async def probe(host: str, port: int = DEFAULT_PORT, timeout: float = 1.0) -> bool:
    """True if `host:port` answers the simple-protocol keepalive."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        writer.write(b"P:VITREA\r\n")
        await writer.drain()
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            line = await asyncio.wait_for(reader.readline(), remaining)
            if not line:
                return False
            if line.strip().startswith(b"S:PSW:"):
                return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        writer.close()


async def scan(network: str, port: int = DEFAULT_PORT, concurrency: int = 64) -> list[str]:
    """Find V-Boxes on a subnet, e.g. scan("192.168.1.0/24")."""
    hosts = [str(h) for h in ipaddress.ip_network(network, strict=False).hosts()]
    sem = asyncio.Semaphore(concurrency)

    async def one(h: str) -> str | None:
        async with sem:
            return h if await probe(h, port) else None

    return [h for h in await asyncio.gather(*(one(h) for h in hosts)) if h]
