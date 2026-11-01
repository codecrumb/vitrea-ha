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
admin_mod = importlib.import_module("vitrea._admin_client")
hub_mod.REFRESH_QUIET = 0.1
hub_mod.REFRESH_GAP = 0.0


class _AdminRejected:
    """Default stand-in for the admin protocol: the login is refused, so the
    hub falls back to the other sources of names."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        raise admin_mod.AdminAuthError("rejected")

    def __exit__(self, *exc):
        return False


hub_mod.AdminVBox = _AdminRejected
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
        # second run: no discovery dump, only per-node state refreshes
        assert len([k for k in box.log if k == "H:NALL:G"]) == len(first)
        assert "H:N001:G" in box.log and "H:N002:G" in box.log
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


ADMIN_DATA = {
    "rooms": {"1": "Kitchen", "2": "Living"},
    "categories": {"1": "Light", "2": "Dimmer", "3": "Blind", "6": "Fan", "4": "AirConditioning"},
    "nodes": {
        "1": {"node": 1, "room_no": 1, "num_keys": 2, "keys": [
            {"node": 1, "key": 0, "class": 255, "category": 1, "room_no": 1, "name": "Lamp"},
            {"node": 1, "key": 1, "class": 255, "category": 3, "room_no": 1, "name": "Shade"},
        ]},
        "2": {"node": 2, "room_no": 2, "num_keys": 7, "keys": [
            {"node": 2, "key": 0, "class": 255, "category": 3, "room_no": 2, "name": "Window"},
            {"node": 2, "key": 1, "class": 255, "category": 3, "room_no": 2, "name": "N2-1 Pair"},
            {"node": 2, "key": 2, "class": 255, "category": 1, "room_no": 2, "name": "Study"},
            {"node": 2, "key": 3, "class": 0, "category": 1, "room_no": 0, "name": "Hall Sconces MW"},
            {"node": 2, "key": 4, "class": 0, "category": 6, "room_no": 2, "name": "Ceiling Fan "},
            {"node": 2, "key": 5, "class": 255, "category": 1, "room_no": 2, "name": "Main"},
            {"node": 2, "key": 6, "class": 0, "category": 4, "room_no": 2, "name": "Split AC"},
        ]},
    },
    "scenarios": [{"id": 10, "name": "Night light"}],
}


def _fake_admin(data, calls=None, error=None):
    class FakeAdmin:
        def __init__(self, host, port=11501, user="admin", password="admin", **k):
            if calls is not None:
                calls.append((host, port, user, password))

        def __enter__(self):
            if error is not None:
                raise error
            return self

        def __exit__(self, *exc):
            return False

        def read_device_list(self):
            return data

    return FakeAdmin


def test_admin_list_gives_exact_names_keys_and_keeps_old_ids():
    async def go():
        box = FakeBox()
        await box.start()
        calls = []
        hub_mod.AdminVBox = _fake_admin(ADMIN_DATA, calls)
        try:
            h = await make(box)
        finally:
            hub_mod.AdminVBox = _AdminRejected
        assert calls and calls[0][2:] == ("admin", "admin")  # the default login, nobody typed it
        by = {(k["node"], k["key"]): k for k in h.keys}
        # physical keys keep their legacy id (rank among physical keys, pair counted)
        assert by[(1, 0)]["name"] == "Lamp" and by[(1, 0)]["simple_key"] == 1 and by[(1, 0)]["ha_type"] == "light"
        assert by[(1, 1)]["ha_type"] == "cover" and by[(1, 1)]["simple_key"] == 2
        assert by[(2, 0)]["name"] == "Window" and by[(2, 0)]["simple_key"] == 1
        assert by[(2, 2)]["name"] == "Study" and by[(2, 2)]["simple_key"] == 3
        assert by[(2, 3)]["name"] == "Main" and by[(2, 3)]["simple_key"] == 6
        # a key with no mobile slot (the fan) is included, addressed by its simple key
        assert by[(2, "s5")]["ha_type"] == "fan" and by[(2, "s5")]["name"] == "Ceiling Fan"
        # aliases, pair keys and AC keys are not entities
        names = {k["name"] for k in h.keys}
        assert not any(n in names for n in ("Hall Sconces MW", "N2-1 Pair", "Split AC"))
        assert by[(2, 3)]["room"] == "Living" and by[(1, 0)]["room"] == "Kitchen"
        assert h.scenarios == [{"id": 10, "name": "Night light", "listed": True, "has_status": False}]
        # no discovery dump at all; states come from per-node queries
        assert "H:NALL:G" not in box.log
        assert "H:N001:G" in box.log and "H:N002:G" in box.log
        # commands use the admin-derived simple key
        await h.async_key_on(2, 3)
        await h.async_key_on(2, "s5")
        assert "H:N002:6:O:" in box.log and "H:N002:5:O:" in box.log
        await h.async_unload()
        await box.stop()
    run(go())


def test_admin_unavailable_falls_back_to_generic_names():
    async def go():
        for err in (admin_mod.AdminInUseError("busy"), admin_mod.AdminAuthError("bad"), OSError("refused")):
            Store.data = {}
            box = FakeBox()
            await box.start()
            hub_mod.AdminVBox = _fake_admin(ADMIN_DATA, error=err)
            try:
                h = await make(box)
            finally:
                hub_mod.AdminVBox = _AdminRejected
            assert {(k["node"], k["key"]) for k in h.keys} == {(1, "s1"), (1, "s2"), (2, "s1")}
            await h.async_unload()
            await box.stop()
    run(go())


def test_admin_list_is_cached_between_starts():
    async def go():
        box = FakeBox()
        await box.start()
        calls = []
        hub_mod.AdminVBox = _fake_admin(ADMIN_DATA, calls)
        try:
            h = await make(box)
            await h.async_unload()
            h2 = await make(box)  # restart: the cached list is used
            assert len(calls) == 1
            assert {k["name"] for k in h2.keys} >= {"Lamp", "Main", "Ceiling Fan"}
            await h2.async_unload()
            h3 = await make(box, options={"disable_metadata_cache": True})
            assert len(calls) == 2  # the rescan toggle reads it again
            await h3.async_unload()
        finally:
            hub_mod.AdminVBox = _AdminRejected
        await box.stop()
    run(go())


def test_admin_login_from_options_is_used():
    async def go():
        box = FakeBox()
        await box.start()
        calls = []
        hub_mod.AdminVBox = _fake_admin(ADMIN_DATA, calls)
        try:
            h = await make(box, options={"admin_user": "installer", "admin_password": "s3cret"})
        finally:
            hub_mod.AdminVBox = _AdminRejected
        assert calls[0][2:] == ("installer", "s3cret")
        await h.async_unload()
        await box.stop()
    run(go())


def test_refresh_lets_commands_go_first():
    async def go():
        box = FakeBox()
        await box.start()
        h = await make(box)
        before = len(box.log)
        h._last_command = hub_mod.time.monotonic()  # a command just went out
        t = asyncio.create_task(h._async_refresh_states())
        await asyncio.sleep(0.3)
        assert not any(l.endswith(":G") for l in box.log[before:])  # waiting for the command window
        t.cancel()
        await h.async_unload()
        await box.stop()
    run(go())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
