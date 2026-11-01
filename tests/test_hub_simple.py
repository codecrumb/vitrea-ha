"""Offline checks for hub.py on the simple protocol (HA is stubbed, the V-Box
is the fake from test_simple_client).

Run: python3 tests/test_hub_simple.py   (or pytest)
"""
import asyncio
import importlib
import pathlib
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_simple_client import FakeBox  # noqa: E402

for n in ("homeassistant", "homeassistant.helpers", "homeassistant.helpers.event",
          "homeassistant.helpers.storage", "homeassistant.core", "homeassistant.exceptions"):
    sys.modules[n] = types.ModuleType(n)
sys.modules["homeassistant.exceptions"].HomeAssistantError = type("HomeAssistantError", (Exception,), {})
sys.modules["homeassistant.helpers.event"].async_track_time_interval = lambda *a, **k: (lambda: None)
_er = types.ModuleType("homeassistant.helpers.entity_registry")
_er.async_get = lambda hass: types.SimpleNamespace(entities={})
sys.modules["homeassistant.helpers.entity_registry"] = _er
sys.modules["homeassistant.helpers"].entity_registry = _er


class Store:
    data: dict = {}

    def __init__(self, hass, version, key):
        self.key = key

    async def async_load(self):
        return Store.data.get(self.key)

    async def async_save(self, value):
        Store.data[self.key] = value


sys.modules["homeassistant.helpers.storage"].Store = Store
pkg = types.ModuleType("vitrea")
pkg.__path__ = [str(HERE.parent / "custom_components" / "vitrea")]
sys.modules["vitrea"] = pkg
hub_mod = importlib.import_module("vitrea.hub")
simple = importlib.import_module("vitrea._simple_client")
simple.DISCOVERY_QUIET = 0.3
HomeAssistantError = sys.modules["homeassistant.exceptions"].HomeAssistantError


class Hass:
    def __init__(self):
        self.tasks = []
        self.config_entries = types.SimpleNamespace(async_get_entry=lambda _id: None)

    async def async_add_executor_job(self, fn, *a):
        return fn(*a)

    def async_create_background_task(self, coro, name):
        t = asyncio.create_task(coro)
        self.tasks.append(t)
        return t


class Entity:
    """Stands in for a platform entity: records pushed state."""

    def __init__(self):
        self.hass = object()
        self.pushed = []
        self.writes = 0

    def on_push_state(self, is_on, ratio):
        self.pushed.append((is_on, ratio))

    def async_write_ha_state(self):
        self.writes += 1


async def make(box, options=None, data=None):
    hass = Hass()
    h = hub_mod.VitreaHub(
        hass, {"host": "127.0.0.1", "simple_port": box.port, **(data or {})}, options or {}, None
    )
    await h.async_setup()
    for _ in range(200):
        if h.metadata_ready:
            break
        await asyncio.sleep(0.05)
    assert h.metadata_ready
    await asyncio.sleep(0.3)  # let the startup state refresh finish arriving
    return h


def run(coro):
    Store.data = {}
    return asyncio.run(asyncio.wait_for(coro, 30))


def test_discovery_builds_generic_entities():
    async def go():
        box = FakeBox()
        await box.start()
        h = await make(box)
        assert not h.has_mobile
        by = {(k["node"], k["key"]): k for k in h.keys}
        # generic keys are addressed by their simple-protocol key
        assert set(by) == {(1, "s1"), (1, "s2"), (2, "s1")}
        assert by[(1, "s1")]["ha_type"] == "switch" and by[(1, "s2")]["ha_type"] == "cover"
        assert by[(1, "s1")]["name"] == "Node 1 key 1"
        assert h.scenarios == []
        # the dump also filled the state cache
        assert h._states[(2, 1)].is_on is True
        assert h._states[(1, 2)] == hub_mod.KeyState(False, 100)
        assert (await h.async_get_key_status(2, "s1")).is_on
        await h.async_unload()
        await box.stop()
    run(go())


def test_commands_and_push_reach_entities():
    async def go():
        box = FakeBox()
        await box.start()
        h = await make(box)
        sw, cover = Entity(), Entity()
        h.register_entity(1, "s1", sw)
        h.register_entity(1, "s2", cover)
        await h.async_key_on(1, "s1")
        await h.async_set_cover_position(1, "s2", 90)
        await h.async_trigger_scenario(10)
        await h.async_stop_cover(1, "s2")
        await h.async_key_off(1, "s1")
        await asyncio.sleep(0.2)
        assert "H:N001:1:O:" in box.log and "H:N001:1:F:000" in box.log
        assert "H:N001:2:B:090" in box.log and "H:N001:2:B:255" in box.log
        assert "H:R0010" in box.log
        assert sw.pushed == [(True, 100), (False, 0)], sw.pushed
        assert cover.pushed == [(False, 90)], cover.pushed
        # a rejected command surfaces as a Home Assistant error
        try:
            await h.async_set_cover_position(1, "s1", 50)  # key 1 is a switch
        except HomeAssistantError as err:
            assert "004" in str(err)
        else:
            raise AssertionError("expected HomeAssistantError")
        await h.async_unload()
        await box.stop()
    run(go())


def test_device_list_is_cached_and_excluded_keys_apply():
    async def go():
        box = FakeBox()
        await box.start()
        h = await make(box)
        first = [k for k in box.log if k == "H:NALL:G"]
        await h.async_unload()
        h2 = await make(box, options={"excluded_keys": "1:s2"})
        # second run: no discovery dump, only the one state refresh
        assert len([k for k in box.log if k == "H:NALL:G"]) == len(first) + 1
        assert {(k["node"], k["key"]) for k in h2.keys} == {(1, "s1"), (2, "s1")}
        await h2.async_unload()
        await box.stop()
    run(go())


