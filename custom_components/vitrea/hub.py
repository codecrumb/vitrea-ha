from __future__ import annotations
import asyncio
import logging
import re
import threading
import time
from datetime import timedelta
from typing import TYPE_CHECKING, NamedTuple

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from ._node_types import node_category
from ._admin_client import CLASS_PHYSICAL, AdminAuthError, AdminInUseError, AdminVBox
from ._mobile_client import MobileVBox, MobileVBoxAuthError
from ._simple_client import (
    DEFAULT_PORT as SIMPLE_PORT,
    KIND_BLIND,
    KIND_BOILER,
    KIND_BUTTON,
    KIND_DIMMER,
    KIND_SWITCH,
    SimpleVBox,
    Status,
    VitreaError,
)
from .const import (
    DOMAIN,
    CONF_ADMIN_PASSWORD,
    CONF_ADMIN_USER,
    CONF_MOBILE_PASSWORD,
    CONF_MOBILE_PORT,
    CONF_MOBILE_USER,
    CONF_DISABLE_METADATA_CACHE,
    CONF_EXCLUDED_KEYS,
    CONF_SIMPLE_PORT,
    CONF_USE_ROOMS_AS_AREAS,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_PORT,
    DEFAULT_ADMIN_USER,
    DEFAULT_DISABLE_METADATA_CACHE,
    DEFAULT_EXCLUDED_KEYS,
    DEFAULT_MOBILE_PORT,
    DEFAULT_MOBILE_USER,
    DEFAULT_USE_ROOMS_AS_AREAS,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# get_key_params "category" byte -> HA platform, for anything not caught
# by the dimmer/toggle/blind special cases below.
_KEY_CAT_HA: dict[int, str] = {
    2: "switch",   # Toggle
    3: "cover",    # Blind
    4: "switch",   # PushButton
    5: "light",    # Dimmer
    10: "light",   # DimmerMW
    11: "cover",   # BlindMW
    13: "switch",  # ToggleScene
    14: "switch",  # DualPole
    15: "switch",  # Boiler
    18: "fan",     # Fan
}
# Live control and push state run over the V-Box's simple protocol (port
# 11502, no login, see _simple_client). The mobile protocol (11503) is only
# used, optionally, to read names/rooms/scenes with a short login that is
# closed again: the box allows one session per mobile account, so a
# persistent session would fight the phone app (and it drops sessions
# at random, which the simple protocol does not).
#
# Push is the primary source of truth. Safety net: re-read one node per
# tick (the box answers status queries at ~0.6 s per key, so a full read
# is slow and is only done once per (re)connect).
POLL_INTERVAL = timedelta(seconds=60)
POLL_QUIET_AFTER_COMMAND = 10.0
SAVE_STATES_EVERY = 10  # poll ticks
METADATA_CACHE_VERSION = 2
DISCOVERY_CACHE_VERSION = 1
ADMIN_CACHE_VERSION = 1
# After a node query the box streams its keys at ~0.6 s each; this much
# silence means the node is done. Refreshing node by node, with gaps, keeps
# the box free for commands (one full dump would block it for minutes).
REFRESH_QUIET = 2.0
REFRESH_NODE_MAX = 20.0
REFRESH_GAP = 1.0

# What the box calls a key (its category icon) -> Home Assistant platform.
# Air conditioning and heating keys are not supported and are skipped.
_ICON_HA: dict[str, str] = {
    "Light": "light", "Dimmer": "light",
    "Blind": "cover", "Curtain": "cover",
    "Fan": "fan",
    "Boiler": "switch", "Undefined": "switch", "Scenario": "switch",
    "Audio": "switch", "TV": "switch", "Alarm": "switch",
}
_DEFAULT_KEY_NAME = re.compile(r"^N\d+-\d+$")
METADATA_PACE = 0.1
METADATA_RELOGIN_EVERY = 60


def _parse_excluded_keys(raw: str) -> set[tuple[int, int | str]]:
    """Parse the `excluded_keys` option: "node:key,node:key", 0-indexed.

    Per-install exclusions (e.g. a key that turns out to cross-push the
    same physical circuit as another one on your setup) belong here, in
    config, not hardcoded in this file — every install's wiring quirks
    are its own.
    """
    out: set[tuple[int, int]] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            node_s, key_s = part.split(":", 1)
            key_s = key_s.strip()
            out.add((int(node_s), int(key_s) if key_s.isdigit() else key_s))
        except ValueError:
            _LOGGER.warning("Vitrea: ignoring malformed excluded_keys entry %r", part)
    return out


class KeyState(NamedTuple):
    is_on: bool
    ratio: int


class VitreaHub:
    """One V-Box: live control/push over the simple protocol, plus device
    metadata from either the simple-protocol discovery dump (generic names)
    or, when mobile credentials are given, the mobile protocol (names,
    rooms, scenes).
    """

    def __init__(self, hass: "HomeAssistant", data: dict, options: dict | None = None,
                 entry_id: str | None = None) -> None:
        self.hass = hass
        self.entry_id = entry_id
        options = options or {}
        self._host = data["host"]
        self._simple_port = data.get(CONF_SIMPLE_PORT) or SIMPLE_PORT
        # Credentials set in the options form win over the ones from setup.
        self._mobile_port = data.get(CONF_MOBILE_PORT) or DEFAULT_MOBILE_PORT
        self._mobile_user = (
            options.get(CONF_MOBILE_USER) or data.get(CONF_MOBILE_USER) or DEFAULT_MOBILE_USER
        )
        self._mobile_password = options.get(CONF_MOBILE_PASSWORD) or data.get(CONF_MOBILE_PASSWORD) or ""
        # Admin login for the one-time device-list read. Nobody has to enter
        # it: the vendor default is tried, and any failure just falls back.
        self._admin_user = options.get(CONF_ADMIN_USER) or data.get(CONF_ADMIN_USER) or DEFAULT_ADMIN_USER
        self._admin_password = (
            options.get(CONF_ADMIN_PASSWORD) or data.get(CONF_ADMIN_PASSWORD) or DEFAULT_ADMIN_PASSWORD
        )
        self._use_areas = options.get(CONF_USE_ROOMS_AS_AREAS, DEFAULT_USE_ROOMS_AS_AREAS)
        self._disable_metadata_cache = options.get(
            CONF_DISABLE_METADATA_CACHE, DEFAULT_DISABLE_METADATA_CACHE
        )
        self._excluded_keys = _parse_excluded_keys(
            options.get(CONF_EXCLUDED_KEYS, DEFAULT_EXCLUDED_KEYS)
        )
        self.keys: list[dict] = []
        self.scenarios: list[dict] = []
        self.available = False
        self.metadata_ready = False
        self.controller_version: str | None = None
        self.scenario_enabled: dict[int, bool] = {}
        self._simple: SimpleVBox | None = None
        self._mobile: MobileVBox | None = None
        self._mobile_lock = threading.Lock()
        # Everything live is keyed by (node, simple-protocol key). That
        # protocol numbers every key on a node (MW aliases and keys with no
        # mobile slot included), starting at 1; the mobile protocol numbers
        # only the physical keys, so the two are NOT offset by a constant.
        # `_skey` maps an entity's own (node, key["key"]) to its simple key.
        self._entities: dict[tuple[int, int], object] = {}
        self._skey: dict[tuple[int, object], int] = {}
        self._scenario_entities: dict[int, object] = {}
        self._states: dict[tuple[int, int], KeyState] = {}
        self._poll_unsub = None
        self._poll_index = 0
        self._poll_ticks = 0
        self._last_command = 0.0
        self._last_status = 0.0
        self._force_rediscover = False
        self._just_discovered = False
        self._last_activity = time.monotonic()
        self._meta_count = 0
        self._metadata_listeners: list = []
        self._store = Store(hass, 1, "vitrea_metadata")
        self._discovery_store = Store(hass, 1, "vitrea_simple_keys")
        self._state_store = Store(hass, 1, "vitrea_simple_states")
        self._admin_store = Store(hass, 1, "vitrea_admin")

    @property
    def has_mobile(self) -> bool:
        """True when mobile credentials were given (names, rooms, scenes)."""
        return bool(self._mobile_password)

    async def async_setup(self) -> None:
        self._simple = SimpleVBox(self._host, self._simple_port)
        self._simple.add_status_listener(self._on_status)
        self._simple.add_connection_listener(self._on_link)
        # Raises if the box is unreachable, so setup is retried by HA.
        await self._simple.connect()
        self.available = True
        try:
            saved = await self._state_store.async_load()
        except Exception:
            saved = None
        for slot, (on, ratio) in ((saved or {}).get("states") or {}).items():
            node, key = slot.split(",")
            self._states[(int(node), int(key))] = KeyState(bool(on), int(ratio))
        self._poll_unsub = async_track_time_interval(
            self.hass, self._async_poll_tick, POLL_INTERVAL
        )
        # Discovery can take minutes on a big install, so it must not block
        # config-entry setup: platforms add entities via the listeners below.
        self.hass.async_create_background_task(self._async_load(), "vitrea_load")

    async def async_unload(self) -> None:
        if self._poll_unsub is not None:
            self._poll_unsub()
            self._poll_unsub = None
        await self._async_save_states()
        if self._simple is not None:
            await self._simple.close()

    def register_entity(self, node: int, key, entity) -> None:
        skey = self._skey.get((node, key))
        if skey is not None:
            self._entities[(node, skey)] = entity

    def _simple_key(self, node: int, key) -> int:
        skey = self._skey.get((node, key))
        if skey is None:
            raise HomeAssistantError(f"Vitrea V-Box: unknown key {key} on node {node}")
        return skey

    def add_metadata_listener(self, callback) -> None:
        """Call `callback()` once the device list (self.keys/scenarios) is
        ready. Each platform's async_setup_entry registers one of these
        instead of reading hub.keys synchronously: discovery may still be
        running when platforms are set up. Calls back immediately if the
        list is already ready.
        """
        if self.metadata_ready:
            callback()
        else:
            self._metadata_listeners.append(callback)

    # ---------------------------------------------------------------
    # Device list: mobile metadata when credentials exist, else the
    # simple-protocol discovery dump.
    # ---------------------------------------------------------------
    async def _async_load(self) -> None:
        # 1. The box's own device list over the admin protocol: exact names,
        #    rooms, key numbers and scenarios, no discovery dump needed.
        keys, scenarios = await self._async_admin_metadata()
        if not keys:
            # 2. Names from the mobile login where the pairing is certain,
            # 3. else generic names, both on top of the simple-protocol dump.
            mobile: list[dict] = []
            if self.has_mobile:
                mobile, scenarios = await self._async_mobile_metadata()
            raw: list[dict] = []
            for attempt in range(3):
                raw = await self._async_discovered_raw()
                if raw:
                    break
                if attempt < 2:
                    await asyncio.sleep(30)
            keys = self._merge_keys(mobile, raw)
        keys = [k for k in keys if (k["node"], k["key"]) not in self._excluded_keys]
        self.keys, self.scenarios = keys, scenarios
        self._skey = {(k["node"], k["key"]): k["simple_key"] for k in keys}
        try:
            self._async_migrate_unique_ids()
        except Exception:
            _LOGGER.exception("Vitrea unique-id migration failed")
        try:
            self._async_apply_rooms_as_areas()
        except Exception:
            _LOGGER.exception("Vitrea: assigning rooms as areas failed")
        self.metadata_ready = True
        for callback in self._metadata_listeners:
            try:
                callback()
            except Exception:
                _LOGGER.exception("Vitrea metadata listener failed")
        self._metadata_listeners.clear()
        if not self._just_discovered:
            await self._async_refresh_states()

    def _read_admin(self) -> dict:
        with AdminVBox(
            self._host, port=DEFAULT_ADMIN_PORT, user=self._admin_user, password=self._admin_password
        ) as admin:
            return admin.read_device_list()

    async def _async_admin_metadata(self) -> tuple[list[dict], list[dict]]:
        """Entities from the box's own device list, or ([], []) if it is not
        available (wrong password, session held by another client such as
        Control4, firmware without it). The result is cached: a read takes
        seconds, but there is no reason to take the admin session at every
        start."""
        data = None
        if not self._disable_metadata_cache:
            try:
                cached = await self._admin_store.async_load()
            except Exception:
                cached = None
            if cached and cached.get("version") == ADMIN_CACHE_VERSION and cached.get("data"):
                data = cached["data"]
        if data is None:
            try:
                data = await self.hass.async_add_executor_job(self._read_admin)
            except AdminInUseError:
                _LOGGER.info("Vitrea: the admin session is in use by another client; using other sources of names")
                return [], []
            except AdminAuthError:
                _LOGGER.info(
                    "Vitrea: the admin login was rejected (default admin/admin); set it in the options "
                    "to get exact names. Using other sources of names"
                )
                return [], []
            except Exception as err:
                _LOGGER.info("Vitrea: could not read the device list over the admin protocol (%s)", err)
                return [], []
            try:
                await self._admin_store.async_save({"version": ADMIN_CACHE_VERSION, "data": data})
            except Exception:
                _LOGGER.debug("Vitrea: saving the admin device list failed", exc_info=True)
        keys, scenarios = self._keys_from_admin(data)
        _LOGGER.info("Vitrea: device list from the box: %d keys, %d scenarios", len(keys), len(scenarios))
        return keys, scenarios

    @staticmethod
    def _keys_from_admin(data: dict) -> tuple[list[dict], list[dict]]:
        """Entities from an admin device list.

        The simple protocol's key number is the admin key index plus one.
        Physical keys (class 255) keep the unique-id index the mobile
        protocol gave them (their rank among the node's physical keys), so
        existing entities, renames and areas carry over; keys with no mobile
        slot (class 0, for example a fan) are named by their simple key.
        Multi-way aliases and pair keys duplicate another key and are skipped.
        """
        rooms = {int(k): v for k, v in data["rooms"].items()}
        icons = {int(k): v for k, v in data["categories"].items()}
        keys: list[dict] = []
        for node_s, nd in sorted(data["nodes"].items(), key=lambda kv: int(kv[0])):
            node = int(node_s)
            rank = -1
            for k in sorted(nd["keys"], key=lambda k: k["key"]):
                if k["class"] == CLASS_PHYSICAL:
                    rank += 1
                name = (k.get("name") or "").strip()
                lname = name.lower()
                if lname.endswith("mw") or " pair" in lname or _DEFAULT_KEY_NAME.match(name):
                    continue
                icon = icons.get(k["category"], "Undefined")
                ha_type = _ICON_HA.get(icon)
                if ha_type is None:
                    continue
                skey = k["key"] + 1
                keys.append({
                    "node": node,
                    "key": rank if k["class"] == CLASS_PHYSICAL else f"s{skey}",
                    "simple_key": skey,
                    "name": name or f"Node {node} key {skey}",
                    "ha_type": ha_type,
                    "dimmable": icon == "Dimmer",
                    "room": rooms.get(k["room_no"]) or rooms.get(nd.get("room_no"), ""),
                })
        scenarios = [
            {"id": sc["id"], "name": sc["name"], "listed": True, "has_status": False}
            for sc in data.get("scenarios", [])
        ]
        return keys, scenarios

    async def _async_mobile_metadata(self) -> tuple[list[dict], list[dict]]:
        # The full scan only needs to run when the controller's config
        # changed: opcode 3 returns the last-change timestamp, which is
        # the cache key.
        cached = None
        if not self._disable_metadata_cache:
            try:
                cached = await self._store.async_load()
            except Exception:
                _LOGGER.exception("Vitrea: metadata cache unreadable")
        valid = bool(
            cached and cached.get("version") == METADATA_CACHE_VERSION and cached.get("keys")
        )

        def work():
            stamp = None
            try:
                stamp = self._mobile.get_last_change_timestamp().hex()
            except Exception as err:
                _LOGGER.warning("Vitrea: could not read config timestamp: %s", err)
            if valid and (stamp is None or cached.get("stamp") == stamp):
                try:
                    self.scenario_enabled.update(self._mobile.get_scenario_statuses())
                except Exception as err:
                    _LOGGER.debug("Vitrea: scenario status read failed: %s", err)
                return cached["keys"], cached["scenarios"], stamp, False
            keys, scenarios = self._fetch_metadata()
            return keys, scenarios, stamp, True

        try:
            keys, scenarios, stamp, fresh = await self.hass.async_add_executor_job(
                self._mobile_job, work
            )
        except MobileVBoxAuthError as err:
            _LOGGER.error("Vitrea: V-Box rejected the mobile login (%s); using generic names", err)
            self._start_reauth()
            return [], []
        except Exception as err:
            _LOGGER.error("Vitrea: reading names from the V-Box failed: %s", err)
            if valid:
                return cached["keys"], cached["scenarios"]
            return [], []
        if fresh:
            # The box's configuration changed, so the cached key list is stale too.
            self._force_rediscover = True
        if fresh and keys and stamp is not None:
            await self._store.async_save({
                "version": METADATA_CACHE_VERSION, "stamp": stamp,
                "keys": keys, "scenarios": scenarios,
            })
        return keys, scenarios

    async def _async_discovered_raw(self) -> list[dict]:
        """Every key the box reports over the simple protocol, as
        {"node", "key", "code"}. Cached: the dump is slow."""
        if not (self._disable_metadata_cache or self._force_rediscover):
            try:
                cached = await self._discovery_store.async_load()
            except Exception:
                cached = None
            if cached and cached.get("version") == DISCOVERY_CACHE_VERSION and cached.get("keys"):
                _LOGGER.info("Vitrea: using cached device list (%d keys)", len(cached["keys"]))
                return cached["keys"]
        _LOGGER.info("Vitrea: discovering devices (a few minutes on a large install)")
        try:
            found = await self._simple.discover()
        except Exception as err:
            _LOGGER.error("Vitrea: device discovery failed: %s", err)
            return []
        raw = [{"node": n, "key": k, "code": st.code} for (n, k), st in sorted(found.items())]
        _LOGGER.info("Vitrea: discovered %d keys on %d nodes", len(raw), len({r["node"] for r in raw}))
        if raw:
            self._just_discovered = True
            await self._discovery_store.async_save({"version": DISCOVERY_CACHE_VERSION, "keys": raw})
        return raw

    @staticmethod
    def _generic_key(st: Status) -> dict | None:
        """A discovered key as a generic entity. The simple protocol has no
        names, rooms or light-vs-switch distinction, so on/off keys are
        switches (HA can show a switch as a light) and blinds are covers."""
        ha_type = {
            KIND_SWITCH: "switch", KIND_BOILER: "switch",
            KIND_BLIND: "cover", KIND_DIMMER: "light",
        }.get(st.kind)
        if ha_type is None:
            return None
        return {
            "node": st.node,
            "key": f"s{st.key}",
            "simple_key": st.key,
            "name": f"Node {st.node} key {st.key}",
            "ha_type": ha_type,
            "dimmable": st.kind == KIND_DIMMER,
            "room": "",
        }

    @staticmethod
    def _compatible(m: dict, st: Status) -> bool:
        if m["ha_type"] == "cover":
            return st.kind == KIND_BLIND
        return st.kind in (KIND_SWITCH, KIND_BOILER, KIND_DIMMER)

    def _merge_keys(self, mobile: list[dict], raw: list[dict]) -> list[dict]:
        """Entities for every discovered key, named from the mobile metadata
        where that is safe.

        The mobile protocol lists only physical keys, in order; the simple
        protocol lists every key on the node. The two can only be paired
        when the node has the same number of keys in both and each pair
        has a compatible kind: then the order-preserving pairing is the
        only possible one. Anywhere else guessing would send commands to
        the wrong circuit, so those keys get generic names (and correct
        control) instead.
        """
        simple: dict[int, list[Status]] = {}
        for r in raw:
            simple.setdefault(r["node"], []).append(Status(r["node"], r["key"], r["code"]))
        named: dict[int, list[dict]] = {}
        for m in mobile:
            named.setdefault(m["node"], []).append(m)
        out: list[dict] = []
        generic_nodes: list[int] = []
        for node in sorted(simple):
            sk = sorted(simple[node], key=lambda st: st.key)
            mk = sorted(named.get(node, []), key=lambda m: m["key"])
            if mk and len(mk) == len(sk) and all(self._compatible(m, st) for m, st in zip(mk, sk)):
                for m, st in zip(mk, sk):
                    out.append({**m, "simple_key": st.key})
                continue
            if mk:
                generic_nodes.append(node)
            out.extend(g for g in (self._generic_key(st) for st in sk) if g)
        if generic_nodes:
            _LOGGER.info(
                "Vitrea: nodes %s have keys the mobile list cannot be matched to safely; "
                "their keys use generic names", generic_nodes,
            )
        missing = sorted(set(named) - set(simple))
        if missing:
            _LOGGER.info("Vitrea: nodes %s are named by the V-Box but not reported by it; skipped", missing)
        return out

    def _start_reauth(self) -> None:
        entry = self.hass.config_entries.async_get_entry(self.entry_id) if self.entry_id else None
        if entry is not None:
            entry.async_start_reauth(self.hass)

    def _async_apply_rooms_as_areas(self) -> None:
        """With the "rooms as areas" option on, put each node's device in the
        area named after its V-Box room, creating the area only if no area
        of that name exists (HA matches names case-insensitively). Works on
        devices that already exist, but never overrides an area the user
        has set.
        """
        if not (self._use_areas and self.entry_id):
            return
        from homeassistant.helpers import area_registry as ar, device_registry as dr

        areas, devices = ar.async_get(self.hass), dr.async_get(self.hass)
        rooms: dict[int, str] = {}
        for k in self.keys:
            if k.get("room"):
                rooms.setdefault(k["node"], k["room"])
        for node, room in rooms.items():
            dev = devices.async_get_or_create(
                config_entry_id=self.entry_id, **self.device_info(node, None)
            )
            if dev.area_id:
                continue
            area = areas.async_get_area_by_name(room) or areas.async_create(room)
            devices.async_update_device(dev.id, area_id=area.id)

    def _async_migrate_unique_ids(self) -> None:
        """Move entities registered under admin key indices to mobile ones.

        Entities were originally created with unique_id vitrea_{node}_{admin
        key}. Keys after an MW alias now have a different (mobile) index, so
        without this HA would orphan them and create duplicates, losing
        renames/areas. Match by (node, name); two phases via temporary IDs
        so swapped indices on one node can't collide mid-update.
        """
        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(self.hass)
        by_name = {(k["node"], k["name"].strip().lower()): k for k in self.keys}
        moves: list[tuple[str, str]] = []
        for e in list(reg.entities.values()):
            if e.platform != DOMAIN:
                continue
            m = re.fullmatch(r"vitrea_(\d+)_(\d+)", e.unique_id or "")
            if m is None:
                continue
            node = int(m.group(1))
            k = by_name.get((node, (e.original_name or "").strip().lower()))
            if k is None:
                continue
            new = f"vitrea_{node}_{k['key']}"
            if new != e.unique_id:
                moves.append((e.entity_id, new))
        moved = {eid for eid, _ in moves}
        # Never delete anything: an entity outside this migration that already
        # owns a target ID makes that one move impossible, so skip it.
        owners = {e.unique_id: e.entity_id for e in reg.entities.values() if e.platform == DOMAIN}
        blocked = [(eid, new) for eid, new in moves if owners.get(new) not in (None, eid) and owners[new] not in moved]
        for eid, new in blocked:
            _LOGGER.warning("Vitrea: not migrating %s, unique id %s is taken by %s", eid, new, owners[new])
        moves = [m for m in moves if m not in blocked]
        if not moves:
            return
        for eid, _ in moves:
            reg.async_update_entity(eid, new_unique_id=f"vitrea_migrating_{eid}")
        for eid, new in moves:
            try:
                reg.async_update_entity(eid, new_unique_id=new)
                _LOGGER.info("Vitrea: migrated %s to unique_id %s", eid, new)
            except ValueError as err:
                _LOGGER.warning("Vitrea: could not migrate %s to %s: %s", eid, new, err)

    # ---------------------------------------------------------------
    # Metadata: names/types/rooms/scenarios, fetched over the same mobile
    # connection used for control (opcodes 20/21/30/31/40/60/61). The
    # admin protocol is no longer used at all.
    # ---------------------------------------------------------------
    def _paced(self, method: str, *args, attempts: int = 4):
        """Call a metadata request on the mobile client, paced and retried.

        Confirmed live 2026-10-26: after a few hundred requests the box
        stops answering on a session (every later request then burns the
        10s read timeout, even with spacing between requests), but a fresh
        login answers again. So: space requests, re-login on a timeout,
        and re-login proactively every METADATA_RELOGIN_EVERY requests.
        """
        for attempt in range(attempts):
            try:
                if self._meta_count and self._meta_count % METADATA_RELOGIN_EVERY == 0:
                    self._connect_mobile()
                self._meta_count += 1
                self._last_activity = time.monotonic()
                result = getattr(self._mobile, method)(*args)
                time.sleep(METADATA_PACE)
                return result
            except Exception:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.5 * (attempt + 1))
                try:
                    self._connect_mobile()
                except Exception:
                    pass

    def _fetch_metadata(self) -> tuple[list[dict], list[dict]]:
        paced = self._paced
        self._meta_count = 0
        room_names: dict[int, str] = {}
        try:
            for rno in paced("get_room_numbers"):
                try:
                    room_names[rno] = paced("get_room_name", rno)
                except Exception:
                    room_names[rno] = f"Room {rno}"
        except Exception:
            pass

        keys: list[dict] = []
        for nno in paced("get_node_numbers"):
            try:
                info = paced("get_node_params", nno)
            except Exception as err:
                _LOGGER.warning("Vitrea: node %s params unavailable: %s", nno, err)
                continue
            node_cat = node_category(info["node_type"])
            # The mobile key table lists only physical (class 255) keys, in
            # order, so position == mobile key index. MW aliases (admin
            # class 0) are simply absent — see _mobile_client.get_key_params.
            for ki in range(info["num_keys"]):
                try:
                    kp = paced("get_key_params", nno, ki)
                except Exception:
                    _LOGGER.warning("Vitrea: key params for N%s-K%s unavailable, skipping", nno, ki)
                    continue
                name = kp["name"] or f"N{nno}-K{ki}"
                lname = name.strip().lower()
                cat_id = kp["category"]
                if cat_id == 0:
                    continue  # NotExist — genuinely no key here
                if lname.endswith("mw") or " pair" in lname:
                    continue  # named MW/pair alias convention
                if (nno, ki) in self._excluded_keys:
                    continue

                dimmable = cat_id in {5, 10} or node_cat == "Dimmer"
                if "fan" in lname:
                    ha_type = "fan"
                elif cat_id in {3, 11}:
                    # Reliable signal on its own — every confirmed blind
                    # key reports cat_id 3/11 directly (node_cat=="Blind"
                    # misclassified reading lights that share a Blind-
                    # type node with real blind keys).
                    ha_type = "cover"
                elif dimmable or node_cat == "Toggle" or "light" in lname:
                    # "light" in the name: reading lights sit on Blind-type
                    # nodes (seen live: a reading light sharing a node with
                    # a blind) and would otherwise fall through to "switch".
                    ha_type = "light"
                else:
                    ha_type = _KEY_CAT_HA.get(cat_id, "switch")

                # Boiler keys are timed switches (CustomBoilers in the app):
                # "on" must carry timer 0xFFFF. Category 15 is the admin
                # enum's Boiler; type 21 is the app's NODE_KEY_BOILER.
                boiler = cat_id == 15 or kp.get("type") == 21
                if boiler:
                    ha_type = "switch"

                keys.append({
                    "node": nno,
                    # The mobile slot index is the only key index now, so
                    # "key" == "mobile_key". Unique IDs built from the old
                    # admin index are moved over by
                    # _async_migrate_unique_ids.
                    "key": ki,
                    "mobile_key": ki,
                    "name": name,
                    "ha_type": ha_type,
                    "dimmable": dimmable,
                    "boiler": boiler,
                    # Recorded, not used for classification yet: the
                    # app's ENodeKeyType (2 toggle, 3 blind, 13 toggle
                    # sense, 20 dimmer, 21 boiler, 23 dual pole) should
                    # match this byte, but that is unverified live.
                    "key_type": kp.get("type"),
                    "category": cat_id,
                    "room": room_names.get(kp["room_no"] or info["room_no"], ""),
                })

        counts: dict[tuple, int] = {}
        for k in keys:
            counts[(k["key_type"], k["category"], k["ha_type"])] = counts.get((k["key_type"], k["category"], k["ha_type"]), 0) + 1
        _LOGGER.info(
            "Vitrea key (type, category, platform) -> count: %s",
            ", ".join(f"{t}/{c}/{p}={n}" for (t, c, p), n in sorted(counts.items(), key=str)),
        )

        scenarios: list[dict] = []
        try:
            numbers = list(paced("get_scenario_numbers"))
        except Exception as err:
            _LOGGER.warning("Vitrea: scenario list unavailable: %s", err)
            numbers = []
        # Opcode 66 is the list behind the app's Enable/Disable screen. The
        # regular list can omit scenarios (seen live: 37/38/39/42), so take
        # the union. attempts=1: a NACK here must not trigger re-logins.
        try:
            statuses = paced("get_scenario_statuses", attempts=1)
        except Exception as err:
            _LOGGER.info("Vitrea: scenario status list unavailable: %s", err)
            statuses = {}
        for sno in sorted(set(numbers) | set(statuses)):
            try:
                scenarios.append({
                    "id": sno,
                    "name": paced("get_scenario_name", sno),
                    "listed": sno in numbers,
                    "has_status": sno in statuses,
                })
            except Exception:
                _LOGGER.warning("Vitrea: scenario %s name unavailable", sno)
        extra = [s["id"] for s in scenarios if not s["listed"]]
        if extra:
            _LOGGER.info("Vitrea: scenarios %s are not in the regular list but exist (op 66)", extra)
        self.scenario_enabled.update(statuses)
        return keys, scenarios

    # ---------------------------------------------------------------
    # Mobile protocol: short sessions only (metadata, scenario flags).
    # ---------------------------------------------------------------
    def _connect_mobile(self) -> None:
        if self._mobile is not None:
            try:
                self._mobile.close()
            except Exception:
                pass
            self._mobile = None
        m = MobileVBox(
            host=self._host,
            port=self._mobile_port,
            user=self._mobile_user,
            password=self._mobile_password,
        )
        try:
            m.connect()
            m.login()
            # The official app follows login with version -> super-user ->
            # timestamp before anything else; do the same.
            m.handshake(timeout=3.0)
        except Exception:
            # Don't leak a half-open session: the box allows one per account.
            try:
                m.close()
            except Exception:
                pass
            raise
        self._mobile = m
        self.controller_version = getattr(m, "controller_version", None) or self.controller_version

    def _mobile_job(self, fn):
        """Run fn() on a fresh mobile login, then log out again."""
        with self._mobile_lock:
            self._connect_mobile()
            try:
                return fn()
            finally:
                try:
                    self._mobile.close()
                except Exception:
                    pass
                self._mobile = None

    # ---------------------------------------------------------------
    # Live state (simple protocol push)
    # ---------------------------------------------------------------
    def _on_status(self, st: Status) -> None:
        self._last_status = time.monotonic()
        kind = st.kind
        if kind is None or kind == KIND_BUTTON:
            return  # momentary physical-key events, not state
        slot = (st.node, st.key)
        prev = self._states.get(slot)
        if kind == KIND_BLIND:
            ks = KeyState(False, st.position if st.position is not None else 0)
        elif kind == KIND_DIMMER:
            try:
                level = int(st.data) if st.data is not None else 0
            except ValueError:
                level = 0
            ks = KeyState(level > 0, level)
        elif st.is_on:
            # Plain on/off carries no level; keep the last known one so a
            # dimmable light does not drop to 1% brightness.
            ks = KeyState(True, prev.ratio if prev and prev.ratio else 100)
        else:
            ks = KeyState(False, 0)
        self._states[slot] = ks
        self._apply_state(self._entities.get(slot), ks)

    def _on_link(self, up: bool) -> None:
        self.available = up
        if up:
            _LOGGER.info("Vitrea V-Box connected")
            if self.metadata_ready:
                self.hass.async_create_background_task(self._async_refresh_states(), "vitrea_refresh")
        else:
            _LOGGER.warning("Vitrea V-Box connection lost, reconnecting")
        for entity in list(self._entities.values()):
            if getattr(entity, "hass", None) is not None:
                try:
                    entity.async_write_ha_state()
                except Exception:
                    _LOGGER.exception("Vitrea: refreshing availability of %s failed", entity)

    @staticmethod
    def _apply_state(entity, ks) -> None:
        # An entity is registered in __init__ but only gets .hass once HA
        # adds it; writing state before then raises ("Attribute hass is
        # None"). async_added_to_hass reads its own initial state.
        if entity is None or getattr(entity, "hass", None) is None:
            return
        try:
            entity.on_push_state(ks.is_on, ks.ratio)
        except Exception:
            _LOGGER.exception("Vitrea: applying state to %s failed", getattr(entity, "entity_id", entity))

    async def _async_wait_quiet(self) -> None:
        """Wait until the box has stopped streaming status lines."""
        start = time.monotonic()
        while time.monotonic() - start < REFRESH_NODE_MAX:
            await asyncio.sleep(0.25)
            if time.monotonic() - self._last_status > REFRESH_QUIET:
                return

    async def _async_refresh_states(self) -> None:
        """Re-read every node's states, one node at a time with gaps. The
        answers arrive as pushes. Commands go first: while one was sent
        recently the refresh waits."""
        if self._simple is None:
            return
        for node in sorted({k["node"] for k in self.keys}):
            while time.monotonic() - self._last_command < POLL_QUIET_AFTER_COMMAND:
                await asyncio.sleep(1.0)
            if not self._simple.connected:
                return
            try:
                await self._simple.request_node(node)
            except VitreaError as err:
                _LOGGER.debug("Vitrea: refresh of node %s failed: %s", node, err)
                continue
            await self._async_wait_quiet()
            await asyncio.sleep(REFRESH_GAP)

    async def _async_poll_tick(self, _now) -> None:
        """Re-read one node per tick to correct any missed push."""
        if not self.available or self._simple is None:
            return
        self._poll_ticks += 1
        if self._poll_ticks % SAVE_STATES_EVERY == 0:
            await self._async_save_states()
        if time.monotonic() - self._last_command < POLL_QUIET_AFTER_COMMAND:
            return
        # While a full dump is streaming the box rejects other queries.
        if time.monotonic() - self._last_status < 5.0:
            return
        nodes = sorted({n for n, _ in self._entities})
        if not nodes:
            return
        node = nodes[self._poll_index % len(nodes)]
        self._poll_index += 1
        try:
            await self._simple.request_node(node)
        except VitreaError as err:
            _LOGGER.debug("Vitrea: poll of node %s failed: %s", node, err)

    async def _async_save_states(self) -> None:
        try:
            await self._state_store.async_save({
                "states": {f"{n},{k}": [s.is_on, s.ratio] for (n, k), s in self._states.items()}
            })
        except Exception:
            _LOGGER.debug("Vitrea: saving states failed", exc_info=True)

    async def async_get_key_status(self, node: int, key, mobile: bool = False):
        """Last known state of a key (from push, a refresh, or the saved
        copy from the previous run), or None if the box has not reported it.
        """
        skey = self._skey.get((node, key))
        return self._states.get((node, skey)) if skey is not None else None

    # ---------------------------------------------------------------
    # Commands
    # ---------------------------------------------------------------
    async def _async_command(self, awaitable) -> None:
        self._last_command = time.monotonic()
        try:
            await awaitable
        except VitreaError as err:
            raise HomeAssistantError(f"Vitrea V-Box: {err}") from err

    def _link(self) -> SimpleVBox:
        if self._simple is None:
            raise HomeAssistantError("Vitrea V-Box is not connected")
        return self._simple

    async def async_key_on(self, node: int, key, ratio: int = 0, timer: int = 0) -> None:
        # ratio 254 is the app's "restore last level" sentinel; a plain on
        # does that on the box.
        skey = self._simple_key(node, key)
        if ratio and ratio != 254:
            await self._async_command(self._link().dimmer(node, skey, ratio))
        else:
            await self._async_command(self._link().key_on(node, skey))

    async def async_key_off(self, node: int, key, timer: int = 0) -> None:
        await self._async_command(self._link().key_off(node, self._simple_key(node, key)))

    async def async_boiler_on(self, node: int, key) -> None:
        await self._async_command(self._link().key_on(node, self._simple_key(node, key)))

    async def async_set_cover_position(self, node: int, key, ratio: int) -> None:
        await self._async_command(self._link().blind_position(node, self._simple_key(node, key), ratio))

    async def async_stop_cover(self, node: int, key) -> None:
        await self._async_command(self._link().blind_stop(node, self._simple_key(node, key)))

    async def async_trigger_scenario(self, scenario_id: int) -> None:
        await self._async_command(self._link().scenario(scenario_id))

    def device_info(self, node: int, room: str | None = None):
        """Device registry entry for a V-Box node (a wall unit / module),
        parented to the V-Box itself. None when the hub has no config
        entry id (offline tests).
        """
        if self.entry_id is None:
            return None
        from homeassistant.helpers.device_registry import DeviceInfo

        info = DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry_id}_node_{node}")},
            name=f"V-Box node {node}",
            manufacturer="Vitrea",
            # No via_device: recent HA versions reject it when entities
            # create the device (seen live on 2026.10), which stopped every
            # entity from being added.
        )
        if self._use_areas and room:
            info["suggested_area"] = room
        return info

    def scenes_device_info(self):
        """One device for all scenes and scenario switches, so they are not
        left ungrouped. None when the hub has no config entry id."""
        if self.entry_id is None:
            return None
        from homeassistant.helpers.device_registry import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry_id}_scenes")},
            name="Vitrea Scenes",
            manufacturer="Vitrea",
        )

    def hub_device_info(self):
        from homeassistant.helpers.device_registry import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, self.entry_id)},
            name="Vitrea V-Box",
            manufacturer="Vitrea",
            model="V-Box",
            sw_version=self.controller_version,
        )

    # ---------------------------------------------------------------
    # Scenario enable flags (mobile protocol, opcode 66). Needs mobile
    # credentials; each change is one short login.
    # ---------------------------------------------------------------
    def register_scenario_entity(self, scenario_id: int, entity) -> None:
        self._scenario_entities[scenario_id] = entity

    async def async_set_scenario_enabled(self, scenario_id: int, enabled: bool) -> None:
        if not self.has_mobile:
            raise HomeAssistantError("Enabling or disabling scenarios needs mobile credentials")
        try:
            await self.hass.async_add_executor_job(
                self._mobile_job,
                lambda: self._mobile.set_scenario_enabled(scenario_id, enabled),
            )
        except Exception as err:
            raise HomeAssistantError(f"Vitrea V-Box: {err}") from err
        self.scenario_enabled[scenario_id] = enabled