def test_status_mapping():
    async def go():
        box = FakeBox()
        await box.start()
        h = await make(box)
        S = simple.Status
        h._on_status(S(5, 3, "S"))  # satellite/button events are not state
        assert (5, 3) not in h._states
        h._on_status(S(5, 1, "D", "040"))
        assert h._states[(5, 1)] == hub_mod.KeyState(True, 40)
        h._on_status(S(5, 1, "O"))  # plain on keeps the known level
        assert h._states[(5, 1)] == hub_mod.KeyState(True, 40)
        h._on_status(S(5, 1, "F"))
        assert h._states[(5, 1)] == hub_mod.KeyState(False, 0)
        h._on_status(S(5, 2, "B", "071"))
        assert h._states[(5, 2)] == hub_mod.KeyState(False, 71)
        await h.async_unload()
        await box.stop()
    run(go())


def test_link_loss_marks_unavailable_then_recovers():
    async def go():
        simple.KEEPALIVE_INTERVAL = 0.2
        simple.RECONNECT_DELAYS = (0.1,)
        simple.COMMAND_TIMEOUT = 0.3
        box = FakeBox()
        await box.start()
        h = await make(box)
        e = Entity()
        h.register_entity(1, "s1", e)
        box.mute = True
        await asyncio.sleep(1.0)
        assert h.available is False and e.writes >= 1
        box.mute = False
        await asyncio.sleep(1.0)
        assert h.available is True
        await h.async_key_on(1, "s1")
        await h.async_unload()
        await box.stop()
    run(go())


def _mk(node, key, ha_type, name="x"):
    return {"node": node, "key": key, "name": name, "ha_type": ha_type, "dimmable": False, "room": ""}


def _raw(node, key, code):
    return {"node": node, "key": key, "code": code}


def test_mobile_names_only_attach_when_pairing_is_certain():
    async def go():
        box = FakeBox()
        await box.start()
        h = await make(box)
        mobile = [
            # node 7: same count and compatible kinds -> paired in order
            _mk(7, 0, "light", "A"), _mk(7, 1, "cover", "B"),
            # node 41: Blind, Study, Main, Night but the box lists 7 keys
            # (aliases and a key with no mobile slot) -> must NOT pair
            _mk(41, 0, "cover", "Matan Blind"), _mk(41, 1, "light", "Matan Study"),
            _mk(41, 2, "light", "Matan Main"), _mk(41, 3, "light", "Matan Night"),
            # node 9: equal count but a cover paired with a plain key
            _mk(9, 0, "cover", "C"),
            # node 99: named by the box but never reported by it
            _mk(99, 0, "light", "Ghost"),
        ]
        raw = [
            _raw(7, 1, "O"), _raw(7, 3, "B"),
            _raw(41, 1, "B"), _raw(41, 3, "F"), _raw(41, 4, "F"), _raw(41, 5, "F"),
            _raw(41, 6, "F"), _raw(41, 7, "O"), _raw(41, 8, "F"),
            _raw(9, 1, "F"),
        ]
        out = h._merge_keys(mobile, raw)
        by = {(k["node"], k["key"]): k for k in out}
        # paired: keeps the mobile name and unique-id key, controls the right simple key
        assert by[(7, 0)]["name"] == "A" and by[(7, 0)]["simple_key"] == 1
        assert by[(7, 1)]["name"] == "B" and by[(7, 1)]["simple_key"] == 3
        # node 41: generic names, every key reachable by its own simple number
        assert {k["simple_key"] for k in out if k["node"] == 41} == {1, 3, 4, 5, 6, 7, 8}
        assert all(k["name"].startswith("Node 41 key") for k in out if k["node"] == 41)
        assert (41, 2) not in by  # the old mobile-indexed "Matan Main" is gone
        # incompatible kinds: generic
        assert by[(9, "s1")]["ha_type"] == "switch" and (9, 0) not in by
        assert not any(k["node"] == 99 for k in out)
        await h.async_unload()
        await box.stop()
    run(go())


def test_commands_use_the_simple_key_not_the_mobile_index():
    async def go():
        box = FakeBox()
        await box.start()
        h = await make(box)
        # a paired key whose mobile index (3) differs from its simple key (7)
        h._skey[(1, 3)] = 1
        h._skey[(2, 5)] = 1
        await h.async_key_on(1, 3)
        await h.async_key_on(2, 5)
        assert "H:N001:1:O:" in box.log and "H:N002:1:O:" in box.log
        try:
            await h.async_key_on(1, 99)
        except HomeAssistantError:
            pass
        else:
            raise AssertionError("an unknown key must not be sent anywhere")
        await h.async_unload()
        await box.stop()
    run(go())


def test_poll_waits_while_a_dump_is_streaming():
    async def go():
        box = FakeBox()
        await box.start()
        h = await make(box)
        h.register_entity(1, "s1", Entity())
        before = len(box.log)
        h._last_status = hub_mod.time.monotonic()  # a status line just arrived
        h._last_command = 0.0
        await h._async_poll_tick(None)
        assert len(box.log) == before
        h._last_status = 0.0
        await h._async_poll_tick(None)
        await asyncio.sleep(0.1)
        assert any(line.startswith("H:N") and line.endswith(":G") for line in box.log[before:])
        await h.async_unload()
        await box.stop()
    run(go())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
